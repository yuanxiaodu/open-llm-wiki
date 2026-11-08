"""Bound wiki tools with localized failures, optimistic writes and actor permissions.
Sources and quality use the shared store policy. Analyses are versioned drafts until the
publication contract is met; restricted members may repair their own drafts only.
Read supports source windows, task reports and persisted compilation candidates.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import re
import sqlite3
import subprocess
from typing import Any, Literal, get_type_hints

from agents import RunContextWrapper, function_tool
from pydantic import BaseModel, Field, ValidationError, create_model

from ..config import settings
from ..services import document_ingest
from ..services import search as search_service
from ..services import web_ingest
from ..services import web_search as web_search_service
from ..wiki import repo, serialize, store
from ..wiki.quality import default_confidence, normalize_confidence
from ..wiki.analysis import AnalysisSpec, prepare as prepare_analysis
from ..wiki.provenance import canonical_url, source_file

logger = logging.getLogger("openllmwiki.agent")

# 写路径失败的返回串前缀（成功是"已生效："，读路径失败是"读不到："，那两段只有工具
# 自己认，不必提常量）。裁定 R-S2-14：这个前缀全站只此一份字面量——hooks 判 tool-result
# 的 ok 用的就是它（`not text.startswith(FAIL_PREFIX)`），两边各写一份的话，改一次文案
# 就会把 ok 悄悄翻成一路 True（SSE 上"这一轮写砸了"从此不亮红）。
# 工具返回的失败串一律以 FAIL_TEXT 开头，它就是前缀加上那个全角冒号。
FAIL_PREFIX = "没写成"
FAIL_TEXT = f"{FAIL_PREFIX}："

# 提交主题 `{kind}|{action}|{path}|{actor}|{note}` 的 actor 字段：记的是**触发这一轮
# 的人**（AgentContext.actor_name）——某人摄入内容之后接着的建页/改页/删页/网页摄入
# 都要记到这个人名下（2026-10-30 裁定），日志里才看得出这串连锁动作是谁引起的。
# 拿不到姓名（脚本裸造 AgentContext、旧测试）才回退这个常量（见 _actor_label）。
AGENT_ACTOR = "agent"
# note 里不再出现"智能体"：actor 已经是成员本人，note 只说明这次提交干了什么。
NOTE_CREATE = "建页"
NOTE_EDIT = "改页"
NOTE_DELETE = "删页"

# 会改 vault 的工具名（hooks 用它标记"这一轮动过 wiki"，runner 据此决定收尾要不要
# 跑一次 lint 看还有没有 error）。名字只在这里写一份，runner 与 hooks 都从这里取。
WRITE_TOOL_NAMES = ("write_page", "edit_page", "delete_page")

# 只有使用权限的成员允许新建的页面类型（2026-10-30 委托人二次确认）：
#   - query / comparison / summary 是"把已有内容整理成一张查询/对照/摘要页"，
#     属于查询的沉淀，不是修改既有知识；
#   - entity / concept 是建设知识库本体，不在只读权限内；
#   - 可 edit 自己的分析草稿；发布、编辑已发布页和 delete_page 仍拒绝。
READONLY_WRITE_TYPES = ("query", "comparison", "summary")

# 一次交给模型的正文/材料上限。与 search.MAX_BODY 同一个数：检索里能搜到的那一截，
# 与 read 端出来的那一截，长度口径一致，模型不会以为"搜到的比读到的多"。
READ_MAX = search_service.MAX_BODY
SEARCH_LIMIT = 8          # 一次搜索给模型的命中条数（每条一行，太多就顶掉正文的窗口）
SNIPPET_MAX = 200         # 一行命中里 snippet 的截断长度，与 search.LIKE_SNIPPET 同口径
LIST_MAX = 200            # list_wiki 每一段最多列多少条（防止材料堆起来顶爆上下文）
# lint 报告给模型看的条数上限。**error 永远全列**（截断只截 warn/info）：生产上出过
# 一次 3 条 error 被 186 条 orphan warn 挤出前 60 条、模型只看见 1 条的故障，所以
# 排序也改成 error 优先（见 _lint）。LINT_WARN_MIN 是"error 再多也要留几条 warn"的底线。
LINT_MAX = 60
LINT_WARN_MIN = 20
HISTORY_MAX = 3           # read 一页时附带的最近版本条数

# 标题上限与 app/routers/api/wiki.py 的 TITLE_MAX 同源（裁定 R86）：智能体写的页与人写的
# 页是同一套渲染，超长标题一样会把一行顶破。两处各写一份字面量，改一处就漂。
TITLE_MAX = 120

# read 的旧入口（保留 S2 的 `upload/{编号}`，材料正文仍从 extracted_text 读）；
# 其余路径一律是 vault 内的相对 path。
_UPLOAD_PATH_RE = re.compile(r"^upload/(?P<upload_id>\d{1,9})$")

# 特殊文件：URL / 工具入参里认这几种写法（大小写不敏感）。它们不是页面，走 read_special。
_SPECIAL_READ = {
    "index": "index",
    "index.md": "index",
    "schema": "schema",
    "schema.md": "schema",
    "log": "log",
    "log.md": "log",
}
WHITELIST_TEXT = (
    "只许读 entities/concepts/comparisons/queries/summaries 下的页面、raw/ 里的材料、"
    "index.md / SCHEMA.md / log.md，或旧入口 upload/{编号}"
)

# search 的三种范围 → fts_shared 的 kind（spec §6.1：scope ∈ wiki|raw|all）。
# wiki=只搜页面；raw=只搜"尚未成页的原始语料"——人会话说的话与上传材料（它们是引注的
# 来源）；all=三样都搜。outline 那个 scope 随大纲页一起退休（大纲已归书模块）。
SCOPE_KINDS = {
    "wiki": ("page",),
    "raw": ("message", "upload"),
    "all": ("message", "upload", "page"),
}
KIND_LABEL = {"message": "消息", "upload": "材料", "page": "页面"}
PARSE_STATE = {"done": "已解析", "failed": "解析失败", "none": "这类不解析"}



# --- 错误串：SDK 的入参闸 + 工具自己的兜底 ------------------------------------


def _where(loc) -> str:
    """pydantic 的 loc 拼成人看得懂的字段路径：("citations", 0, "key") → citations[0].key。"""
    out = ""
    for part in loc or ():
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out or "参数"


def _expected(raw) -> str:
    """pydantic 的 ctx["expected"] 摊成人话。

    Literal 校验不过时它给的是一个**字符串**，而且最后两项之间是 " or "
    （`"'topic', 'concept' or 'comparison'"`）：直接 join 会一个字符一个字符地拆开
    （"只许 '、t、o、p、i、c…"）。所以先把 " or " 当分隔符替成逗号，再按逗号拆、去引号。
    我们的白名单值里既没有逗号也没有 " or "，这么拆是安全的。
    """
    if isinstance(raw, str):
        parts = raw.replace(" or ", ",").split(",")
    elif isinstance(raw, (list, tuple, set, frozenset)):
        parts = [str(p) for p in raw]
    else:
        parts = []
    cleaned = [p.strip().strip("'\"") for p in parts]
    cleaned = [p for p in cleaned if p]
    return "、".join(cleaned) if cleaned else "规定的几个值"


def _cn_reason(err: dict) -> str:
    """一条 pydantic 校验错误翻成一句中文。翻不出来的就带上 pydantic 自己的 msg。"""
    where = _where(err.get("loc"))
    kind = err.get("type", "")
    ctx = err.get("ctx") or {}
    if kind == "missing":
        return f"缺了 {where}"
    if kind == "string_pattern_mismatch":
        return f"{where} 不合法：只许字母、数字、下划线和连字符"
    if kind == "literal_error":
        return f"{where} 不合法：只许 {_expected(ctx.get('expected'))}"
    if kind in ("int_parsing", "int_type", "int_from_string"):
        return f"{where} 的类型不对：要一个整数"
    if kind == "string_type":
        return f"{where} 的类型不对：要一段文字"
    if kind == "list_type":
        return f"{where} 的类型不对：要一个清单"
    if kind == "model_type":
        return f"{where} 的类型不对：要一个对象"
    if kind == "extra_forbidden":
        return f"{where} 是多余的，这个工具没有这个参数"
    return f"{where} 不合法：{err.get('msg') or kind}"


def _args_model(func) -> type[BaseModel]:
    """按**工具自己的签名**造一个只用于诊断的 pydantic 模型。

    不另写一份 args 模型：签名是唯一的事实源，两份迟早漂（改了签名忘了改诊断模型，
    模型就收到一句"参数不对"却问不出哪儿不对）。ctx 那一参不算入参。
    """
    hints = get_type_hints(func)
    fields: dict[str, Any] = {}
    for name, param in inspect.signature(func).parameters.items():
        if name == "ctx":
            continue
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[name] = (hints.get(name, Any), default)
    return create_model(f"{func.__name__}_args", **fields)


def _args_detail(model: type[BaseModel], ctx) -> str | None:
    """拿原始参数再校验一遍，返一句中文的字段级原因；参数没问题就返 None。

    原始参数从 `ctx.tool_arguments` 取——真跑时 SDK 放进去的就是模型交上来的那个串，
    与 on_invoke_tool 的 input 同源。返回 None 说明"这一次不是入参的错"，调用方据此
    走"工具自己出错了"那一支。
    """
    raw = getattr(ctx, "tool_arguments", "") or ""
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return "交上来的参数不是合法的 JSON，原样重发一次"
    if not isinstance(data, dict):
        return "参数得是一个 JSON 对象"
    try:
        model.model_validate(data)
    except ValidationError as exc:
        return "；".join(_cn_reason(e) for e in exc.errors())
    return None


def _under_db_lock(func):
    """工具体从头到尾持着 AgentContext.db_lock（裁定 R-S2-17）。

    工具体跑在 asyncio.to_thread 的 worker 线程，relay（_append_text/_seal/_event）与
    hooks 跑在事件循环线程，踩的是**同一条** sqlite 连接；
    max_function_tool_concurrency=1 只把工具与工具之间串起来，管不了这条缝。不持锁，
    两个线程同时开隐式事务就是 "cannot start a transaction within a transaction"，
    而且现象随线程调度漂。7 人规模，全串行的代价无感。

    套在 _tool 里、function_tool 之外：SDK 见到的是这个 wrapper（functools.wraps 把
    签名与 docstring 原样带过去，模型看到的说明书不变），锁一定在真工具体外面。
    """

    @functools.wraps(func)
    def wrapper(ctx, *args, **kwargs):
        with ctx.context.db_lock:
            return func(ctx, *args, **kwargs)

    return wrapper


def _tool(prefix: str):
    """装饰器 = @function_tool + 持 db_lock + 一个把入参错误翻成中文的 failure_error_function。

    为什么不用 SDK 默认的那个：0.24.0 里 `_debug.DONT_LOG_TOOL_DATA` 默认为真，
    agents/tool.py 的 _prepare_arguments 在校验失败时**把 ValidationError 整个丢掉**，
    只抛一句 ModelBehaviorError("Invalid JSON input for tool X")；默认的
    default_tool_error_function 再把它换成 "An error occurred while running the tool.
    Please try again."。模型既不知道是哪个字段、也不知道该怎么改，只会原样重发一遍，
    白烧一轮。这里仍走 SDK 的 failure_error_function 口子（错误串当 tool-result 交回
    模型、这一轮不失败），只是把内容换成中文的字段级原因。
    """

    def decorate(func):
        model = _args_model(func)

        def on_error(ctx, error: Exception) -> str:
            detail = _args_detail(model, ctx)
            if detail:
                return f"{prefix}参数不对，{detail}。照着每个参数的说明重发一次"
            # 走到这里说明工具里冒出了没被 _guard 接住的异常：记一条日志给运维，
            # 给模型一句不含 traceback 的中文（工具绝不把异常抛给 runner）。
            logger.error("工具 %s 抛出没接住的异常：%r", func.__name__, error, exc_info=error)
            return f"{prefix}工具自己出错了（{type(error).__name__}），这一次没做成"

        return function_tool(failure_error_function=on_error)(_under_db_lock(func))

    return decorate


def _conn(ctx) -> sqlite3.Connection:
    """从 ctx.context 取数据库连接（runner 交进来的是 AgentContext，见模块 docstring）。"""
    return ctx.context.conn


def _actor_label(ctx) -> str:
    """这一轮的写动作记到谁名下：触发这一轮的人；拿不到姓名才回退 AGENT_ACTOR。

    2026-10-30 裁定：连锁动作不能再记成"智能体"。委托人看到的是"某个人摄入内容"，
    后面的建/改/删页与网页摄入归到同一个人名下，日志才是一条完整的因果链。
    runner 把人的姓名放进 AgentContext.actor_name；脚本与旧测试裸造上下文时是空串，
    这时才退回 AGENT_ACTOR，保证提交主题的 actor 字段永不为空。
    """
    name = str(getattr(ctx.context, "actor_name", "") or "").strip()
    return name or AGENT_ACTOR


def _can_build(ctx) -> bool:
    """这一轮能不能建设 wiki（摄入材料 + 建/改/删页）。缺省 True：裸造上下文的旧行为。

    判据来自 AgentContext.can_build_wiki（runner 从路由交来的 actor dict 里取，
    已经按 users.can_build_wiki + 管理员恒真算好）。工具不再自己查库：
    可见性/能力只在 access 一处判，这里只认快照。
    """
    allowed = bool(getattr(ctx.context, "can_build_wiki", True))
    uid = getattr(ctx.context, "user_id", None)
    if allowed and uid is not None:
        from ..services.access import can_build_wiki
        user = _conn(ctx).execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return user is not None and can_build_wiki(user)
    return allowed


def _message_owner_id(ctx) -> int | None:
    """search 里消息命中的 owner 过滤：None = 不过滤（`all` 模式与管理员）。

    `own` 模式下 runner 交的是触发这一轮的 user id，search 只留这个人的会话里的消息。
    """
    value = getattr(ctx.context, "message_owner_id", None)
    return None if value is None else int(value)


def _denied_text(what: str) -> str:
    """没有建设权限时的工具拒绝串。以 FAIL_TEXT 开头：hooks 与模型都按"这次没做成"认。

    文案要明确"还能做什么"：只写"不能"的话，模型容易把它当临时故障反复重试，
    白烧轮次。兜底口径与 wiki 路由的 403 一致（同一件事，人的那一面与智能体这一面）。
    """
    return (
        f"{FAIL_TEXT}当前账号只有使用权限，不能{what}。"
        "可以查询、搜索、阅读；需要沉淀查询结果时，"
        "可以新建 page_type 为 query / comparison / summary 的查询页、对照页或摘要页。"
    )


def _guard(prefix: str, func, *args, **kwargs) -> str:
    """工具的统一兜底：把没接住的异常翻成一句中文。**工具绝不往外抛异常。**

    接的是这一层真会碰到的四类：store.StoreError（读侧 git 故障、回滚没做成）、
    repo.GitError（写侧漏出来的 git 非零退出）、ValueError（path/action 形状闸）、
    OSError（磁盘满、只读挂载、文件被删），外加 sqlite3.Error（库锁死、连接被关）。
    别的异常（程序 bug）由 _tool 的 failure_error_function 兜住，同样不出工具。
    """
    try:
        return func(*args, **kwargs)
    except store.ConflictError as exc:
        return _conflict_text(prefix, exc)
    except (store.StoreError, repo.GitError, ValueError, OSError, sqlite3.Error) as exc:
        logger.warning("工具 %s 出错：%r", func.__name__, exc, exc_info=exc)
        detail = " ".join(str(exc).split())[:200] or type(exc).__name__
        return f"{prefix}出错了，{detail}"


def _conflict_text(prefix: str, exc: store.ConflictError) -> str:
    """乐观锁冲突（裁定 R33）：告诉模型"别人先改了"，并把差异给它看一眼。"""
    diff = (exc.diff_text or "").strip()
    tail = f"\n改前改后的差异（最多前 1200 字）：\n{diff[:1200]}" if diff else ""
    return (
        f"{prefix}{exc.reason}。你交上来的 base_commit 不是现行版本，"
        f"先 read 一遍这一页拿到现行版本号，把要改的内容合进去再重写一次。{tail}"
    )


def _window(text: str, path: str, offset: int, limit: int) -> str:
    end = min(len(text), offset + limit)
    return f"{path}｜字符 {offset}..{end} / {len(text)}\n{text[offset:end]}\n下一窗口 offset={end}"


def _clip(text: str, what: str) -> str:
    """正文/材料超过 READ_MAX 就只给前面一截，并说清还剩多少、怎么拿到剩下的。"""
    text = text or ""
    if len(text) <= READ_MAX:
        return text
    return (
        f"{text[:READ_MAX]}\n…（{what}后面还有 {len(text) - READ_MAX} 字没给全，"
        f"先用 search 定位，或 read(path, offset=20000, limit=20000) 续读）"
    )


# --- 六个工具的实现（真正跑的那一段） -----------------------------------------


def _read(conn: sqlite3.Connection, path: str, offset: int = 0, limit: int = READ_MAX) -> str:
    """按 path 读：页面 / raw 材料 / 特殊文件 / 旧 upload 入口。"""
    if offset < 0 or not 1 <= limit <= READ_MAX:
        raise ValueError(f"offset ≥0，limit 在 1..{READ_MAX}")
    windowed = offset != 0 or limit != READ_MAX
    text = (path or "").strip()
    if not text:
        return f"读不到：{WHITELIST_TEXT}（收到的是空串）"
    # 旧模型会把前缀写成 wiki/；页面的 path 本身就是 vault 相对路径，去掉这一层再认。
    if text.startswith("wiki/"):
        text = text[len("wiki/"):]

    hit = _UPLOAD_PATH_RE.match(text)
    if hit:
        if windowed:
            body = search_service.upload_text(conn, int(hit.group("upload_id")))
            return _window(body, text, offset, limit) if body is not None else "读不到：材料没有正文"
        return _upload_read(conn, int(hit.group("upload_id")))

    job_path = re.fullmatch(r"jobs/(\d+)(/candidates)?", text)
    if job_path:
        job_id = int(job_path.group(1))
        job = document_ingest.get_job(conn, job_id)
        if job is None:
            return "读不到：没有这个摄入任务"
        if job_path.group(2):
            rows = conn.execute("SELECT id,chunk_index,candidate_json,status,result_path,note FROM ingest_candidates WHERE job_id=? ORDER BY id", (job_id,)).fetchall()
            payload = [{**dict(row), "candidate": json.loads(row["candidate_json"])} for row in rows]
        else:
            payload = job
        return _window(json.dumps(payload, ensure_ascii=False, indent=2), text, offset, limit)
    if text == "_meta/topic-map.md":
        target = settings.wiki_dir / text
        if not target.is_file():
            return "读不到：主题地图尚未生成"
        return _window(target.read_text(encoding="utf-8"), text, offset, limit)
    if text.startswith(serialize.RAW_DIR + "/"):
        raw = store.read_raw(text)
        if raw is None:
            return (
                f"读不到：raw 里没有 {text}。用 list_wiki 看有哪些 raw 材料"
                f"（raw/uploads/{'{上传编号}-{文件名}'}.md）"
            )
        return _window(raw, text, offset, limit) if windowed else f"raw 材料：{text}\n{_clip(raw, '这份材料')}"

    special = _SPECIAL_READ.get(text.lower())
    if special is not None:
        got = store.read_special(special)
        if got is None:
            return f"读不到：{text} 还没生成（服务端启动时会建出来）"
        if windowed:
            return _window(got["body_md"], text, offset, limit)
        return f"{got['path']}（最后更新 {got['updated_at']}）：\n{_clip(got['body_md'], '这份文件')}"

    page = store.get_page(conn, text)
    if page is None:
        return (
            f"读不到：wiki 里没有这一页（path 收到的是 {path!r}）。"
            f"用 list_wiki 看看有哪些页，或 search 找一找"
        )
    return _window(page["body_md"], page["path"], offset, limit) if windowed else _page_text(conn, page)


def _page_text(conn: sqlite3.Connection, page: dict) -> str:
    """一页的全文摘要：path/标题/类型/tags/sources/现行版本 + 正文 + 互链 + 最近版本。

    现行版本号一定要给：edit_page 的 base_commit 就是它，不给的话模型要么不校验乐观锁、
    要么瞎编一个（瞎编的那个会撞上 ConflictError，白烧一轮）。
    """
    head = page.get("head") or ""
    tags = "、".join(page.get("tags") or []) or "无"
    sources = "、".join(page.get("sources") or []) or "无"
    confidence = (page.get("confidence") or "").strip() or "未填"
    lines = [
        f"路径：{page['path']}",
        f"标题：{page['title']}",
        f"类型：{page['page_type']}",
        f"tags：{tags}",
        f"sources：{sources}",
        f"confidence：{confidence}",
        f"contested：{page.get('contested', False)}",
        f"contradictions：{page.get('contradictions', [])}",
        f"metadata：{json.dumps(page.get('metadata', {}), ensure_ascii=False)}",
        f"现行版本：{head[:12] or '（这一页还没有版本）'}",
        "正文：",
        _clip(page["body_md"], "正文"),
    ]
    links = page.get("wikilinks") or []
    if links:
        lines.append("页面互链（[[...]]）：")
        lines += [
            f"- {link['label']} → {link['path'] or '（断链：没有这一页）'}"
            for link in links
        ]
    backlinks = page.get("backlinks") or []
    if backlinks:
        lines.append("入链（谁链到这一页）：")
        lines += [f"- {item['path']}" for item in backlinks]
    history = store.versions(page["path"])[:HISTORY_MAX]
    if history:
        lines.append("最近版本（新到旧）：")
        lines += [
            f"- {entry['sha'][:12]}｜{entry['action']}｜{entry['actor'] or '不详'}"
            f"｜{entry['at']}｜{entry['note']}"
            for entry in history
        ]
    lines.append(f"要改这一页：edit_page(path=\"{page['path']}\", base_commit=\"{head[:12]}\")")
    return "\n".join(lines)


def _upload_read(conn: sqlite3.Connection, upload_id: int) -> str:
    """一份上传材料的解析文本窗口（旧 upload/{编号} 入口）。

    整文不超过 READ_MAX 就是全文（要引用原话时窗口切在中间会抄不全）；超过了给开头
    一截，并说清还剩多少。也告诉模型它在 raw 层的归档路径——sources 里要写这个。
    """
    row = conn.execute(
        "SELECT orig_filename, parse_status FROM upload_files WHERE id = ?", (upload_id,)
    ).fetchone()
    if row is None:
        return f"读不到：没有这一份上传材料（编号 {upload_id}）。用 list_wiki 看看有哪些"
    text = search_service.upload_text(conn, upload_id)
    if text is None:
        state = PARSE_STATE.get(row["parse_status"], row["parse_status"])
        return (
            f"读不到：这份材料（{row['orig_filename']}）没有解析文本（{state}），"
            f"读不到原文，写页时不要引用它"
        )
    raw_rel = store.raw_path_for(upload_id, row["orig_filename"])
    raw_note = (
        f"\nraw 归档：{raw_rel}" if (settings.wiki_dir / raw_rel).exists()
        else "\nraw 归档：（还没有，sources 里先不要写它）"
    )
    return (
        f"材料 {upload_id}：{row['orig_filename']}{raw_note}\n"
        f"解析文本（共 {len(text)} 字）：\n{_clip(text, '这份材料')}"
    )


def _list_wiki(conn: sqlite3.Connection, offset: int = 0, limit: int = LIST_MAX) -> str:
    """目录 + raw 清单。份数报的是**真数**（COUNT(*)），列出来的最多 LIST_MAX 条。

    两处上限都只为护住上下文窗口：页数真上到几百时，模型该用 search 而不是把整本目录
    读一遍。报真数、只截显示，模型才知道"还有多少没看见"。
    """
    if offset < 0 or not 1 <= limit <= 500:
        raise ValueError("offset ≥0，limit 在 1..500")
    pages = store.list_pages(conn)
    total = conn.execute("SELECT COUNT(*) AS n FROM upload_files").fetchone()["n"]
    rows = conn.execute(
        "SELECT id, orig_filename, parse_status, parse_report, created_at"
        " FROM upload_files ORDER BY id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    lines = [f"wiki 页 {len(pages)} 份（新到旧）："]
    if pages:
        for page in pages[offset:offset+limit]:
            tags = "、".join(page.get("tags") or []) or "无"
            summary = page.get("summary") or "（没有摘要）"
            lines.append(
                f"- {page['path']}｜{page['page_type']}｜{page.get('status', 'published')}｜tags: {tags}｜{summary}"
            )
        if len(pages) > offset + limit:
            lines.append(f"- （只列了最近改过的 {limit} 份，剩下的用 search 找，或 list_wiki(offset={offset+limit}) 续读）")
    else:
        lines.append("- （还没有页。第一页用 write_page 建）")
    lines.append("")
    lines.append(f"raw 材料 {total} 份（新到旧）：")
    if rows:
        lines += [_upload_line(row) for row in rows]
        if total > LIST_MAX:
            lines.append(f"- （只列了最近的 {limit} 份，剩下的用 search 找，或 list_wiki(offset={offset+limit}) 续读）")
    else:
        lines.append("- （还没有上传材料）")
    return "\n".join(lines)


def _upload_line(row) -> str:
    state = PARSE_STATE.get(row["parse_status"], row["parse_status"])
    if row["parse_report"]:
        try:
            report = json.loads(row["parse_report"])
        except ValueError:
            report = {}
        # pages_with_text 才是引得到的页数：表格与演讲者备注不在里面（提示词里也这么写）
        if report:
            state += f"（{report.get('pages_with_text', 0)}/{report.get('pages_total', 0)} 页有文字）"
    raw_rel = store.raw_path_for(row["id"], row["orig_filename"])
    raw_part = raw_rel if (settings.wiki_dir / raw_rel).exists() else "（没有 raw）"
    return f"- upload/{row['id']}｜{row['orig_filename']}｜{state}｜raw: {raw_part}"


def _term_fallback_hits(
    conn: sqlite3.Connection, query: str, kinds: tuple[str, ...],
    message_owner_id: int | None,
) -> list[dict]:
    """整句短语搜不到时，按空格拆成关键词再搜一遍。

    FTS 的 MATCH 是**整句短语**匹配：模型常把用户原话直接当 query 发下来
    （"产品实习 简历 投递"），库里这几个词分散在不同句子时就一条也搜不到，
    模型只好反复换词，把查询轮的预算烧光（2026-10-30 测试账号的真实回答就是
    卡在这里）。拆词是召回兜底，不改变"整句优先"的口径：整句有命中就绝不走这里。

    只取 2 字以上的词、最多 4 个；同一条命中按 (kind, ref_id) 去重，顺序按词序。
    """
    terms: list[str] = []
    for raw in re.split(r"\s+", (query or "").strip()):
        term = raw.strip()
        if len(term) < 2 or term in terms:
            continue
        terms.append(term)
        if len(terms) >= 4:
            break
    if len(terms) < 2:
        return []
    merged: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for term in terms:
        for hit in search_service.search(
            conn, term, kinds=kinds, limit=SEARCH_LIMIT,
            message_owner_id=message_owner_id,
        ):
            key = (str(hit["kind"]), str(hit["ref_id"]))
            if key in seen:
                continue
            seen.add(key)
            merged.append(hit)
    return merged[:SEARCH_LIMIT]


def _search(conn: sqlite3.Connection, query: str, scope: str,
            message_owner_id: int | None = None) -> str:
    if not (query or "").strip():
        return "没搜到：查询是空的。给一句要搜的话（三个字符以上才走全文索引，更短的走 LIKE）"
    kinds = SCOPE_KINDS.get(scope)
    if kinds is None:
        return f"读不到：scope 只许 wiki、raw、all（收到的是 {scope!r}）"
    # 一份材料切成多片、同一条查询命中几片的情况由 search_service 去重成一条；
    # 三种 scope 都是纯 kind 过滤，不再像 S2 那样在命中集上读 front matter 分大纲。
    # message_owner_id（2026-10-30）：own 模式下只让模型搜到自己的会话消息，
    # 与前端 /api/search 同一条判据（access.message_owner_filter → runner → 这里）。
    hits = search_service.search(
        conn, query, kinds=kinds, limit=SEARCH_LIMIT, message_owner_id=message_owner_id
    )
    if not hits:
        # 整句短语没命中：按词召回（见 _term_fallback_hits），避免模型把预算烧在换词上。
        hits = _term_fallback_hits(conn, query, kinds, message_owner_id)
    if not hits:
        return (
            f"没搜到与「{query}」相关的东西（范围 {scope}）。"
            f"换个说法再搜，或用 list_wiki 看看有些什么"
        )
    lines = [f"搜到 {len(hits)} 条（范围 {scope}）："]
    lines += [_hit_line(hit) for hit in hits]
    return "\n".join(lines)


def _hit_line(hit: dict) -> str:
    """一行命中：`【类别 定位符】标题：snippet`。

    定位符必须带上：页面是 path（read/edit_page 用），消息/材料是编号（read 上传时用）。snippet 压成单行，免得一条命中劈成好几行、行首的定位符认不出来。

    行内的 【】 换成 「」：search.search 的 snippet() 就是用 【】 标命中处的
    （`snippet(fts_shared, 4, '【', '】', …)`），不换的话一行里会出现两层同一种括号，
    模型（与前端搜索页照着这个格式渲染的那一头）分不清哪个是行首的类别定位符。
    """
    kind = KIND_LABEL.get(hit["kind"], hit["kind"])
    snippet = " ".join(str(hit["body"] or "").split())[:SNIPPET_MAX]
    snippet = snippet.replace("【", "「").replace("】", "」")
    return f"【{kind} {hit['ref_id']}】{hit['heading'] or '（没有标题）'}：{snippet}"


def _write_page(conn: sqlite3.Connection, title: str, page_type: str, body_md: str,
                tags=None, sources=None, confidence: str = "",
                actor: str = AGENT_ACTOR, contested: bool = False, contradictions=None,
                analysis: AnalysisSpec | None = None, status: str = "draft", author_id: int | None = None) -> str:
    """建页。actor 由 @_tool 包装函数从 AgentContext 取（见 _actor_label）。

    confidence 空串=模型没给，按来源广度兜底（见 app/wiki/quality.py）；给了就用它。
    """
    if len((title or "").strip()) > TITLE_MAX:
        return f"{FAIL_TEXT}标题太长了（最长 {TITLE_MAX} 字），改短一点再来"
    extra = None
    if page_type in ("query", "comparison") or analysis is not None:
        sources, extra = prepare_analysis(conn, page_type, body_md, sources, analysis, status, author_id=author_id)
    level = normalize_confidence(confidence) or default_confidence(sources)
    # path=None = create-intent（裁定 R91）：目录与 slug 由 store 在 _CREATE_LOCK 内按
    # title + page_type 发；同标题的页已存在就返 failed「这一页已存在」，绝不静默编辑。
    res = store.write_page_sync(
        conn, path=None, title=title, page_type=page_type, body_md=body_md,
        actor=actor, action=None, note=NOTE_CREATE,
        tags=tags, sources=sources, confidence=level, contested=contested,
        contradictions=contradictions, extra=extra,
    )
    out = _result(conn, res, doing="建")
    if res.get("ok") and extra and extra.get("status") == "draft":
        out += "\n已保存草稿，未进入发布索引与检索；完善条件后由建设账号发布。"
    # 写成功就把"本页有没有断链"当场告诉模型：不要等它攒到收尾 lint 才发现，也不要在
    # 这里拒写（允许先建页后建目标页，见 _link_warnings 的说明）。
    return out + (_link_warnings(res["path"]) if res["ok"] else "")


def _edit_page(conn: sqlite3.Connection, path: str, body_md: str,
               tags=None, sources=None, base_commit: str = "",
               confidence: str | None = None,
               actor: str = AGENT_ACTOR, contested: bool | None = None, contradictions=None,
               analysis: AnalysisSpec | None = None, status: str | None = None) -> str:
    """改页。actor 由 @_tool 包装函数从 AgentContext 取（见 _actor_label）。

    2026-10-30：base_commit 改成**必填**。不带版本号的整页重写会静默覆盖别人刚改的
    内容（store 的乐观锁是 opt-in），而模型"忘了传"在长会话里太常见。这里在工具层
    直接拒绝，并把现行版本号回给它，省掉一轮 read。
    """
    path = (path or "").strip()
    page = store.get_page(conn, path) if path else None
    if page is None:
        return (
            f"{FAIL_TEXT}没有这一页（path={path!r}）。path 从 list_wiki / search / read 的"
            f"结果里拿，别自己拼；要建新页用 write_page(...)，path 由服务端发"
        )
    base = (base_commit or "").strip()
    if not base:
        head = (page.get("head") or "")[:12]
        return (
            f"{FAIL_TEXT}改页必须带 base_commit（这一页现行版本号是 {head}）："
            f"先 read 一次拿到它，再重试 edit_page。不带版本号会覆盖别人刚改的内容。"
        )
    # tags/sources/confidence 省略（None）时保留现行值；confidence 现行是空的
    # （修复前建的页）就按来源广度补一个默认值，不再把空字段带下去。
    final_sources = sources if sources is not None else page.get("sources")
    if confidence is None:
        level = (page.get("confidence") or "").strip().lower() or default_confidence(final_sources)
    else:
        level = normalize_confidence(confidence)
    extra = None
    if page["page_type"] in ("query", "comparison") or analysis is not None or page.get("metadata", {}).get("analysis"):
        final_sources, extra = prepare_analysis(conn, page["page_type"], body_md, final_sources, analysis,
            status or page.get("metadata", {}).get("status", "draft"), previous=page)
    if status is not None and extra is None:
        extra = dict(page.get("metadata", {}))
        if status == "published":
            pages = store.all_pages()
            resolve = store.link_resolver(pages)
            targets = {resolve(t) for t, _ in store.iter_wikilinks(body_md)} - {None, page["path"]}
            if len(targets) < 2 or any(resolve(t) is None for t, _ in store.iter_wikilinks(body_md)):
                raise ValueError("发布前需至少两个有效出链且没有断链")
            if any(pages[target].extra.get("status") == "draft" for target in targets):
                raise ValueError("关联页面尚为草稿；补齐整批关系后重试摄入任务，或改用已发布依据")
            if not final_sources or any(not canonical_url(source) and source_file(settings.wiki_dir, source) is None for source in final_sources):
                raise ValueError("发布前需要真实存在的来源")
        extra["status"] = status
        if status == "published":
            extra.pop("review_notes", None)
    res = store.write_page_sync(
        conn, path=page["path"], title=page["title"], page_type=page["page_type"],
        body_md=body_md,
        actor=actor, action=None, note=NOTE_EDIT,
        base_commit=base,
        tags=tags if tags is not None else page.get("tags"),
        sources=final_sources,
        confidence=level,
        contested=bool(page.get("contested")) if contested is None else contested,
        contradictions=page.get("contradictions") or [] if contradictions is None else contradictions,
        extra=extra,
    )
    out = _result(conn, res, doing="改")
    return out + (_link_warnings(res["path"]) if res["ok"] else "")


def _lint(conn: sqlite3.Connection, semantic: bool = False) -> str:
    """lint 报告摊成给模型看的文本（严重度计数 + 逐条 code/path/message）。

    **error 全列、且排在最前面**（本次修复）：生产上出现过一次真实的坏形状——
    188 条 finding 里只有 3 条 error，却因为按 path 排序 + 只列前 60 条，模型只看到
    了其中 1 条，误以为另外两处不存在。"先修 error"不能依赖模型翻页，只能由报告保证。
    截断只截 warn/info，且至少留 LINT_WARN_MIN 条（不拿几十条 error 把上下文挤爆）。
    """
    report = store.lint(conn)
    findings = report.get("findings") or []
    if not findings:
        return "结构检查通过；事实与矛盾尚未审阅，需读取相关页面和来源复核。" if semantic else "lint 通过：没有发现问题。"
    counts: dict[str, int] = {}
    for finding in findings:
        severity = str(finding.get("severity") or "info")
        counts[severity] = counts.get(severity, 0) + 1
    parts = "｜".join(f"{name} {count}" for name, count in counts.items())
    errors = [f for f in findings if str(f.get("severity")) == "error"]
    others = [f for f in findings if str(f.get("severity")) != "error"]
    room = max(LINT_MAX - len(errors), LINT_WARN_MIN)
    shown_others = others[:room]
    lines = [f"lint 发现 {len(findings)} 条（{parts}）："]
    if errors:
        lines.append(f"—— error {len(errors)} 条（全部列出，先修这些）——")
    for finding in errors + shown_others:
        lines.append(
            f"- [{finding.get('severity') or 'info'}] {finding.get('code') or '?'}"
            f"｜{finding.get('path') or '（全库）'}：{finding.get('message') or ''}"
        )
    hidden = len(others) - len(shown_others)
    if hidden > 0:
        lines.append(f"- （warn/info 还有 {hidden} 条没列，error 已全部列出）")
    if semantic:
        lines.append("语义复核候选（需 read 双方及来源后判断，不自动覆盖）：")
        lines.extend(json.dumps(item, ensure_ascii=False) for item in report.get("review_candidates", []))
    return "\n".join(lines)


def lint_errors(conn: sqlite3.Connection, *, paths: set[str] | None = None) -> list[dict]:
    """lint 里的 error 级 finding（runner 收尾修复轮用：只修 error，不碰 warn）。

    与 _lint 用同一个 store.lint 入口，不另写一套判据；runner 调它的时候这一轮
    已经跑完、没有别的工具在写，可以直接读。
    """
    from ..wiki.lint import lint as run_lint
    report = run_lint(conn, paths=paths)
    return [
        f for f in (report.get("findings") or [])
        if str(f.get("severity")) == "error"
    ]


def _link_warnings(path: str) -> str:
    """一页写成功之后，把"本页断链"当场告诉模型（不拒写）。

    为什么允许断链存在：服务端按标题发 slug（裁定 R91），模型写正文时**不可能**
    知道还没建的页会叫什么 slug；所以"先建页、后补链"是合法工序。但断链必须被
    看见——以前只有收尾 lint 会报，模型要么没轮次看、要么只看到被截断的一条。
    目标页还没建时，正确的写法是 `[[页面标题]]`（resolve_link 按标题解析），
    或者建完目标页后再 edit_page 换成 `[[slug]]`；这两句由这条提示带出去。
    """
    pages = store.all_pages()
    page = pages.get(path)
    if page is None:
        return ""
    broken = [link for link in store.wikilinks_of(page, pages) if link["path"] is None]
    if not broken:
        return ""
    sample = "、".join(f"[[{link['target']}]]" for link in broken[:5])
    tail = " 等" if len(broken) > 5 else ""
    return (
        f"\n注意：这一页有 {len(broken)} 个断链（{sample}{tail}）。"
        "目标页本轮才建、写正文时还不知道服务端会发什么 slug 的话，改写成 [[页面标题]]（按标题解析），"
        "或等拿到目标页真实 path 后用 edit_page 换成 [[slug]]；不要预写凭印象猜的 slug。"
    )



def _result(conn: sqlite3.Connection, res: dict, *, doing: str) -> str:
    """store 的返回 dict → 给模型的中文摘要。

    **先判 ok 再读 res["path"]**：失败路径上的 path 可能是"分配了但没写"的名字，交出去
    模型就会去 read/edit 一个磁盘上不存在的页。唯一的例外是同标题撞车那条，那时它是
    "撞上的那一页"，判据与 app/routers/api/wiki.py 的 _clash_path 同一个
    （failed[0]["note"] == store.ALREADY_EXISTS，见 _failed_text）。

    2026-10-30 起不再有句子级引注（对齐 Hermes/LLM-wiki 约定），成功串只报版本。
    """
    if not res["ok"]:
        return _failed_text(res)
    path = res["path"]
    # 工具自己 commit：runner 是后台任务，没有 get_conn 那道"请求结束才提交"的收尾，
    # 不 commit 的话 citation_checks 与 FTS 只活在这条连接里，SSE 尾读与
    # GET /api/wiki/{path}（另一条连接）看到的页每条引注都是"未核对"。
    conn.commit()
    head = (res["sha"] or "")[:12]
    if res["noop"]:
        detail = f"内容与现行版本逐字相同，没有产生新版本（现行版本 {head}）"
    else:
        detail = f"这一页{doing}好了，现行版本 {head}"
    # 提交已经落地，取版本号只是锦上添花：versions 要再跑一趟 git log，它炸了
    # （git 一时不可用、仓库被锁）绝不能把"写成功了"报成"没写成"（裁定 R-S2-34）——
    # 模型看到「没写成」会重写一遍，页就多出一版重复内容，hooks 那头 ok 也会跟着翻红。
    # 降级串仍以「已生效：」开头，ok 的判据不动。
    try:
        version = len(store.versions(path))
    except (store.StoreError, repo.GitError, OSError, subprocess.SubprocessError) as exc:
        logger.warning("提交成功了但版本号没取到（path=%s）：%r", path, exc)
        return (
            f"已生效：path={path}（版本号没取到）\n{detail}。"
            f"要再改这一页：edit_page(path=\"{path}\", base_commit=\"{head}\")"
        )
    return (
        f"已生效：path={path} v{version}\n{detail}。"
        f"要再改这一页：edit_page(path=\"{path}\", base_commit=\"{head}\")"
    )


def _failed_text(res: dict) -> str:
    """ok=False 的中文摘要。failed[] 每条是 {key, quote, note}；页级失败的 key 是空串。"""
    failed = res.get("failed") or []
    first = (failed[0].get("note") or "").strip() if failed else ""
    if first == store.ALREADY_EXISTS:
        # 只有这一条失败路径上的 res["path"] 是"撞上的那一页"（与 R-S2-5 同一个判据）
        clash = res.get("path")
        return (
            f"{FAIL_TEXT}{store.ALREADY_EXISTS}（同标题的那一页是 {clash}）。"
            f"要改它用 edit_page(path=\"{clash}\", …)，别另建一页"
        )
    lines = [f"{FAIL_TEXT}{first or '这一页没写成，原因不明'}"]
    return "\n".join(lines)


# --- 九个 @function_tool（docstring 就是给模型看的说明书） ---------------------


@_tool("读不到：")
def read(ctx: RunContextWrapper, path: str, offset: int = 0, limit: int = READ_MAX) -> str:
    """读一页 wiki 正文、一份 raw 材料、特殊文件，或一份上传材料的解析文本。

    path 的形状（都是 vault 相对路径）：
    - `entities/{slug}.md` / `concepts/…` / `comparisons/…` / `queries/…` / `summaries/…`：
      wiki 页面，path 从 list_wiki、search 或 read 的结果里拿；
    - `raw/uploads/{编号}-{文件名}.md`：raw 层里的材料原文（frontmatter + 全文）；
    - `index.md` / `SCHEMA.md` / `log.md`：自动维护的索引、领域约定与行动日志；
    - `upload/{编号}`：旧入口，读上传材料的解析文本（归档/引用所依据的解析文本）。
    读一页会拿到标题、类型、tags、sources、现行版本号、正文、质量信号、互链与最近版本记录。
    可传 offset / limit 分页读取后文；争议与发布状态会随页面返回。

    Args:
        path: 要读的路径，例如 entities/baoming.md、raw/uploads/3-notes.md、index.md，或 upload/3。
    """
    return _guard("读不到：", _read, _conn(ctx), path, offset, limit)


@_tool("读不到：")
def list_wiki(ctx: RunContextWrapper, offset: int = 0, limit: int = LIST_MAX) -> str:
    """列出现在有些什么：全部 wiki 页（path、类型、tags、摘要）与全部 raw 材料。

    不知道要搜什么、或想知道知识库已经写到哪儿了，就先调这一个。
    页的 path 与材料的 upload 编号都在里面，read/edit_page/search 要用。
    """
    return _guard("读不到：", _list_wiki, _conn(ctx), offset, limit)


@_tool("读不到：")
def search(ctx: RunContextWrapper, query: str,
           scope: Literal["wiki", "raw", "all"] = "all") -> str:
    """在共享语料里搜：wiki 已生效的正文、raw 材料、会话里的人话。

    先定位相关知识，再 read 完整上下文；片段不能替代证据核对。
    一行一条 `【类别 定位符】标题：片段`：页面定位符是 path，材料编号用于 read(upload/编号)。
    用户明确要求摄入文字时，消息编号可用于 ingest_text(message_id)。
    写分析页时继承依据页的 sources 和版本，不编造出处。

    Args:
        query: 要搜的话，最好是原话里的一串字。
        scope: 搜哪儿。wiki=只搜 wiki 页，raw=只搜原始材料（会话人话与上传材料），all=三样都搜。
    """
    return _guard("读不到：", _search, _conn(ctx), query, scope, _message_owner_id(ctx))


@_tool(FAIL_TEXT)
def write_page(ctx: RunContextWrapper, title: str,
               page_type: Literal["entity", "concept", "comparison", "query", "summary"],
               body_md: str,
               tags: list[str] | None = None,
               sources: list[str] | None = None,
               confidence: str = "", contested: bool = False,
               contradictions: list[str] | None = None, analysis: AnalysisSpec | None = None,
               status: Literal["draft", "published"] = "draft") -> str:
    """建一页新的知识库页面。path（目录 + slug）由服务端按 page_type 发，不要自己编。

    正文是普通 markdown：页面互链用 [[slug]]，来源写在 frontmatter 的 sources 里
    （工具参数 sources），分析页使用 analysis 的段落来源映射；不要求旧式逐句核验。
    同标题的页已经存在时也会退回，那时改用 edit_page 去改那一页。
    正文至少挂 2 个 [[wikilink]] 把新页接进网络。

    没有建设权限的账号只允许新建沉淀型页面（page_type 为 query / comparison /
    summary 的分析草稿）；entity / concept 和发布会被拒绝；只能编辑自己的草稿，不能删除页面。

    Args:
        title: 页标题，最长 120 字。
        page_type: 页的类型，决定目录。entity=实体，concept=概念，comparison=对比，
            query=查询，summary=摘要。
        body_md: 正文（markdown），页面互链用 [[slug]]；不要写句子级引注。
        tags: 标签清单，只能取 SCHEMA.md 里 tag taxonomy 的值；可省略。
        sources: 来源清单，写 raw 路径或 URL（例如 raw/articles/x.md）；可省略。
        confidence: 这一页的证据强度 high/medium/low；可省略。省略时服务端按来源广度
            给默认值（≥2 份独立材料 high、1 份 medium、没写 sources 就 low）。拿不准的页
            自己判断后传，别把空值留给人。
    """
    if getattr(ctx.context, "progress", {}).get("repair_paths") is not None:
        return f"{FAIL_TEXT}自动修复不新建历史断链目标；请保留待审，不能凭空补内容"
    if not _can_build(ctx) and (page_type not in READONLY_WRITE_TYPES or status == "published"):
        return _denied_text("建这一类页面")
    if not _can_build(ctx) and page_type == "summary" and analysis is None:
        analysis = AnalysisSpec()
    return _guard(FAIL_TEXT, _write_page, _conn(ctx), title, page_type, body_md,
                  tags, sources, confidence, _actor_label(ctx), contested, contradictions, analysis, status,
                  getattr(ctx.context, "user_id", None))


@_tool(FAIL_TEXT)
def edit_page(ctx: RunContextWrapper, path: str, body_md: str,
              tags: list[str] | None = None,
              sources: list[str] | None = None,
              base_commit: str = "",
              confidence: str | None = None, contested: bool | None = None,
              contradictions: list[str] | None = None, analysis: AnalysisSpec | None = None,
              status: Literal["draft", "published"] | None = None) -> str:
    """改一页已有的知识库页面：整页正文换掉，可顺带改标签/来源。

    path 从 list_wiki、search 或 read 的结果里拿（例如 entities/baoming.md）；
    标题与类型沿用这一页现行的，这里不改。body_md 是**整页**的新正文，不是补丁：
    先把这一页 read 出来，改完再整页交回来（不要加句子级引注/脚注）。
    tags / sources 省略时保留原值。base_commit 传 read 给出的现行版本号，
    有人先改过就会退回，那时重新 read 一遍再改；留空会被拒绝（工具会拒绝空版本号）。

    使用账号只允许编辑自己的分析草稿；发布和修改已发布页需要建设权限。

    Args:
        path: 要改哪一页，例如 entities/baoming.md。
        body_md: 这一页整页的新正文（markdown），不要写句子级引注。
        tags: 新的标签清单；省略=保留原标签。
        sources: 新的来源清单（raw 路径）；省略=保留原来源。
        base_commit: 必填，read 给出的现行版本号。
        confidence: 改这一页的证据强度 high/medium/low；省略=保留现行值（现行是空的就
            按来源广度补默认值）。证据强度有了变化（补了材料、发现矛盾）时才改它。
    """
    repair_paths = getattr(ctx.context, "progress", {}).get("repair_paths")
    if repair_paths is not None and store.canonical_path(path) not in repair_paths:
        return f"{FAIL_TEXT}自动修复仅限本轮改动及受影响页面；其他问题已保留待审"
    if not _can_build(ctx):
        page = store.get_page(_conn(ctx), path)
        metadata = (page or {}).get("metadata", {})
        uid = getattr(ctx.context, "user_id", None)
        if not (page and page["page_type"] in READONLY_WRITE_TYPES
                and metadata.get("status") == "draft" and uid is not None
                and metadata.get("author_id") == str(uid) and status != "published"):
            return _denied_text("修改已有页面")
    return _guard(FAIL_TEXT, _edit_page, _conn(ctx), path, body_md,
                  tags, sources, base_commit, confidence, _actor_label(ctx), contested, contradictions, analysis, status)


def _delete_page(conn: sqlite3.Connection, path: str, base_commit: str = "",
                 actor: str = AGENT_ACTOR) -> str:
    """删一页（CRUD 的删）：只删现行文件，历史仍在 git；返回中文摘要。

    actor 由 @_tool 包装函数从 AgentContext 取（见 _actor_label）。
    2026-10-30：base_commit 必填（理由同 edit_page）：不给版本号的删页会删掉别人
    刚改过的那一页。
    """
    page = store.get_page(conn, path)
    if page is None:
        return f"{FAIL_TEXT}没有这一页（path={path!r}）"
    base = (base_commit or "").strip()
    if not base:
        head = (page.get("head") or "")[:12]
        return (
            f"{FAIL_TEXT}删页必须带 base_commit（这一页现行版本号是 {head}）："
            f"先 read 一次拿到它，再重试 delete_page。"
        )
    res = store.delete_page_sync(
        conn, path, actor=actor, note=NOTE_DELETE, base_commit=base,
    )
    if not res["ok"]:
        return _failed_text(res)
    # 工具自己 commit：与写页同一个理由（runner 是后台任务，没有请求收尾提交）。
    conn.commit()
    head = (res["sha"] or "")[:12]
    return (
        f"已删除：path={res['path']}（提交 {head}）\n"
        "历史仍留在 git 里，需要的话可以用 revert 恢复；要重建同一主题再 write_page。"
    )


@_tool(FAIL_TEXT)
def delete_page(ctx: RunContextWrapper, path: str, base_commit: str = "") -> str:
    """删除一页知识库页面。**只在用户明确说"删掉/移除这一页"时才用**，含糊的不要删。

    删的是现行文件，历史版本仍留在 git 里（可回看/回滚）；目录 index 与行动日志会
    在同一次提交里更新。path 从 list_wiki / search / read 的结果里拿。
    base_commit 必填：传 read 给出的现行版本号（有人先改过就退回，不传直接拒）。
    没有建设权限的账号不能用这个工具（删页算修改知识库），会得到一句人话拒绝。

    Args:
        path: 要删哪一页，例如 entities/baoming.md。
        base_commit: 必填，read 给出的现行版本号。
    """
    if getattr(ctx.context, "progress", {}).get("repair_paths") is not None:
        return f"{FAIL_TEXT}自动修复不删除页面；无法确认的问题保留待审"
    if not _can_build(ctx):
        return _denied_text("删除页面")
    return _guard(FAIL_TEXT, _delete_page, _conn(ctx), path, base_commit, _actor_label(ctx))


def _actor_user_id(conn: sqlite3.Connection, actor_name: str) -> int:
    """跑这一轮的 agent 是谁 → 一个 users.id（ingest_url 要往 upload_files 里填 user_id）。

    先按 AgentContext.actor_name 精确查（人跟 agent 对话时 actor_name 是人的姓名）；
    查不到（脚本/演示账号、姓名被改过）退到最小 user id——任务是"能摄入"，不该因为
    对不上具体的人就把整个工具拒掉。
    """
    name = (actor_name or "").strip()
    if name:
        row = conn.execute(
            "SELECT id FROM users WHERE name = ? ORDER BY id LIMIT 1", (name,)
        ).fetchone()
        if row is not None:
            return int(row["id"])
    row = conn.execute("SELECT MIN(id) AS id FROM users").fetchone()
    if row is None or row["id"] is None:
        raise ValueError("库里还没有用户，没法记录这份网页材料")
    return int(row["id"])


def _ingest_url(conn: sqlite3.Connection, url: str, actor_name: str, user_id: int | None = None) -> str:
    """把网页抓进知识库，返回给模型的中文摘要（含 upload_id 与 raw 路径）。

    raw 归档那次提交的 actor 记触发这一轮的人（2026-10-30 裁定）：写死 AGENT_ACTOR
    会让"某人摄入的网页"在日志里变成智能体干的，那串连锁动作就断在第一步了。
    """
    result = web_ingest.ingest_url(
        conn, url, user_id if user_id is not None else _actor_user_id(conn, actor_name), actor_name
    )
    conn.commit()
    raw_path = result.get("raw_path") or "（raw 没生成，可以用 search/list_wiki 看 upload 编号）"
    return (
        f"已摄入：{result['title']}\n"
        f"upload_id：{result['upload_id']}\n"
        f"链接：{result['url']}\n"
        f"raw 归档：{raw_path}\n"
        f"正文字数：{result['chars']}\n"
        "下一步：先 read(raw 路径) 读原文（或 read(\"upload/{id}\")），再 search 相关页，"
        "按工作流写/改页，sources 使用真实 raw 路径。"
    )


@_tool(FAIL_TEXT)
def ingest_url(ctx: RunContextWrapper, url: str) -> str:
    """把用户给的网页链接抓进知识库：抓正文 → 存成一份「材料」→ 归进 raw 层。

    用户发来一条网页链接、说"看看这个/把它整理进知识库"时先调这个工具，再继续
    read / search / write_page。抓取走 DeepSeek 内置 Fetch API（服务端替你抓，返回
    Markdown 正文）；只支持 http/https 链接，抓不到/被站点拒绝/没有正文会给中文原因。
    抓下来的正文可以用
    `read("raw/uploads/…")` 或 `read("upload/{upload_id}")` 逐字读，引注来源用
    source_kind=upload + 返回的 upload_id。
    没有建设权限的账号不能用这个工具（摄入材料算修改知识库），会得到一句人话拒绝。

    Args:
        url: 用户给的网页链接（http/https）。
    """
    if not _can_build(ctx):
        return _denied_text("摄入网页材料")
    return _guard(FAIL_TEXT, _ingest_url, _conn(ctx), url, _actor_label(ctx), getattr(ctx.context, "user_id", None))


def _ingest_document(conn: sqlite3.Connection, upload_id: int, actor_name: str, user_id: int | None = None) -> str:
    """起一个长文摄入后台任务，返回给模型的中文摘要（含 job 号与规模）。"""
    result = document_ingest.start_job(
        conn, upload_id, user_id if user_id is not None else _actor_user_id(conn, actor_name),
    )
    job_id = result["job_id"]
    return (
        f"已开始长文摄入：job #{result['job_id']}（upload/{upload_id}，"
        f"共 {result['total_chars']} 字，约 {result['chunks_total']} 块）。"
        "后台按主题形成候选、合并既有知识并保留待审项。"
        f"这个工具只负责起任务，不要等它跑完——稍后 read('jobs/{job_id}') 看质量报告，read('jobs/{job_id}/candidates') 看待审候选。"
    )


@_tool(FAIL_TEXT)
def ingest_document(ctx: RunContextWrapper, upload_id: int) -> str:
    """把一整本书/长文档（几十页以上的已上传材料）交给后台分块摄入。

    什么时候用：用户说"把这本书/这份长材料整理进知识库"、"把整本 PDF 摄入"时用它，
    **不要**自己一页一页 write_page 手写（几十页会把上下文烧光、也容易越写越偏）。
    前置条件：这份材料必须已经解析成功（list_wiki 里 parse_status='done'，有 raw 与
    upload 编号）。起完任务就返回 job 号，摄入在后台串行跑；
    别在同一个回合里反复起同一个任务，也别等它——先做别的，稍后用 search/lint 抽查。
    候选会按主题合并并检查结构；不完整内容保留草稿，结构通过不代表事实已被证明。
    没有建设权限的账号不能用这个工具（长文摄入算修改知识库），会得到一句人话拒绝。

    Args:
        upload_id: 要摄入的材料编号（list_wiki / /api/uploads 里看到的 upload/{编号}）。
    """
    if not _can_build(ctx):
        return _denied_text("摄入长文档材料")
    return _guard(FAIL_TEXT, _ingest_document, _conn(ctx), upload_id, _actor_label(ctx), getattr(ctx.context, "user_id", None))


def _web_search(query: str, count: int) -> str:
    """跑一次 DeepSeek 内置搜索，摊成模型能扫的一行一条。"""
    results = web_search_service.search_web(query, count=count)
    if not results:
        return f"没搜到与「{query}」相关的网页。换个说法再搜，或直接给一个 URL 让我抓。"
    lines = [f"搜到 {len(results)} 个网页（先挑 URL 摄入，不要直接引用下面的摘要当事实）："]
    for index, item in enumerate(results, start=1):
        meta = "｜".join(part for part in (item.get("site_name"), item.get("date")) if part)
        head = item.get("title") or "（无标题）"
        if meta:
            head += f"｜{meta}"
        body = item.get("summary") or item.get("snippet") or "（没有摘要）"
        # 真实搜索的 summary 可能是一大段正文（甚至整页 markdown）：每条只给一截，
        # 否则一次搜索就把上下文窗口顶爆（SNIPPET_MAX 与站内搜索同一口径）。
        body = " ".join(str(body).split())[:SNIPPET_MAX]
        lines.append(f"{index}. {head}\n   {item.get('url') or '（没有 URL）'}\n   {body}")
    lines.append(
        "下一步：挑最相关的 URL 调 ingest_url 抓进 raw，再从 raw 正文里逐字抄引注；"
        "搜索结果摘要本身不能当引注来源。"
    )
    return "\n".join(lines)


@_tool("读不到：")
def web_search(ctx: RunContextWrapper, query: str, count: int = 5) -> str:
    """用 DeepSeek 内置搜索找网页来源：返回 标题｜URL｜日期/站点｜摘要。

    要回答/写页的内容在站内资料里没有时先用它找来源。**搜索结果只是线索**：
    挑最相关的 URL 调 `ingest_url` 把正文抓进知识库，再从 raw 正文里逐字抄引注；
    不要直接引用搜索摘要当事实或引文（引注必须来自已摄入的 raw 正文）。
    没有建设权限的账号不能用这个工具（搜到的网页最终要摄入，属于建设链路），
    会得到一句人话拒绝；站内 search 与 wiki 阅读不受影响。

    Args:
        query: 搜索词，1–128 字。
        count: 要几条结果，1–50，默认 5。
    """
    if not _can_build(ctx):
        return _denied_text("搜索网页来源")
    return _guard("读不到：", _web_search, query, count)


@_tool("读不到：")
def lint(ctx: RunContextWrapper, semantic: bool = False) -> str:
    """给整个 vault 做一次体检：断链、孤儿页、index 缺失、frontmatter、未知 tag、
    raw 来源缺失、过长页、矛盾标记等。写完页之后调它自检，有问题当场修掉。

    只读，不会改动任何文件。返回报告文本：先按严重度报数，再逐条列 code/path/message。
    """
    return _guard("读不到：", _lint, _conn(ctx), semantic)


@_tool(FAIL_TEXT)
def ingest_text(ctx: RunContextWrapper, message_id: int | None = None) -> str:
    """用户明确要求摄入粘贴文字时，将真实人类消息归档到 raw。

    message_id 省略表示本会话最新的人类消息；可传检索得到的可访问消息编号。
    不用于普通问答，不接受模型生成的文本冒充来源。归档后 read raw，再整合已有页。
    """
    if not _can_build(ctx):
        return _denied_text("摄入文字材料")
    from ..services.text_ingest import ingest_message
    uid = getattr(ctx.context, "user_id", None)
    try:
        result = ingest_message(_conn(ctx), ctx.context.session_id,
            uid if uid is not None else _actor_user_id(_conn(ctx), _actor_label(ctx)),
            _actor_label(ctx), message_id, _message_owner_id(ctx))
        return "已摄入真实消息：" + json.dumps(result, ensure_ascii=False)
    except (ValueError, OSError, sqlite3.Error) as exc:
        return f"{FAIL_TEXT}{exc}"


# runner 装配用的那一份（Task 4：Agent(tools=WIKI_TOOLS)）。
_CORE_TOOLS = [
    read, list_wiki, search, write_page, edit_page, delete_page, lint,
    ingest_url, ingest_document, web_search, ingest_text,
]


def _plugin_tools() -> list:
    """插件贡献的 agent 工具；插件未启用时就是空列表。"""
    from ..plugins import get_plugins

    out: list = []
    for info in get_plugins():
        out.extend(info.tools())
    return out


WIKI_TOOLS = _CORE_TOOLS + _plugin_tools()
_names = [tool.name for tool in WIKI_TOOLS]
_duplicates = sorted({name for name in _names if _names.count(name) > 1})
if _duplicates:
    raise RuntimeError(f"插件工具与核心工具重名：{'、'.join(_duplicates)}")

# hooks 的白名单第三道防线（Task 3）与上面同一个来源，不各写一份字面量。
TOOL_NAMES = tuple(_names)
