"""wiki 接口：vault 目录、特殊文件（index/SCHEMA/log/lint）、单页、建页、编辑、版本、diff、回滚、全站改动日志。

这一层很薄：S5 起 wiki 是物理 vault（`data/wiki/`，页面 path = `{dir}/{slug}.md`），
读写路径全在 app/wiki/store.py（校验、乐观锁、恒等闸、index/log 自动维护、多 path 提交）。
路由只做四件事——收 JSON、把 actor 换成登录者姓名、把 store 的异常/failed 翻成状态码、
把 store 的 dict 原样透传（特殊文件与页面都已经是前端要的形状）。

裁定 R-S4-20（整站 wiki 只读）：三条人改写路由（建页/编辑/回滚）挂着 _readonly_gate，
settings.wiki_readonly 为真时在收参之前就 403「wiki 现在只能查看，内容由智能体维护」。
只读闸只拦人的这一面，智能体工具直接调 store（write_page_sync / write_page）照常写。
2026-10-30 起同一道依赖还先判成员的建设权限（users.can_build_wiki，管理员恒有）：
只有使用权限的成员建/改/滚一律 403「当前账号只能使用 wiki，不能修改知识库」。

路径：URL 里的 page path 不带 `.md`（`/wiki/entities/foo`），API 同时接受带/不带，
返回 canonical `path`（带 `.md`）。特殊文件路由（index/schema/log/lint）必须注册在
`{path:path}` 之前——FastAPI 按注册顺序匹配，反过来的话 `/api/wiki/index` 会被当成
path="index" 去查页面（404）。

错误信封（Task 9 备注 N5）：/api/* 一律 {"error": 人话}（app/main.py 的 handler 出），
只有两处允许在 "error" 旁边多带键：
- 409 乐观锁冲突：{"error": "有人先改了这一页", "diff": <text>}；
- 400 写不成（引注校验不过、恒等闸拒收等）：{"error": <人话>, "failed": [...]}，
  edit/create/revert 三条写路由共用同一个 _write_error 形状（裁定 R79）。
  failed[] 每条是 {key, quote, note}：页级失败没有"哪一条引注"可指，key/quote 都是 ""。
  **只在建页撞车这一种 400 上再多带一个 "path"**（撞上的那一页，与 R-S2-5 同精神）：
  {"error": "这一页已存在", "failed": [...], "path": <撞上的页>}。别的 400 不带这个键。
这两处直接 return JSONResponse，不走 HTTPException（detail 会被 handler str() 掉）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...config import settings
from ...deps import Conn, RequiredAdmin, RequiredUser
from ...services import access
from ...services import chat as chat_service
from ...services import git_sync
from ...wiki import repo, store

router = APIRouter()

# 裁定 R-S4-20：整站 wiki 只读。人类面的三条写路由（建页/编辑/回滚）在只读期间一律
# 403 这句话，写只由智能体工具直接调 store 完成——只读是**人的那一面**的闸，不是数据层
# 的闸（所以 store.write_page* 不受影响，agent 照常写）。
READONLY_TEXT = "wiki 现在只能查看，内容由智能体维护"

# 成员能力闸（2026-10-30）：只有「使用权限」的成员不能建/改/滚 wiki 页。
# 与只读闸挂在同一条依赖上，两条都是"人的这一面"的闸；管理员恒有建设权限。
NO_BUILD_TEXT = "当前账号只能使用 wiki，不能修改知识库"


def _readonly_gate(user=RequiredUser) -> None:
    """只读闸 + 建设权限闸：挂在三条人改路由的 route-level dependencies 上。

    两道闸：
    1. 没有建设权限（users.can_build_wiki=0 且不是管理员）→ 403「当前账号只能使用
       wiki，不能修改知识库」。这是账号能力，与全站开关无关。
    2. settings.wiki_readonly 为真 → 403 READONLY_TEXT（全站只读，智能体维护）。
    先判能力再判全站开关：能力不足是这个人本身就做不了，只读是暂时状态，前者更该先说。

    为什么用依赖而不是在函数体里 if：依赖在 FastAPI 解 body_params 与 query 校验之前跑
    （solve_dependencies 先解子依赖，再解 path/query，最后才是 request_body_to_args），
    所以被挡时连 422/400/409 都不会冒出来——字段类型不对、标题超长、base_commit 过期，
    一律先得到 403。写在函数体里的话，Pydantic 的 422 会抢在它前面。（唯一例外是请求体
    根本解不成 JSON：那一步在 FastAPI 更外层就发生了，422 先于依赖，管不到也不该管。）

    依赖里先取 RequiredUser：没登录还是 401（别把权限状态泄给匿名者），登录了才判。
    route 上原本就有的 RequiredUser 与这里共用依赖缓存，不会多查一次会话。
    """
    if not access.can_build_wiki(user):
        raise HTTPException(403, NO_BUILD_TEXT)
    if settings.wiki_readonly:
        raise HTTPException(403, READONLY_TEXT)


# 裁定 R86：入参上限。标题 120 字（md 的一行、前端的一行都放得下）、改动说明 200 字
# （它整个拼进提交主题的 note 字段，越长 git log 越没法看）。超了是 400 人话，
# 不截断——写作者该自己决定砍哪段，不该由服务端悄悄丢字。
TITLE_MAX = 120
NOTE_MAX = 200


class CreateBody(BaseModel):
    """POST /api/wiki 的入参：path 由服务端按 title + page_type 发，不收。"""

    title: str
    page_type: str
    body_md: str
    tags: list[str] = []
    sources: list[str] = []
    confidence: str = ""
    contested: bool = False
    contradictions: list[str] = []


class EditBody(BaseModel):
    """POST /api/wiki/{path}/edit 的入参：title/type 不动，所以不收。

    tags/sources 省略（None）时保留这一页现行的值；
    confidence/contested/contradictions 沿用现行值（这条路由是开发机/测试用的人改口）。
    """

    body_md: str
    tags: list[str] | None = None
    sources: list[str] | None = None
    base_commit: str | None = None
    note: str = ""


def _check_caps(title: str = "", note: str = "") -> None:
    """裁定 R86 的那道闸：超上限 400 人话，不截断。"""
    if len(title) > TITLE_MAX:
        raise HTTPException(400, f"标题太长了（最长 {TITLE_MAX} 字），改短一点再来")
    if len(note) > NOTE_MAX:
        raise HTTPException(400, f"改动说明太长了（最长 {NOTE_MAX} 字），改短一点再来")


def _write_error(res: dict, *, path: str | None = None) -> JSONResponse:
    """store 返 ok=False 时的 400 信封：failed 原样带上（N5 允许的例外之一）。

    path 给了才进信封（只在建页撞车时给，见 _clash_path）：不给时形状与 edit/revert 的
    400 逐字相同，前端不用为路由记两种键集。
    """
    note = res["failed"][0]["note"] if res["failed"] else "这一页没写成"
    content: dict = {"error": note, "failed": res["failed"]}
    if path is not None:
        content["path"] = path
    return JSONResponse(status_code=400, content=content)


def _clash_path(res: dict) -> str | None:
    """create-intent 撞车时返"撞上的那一页"的 path，其余失败路径一律 None。

    判据是 note 等于 store.ALREADY_EXISTS（R17 那句话是常量，cli seed 也认它），不是
    "res 里有 path 就带上"：store 的返回 dict 各条路径都带 path，而失败路径上它是
    **分配了但没落盘**的名字，交出去前端会去 GET 一个 404 的页。
    """
    failed = res.get("failed") or []
    if failed and failed[0].get("note") == store.ALREADY_EXISTS:
        return res.get("path")
    return None


# 特殊文件：URL 片段 → 404 时的人话。read_special 返回 {path, body_md, updated_at}。
_SPECIALS = {
    "index": (store.INDEX_FILE, "索引还没生成"),
    "schema": (store.SCHEMA_FILE, "还没有 SCHEMA.md"),
    "log": (store.LOG_FILE, "日志还没生成"),
}

SPECIAL_ALIASES = {
    "index": "index",
    "index.md": "index",
    "schema": "schema",
    "schema.md": "schema",
    "log": "log",
    "log.md": "log",
}


def _special(name: str) -> dict:
    """读 index.md / SCHEMA.md / log.md：store 给的就是前端要的 {path, body_md, updated_at}。"""
    got = store.read_special(name)
    if got is None:
        _, message = _SPECIALS[name]
        raise HTTPException(404, message)
    return got


# --- 读侧 --------------------------------------------------------------------


@router.get("/api/wiki")
def pages(conn: sqlite3.Connection = Conn, user=RequiredUser):
    """目录：{pages: [{path,slug,title,page_type,tags,summary,created_at,updated_at,actor}], count}。"""
    try:
        items = store.list_pages(conn)
    except store.StoreError as exc:
        raise HTTPException(500, str(exc))
    return {"pages": items, "count": len(items)}


# 特殊文件必须先注册：它们也匹配下面的 `{path:path}`，注册反了会被当成页面查。
@router.get("/api/wiki/index")
def index_file(conn: sqlite3.Connection = Conn, user=RequiredUser):
    """自动生成的索引正文（index.md）。"""
    return _special("index")


@router.post("/api/wiki/sync")
def sync_wiki(conn: sqlite3.Connection = Conn, user=RequiredAdmin):
    """管理员手动同步：从远端 pull 最新 wiki，并重建 index/FTS。

    页面读取本身不会触发 Git 网络操作；服务器只在智能体回合开始/结束时自动 pull。
    这个入口供管理员在本地 push 后立即把页面拉到服务器。
    """
    report = git_sync.pull(conn, reason="manual")
    if not report.get("ok"):
        raise HTTPException(409, report.get("error") or "wiki 同步失败")
    return report


@router.get("/api/wiki/schema")
def schema_file(conn: sqlite3.Connection = Conn, user=RequiredUser):
    """SCHEMA.md 正文（领域约定 / tag taxonomy，只读）。"""
    return _special("schema")


@router.get("/api/wiki/log")
def log_file(conn: sqlite3.Connection = Conn, user=RequiredAdmin):
    """log.md 原文（追加式行动日志）。管理员专属；全站 git log 在 /api/log。

    与 /api/log 同一个可见性口径（2026-10-30 裁定）：log.md 就是提交时间线的正文版，
    普通成员看不到（403 + 人话），匿名仍 401。
    """
    return _special("log")


@router.get("/api/wiki/source")
def source_text(path: str, user=RequiredUser):
    if path == "_meta/topic-map.md":
        target = settings.wiki_dir / path
        body = target.read_text(encoding="utf-8") if target.is_file() else None
    else:
        body = store.read_raw(path)
    if body is None:
        raise HTTPException(404, "没有这份来源正文")
    from ...wiki.provenance import split_raw
    metadata, content = split_raw(body)
    return {"path": path, "body_md": content, "metadata": metadata}


@router.get("/api/wiki/lint")
def lint(conn: sqlite3.Connection = Conn, user=RequiredUser):
    """vault lint：{findings: [{code,severity,path,message}], summary: {...}}。只读。"""
    try:
        return store.lint(conn)
    except store.StoreError as exc:
        raise HTTPException(500, str(exc))


@router.get("/api/wiki/{path:path}/versions")
def versions(path: str, conn: sqlite3.Connection = Conn, user=RequiredUser):
    """这一页的提交记录（新到旧）。path 可带/不带 .md、也收旧裸 slug。

    页面不存在给 404：之前 store.versions 对未知 path 返 []，前端会把"没有这一页"
    渲染成一页空历史（P2-10③）。
    """
    try:
        if store.get_page(conn, path) is None:
            raise HTTPException(404, "没有这一页")
        return store.versions(path)
    except store.StoreError as exc:
        raise HTTPException(500, str(exc))


@router.get("/api/wiki/{path:path}/diff")
def diff(path: str, a: str, b: str, conn: sqlite3.Connection = Conn, user=RequiredUser):
    """两个版本之间这一页的 unified diff。"""
    # 修订号形状闸（repo.REV_RE）在进门处先过一遍：a/b 是 HTTP 查询参数，什么值都
    # 进得来，形状不对是 400 人话。不先拦的话 store.page_diff 会把 repo.GitError
    # 包成 StoreError，一个客户端错误就顶成 500 了。
    for rev in (a, b):
        if not repo.REV_RE.fullmatch(rev or ""):
            raise HTTPException(400, f"不是合法的版本号：{rev!r}")
    try:
        if store.get_page(conn, path) is None:
            raise HTTPException(404, "没有这一页")
        return {"diff": store.page_diff(path, a, b)}
    except store.StoreError as exc:
        raise HTTPException(500, str(exc))
    except ValueError as exc:
        # path 形状闸（store.canonical_path → page_diff 抛 ValueError）。
        raise HTTPException(400, str(exc))


@router.get("/api/wiki/{path:path}")
def page(path: str, conn: sqlite3.Connection = Conn, user=RequiredUser):
    """一页的全文：store.get_page 的字段透传（含引注来源、wikilinks、backlinks）。

    引注来源脱敏（2026-10-30）：对话可见范围是 `own` 时，来源会话当前不可见的 message
    引注，其 who/session_title/session_id 被抹成 None 并标 session_visible=False；逐字
    quote 与 wiki 正文不动（知识库是共享的，限制的是会话本身）。上传材料来源不受影响。
    """
    try:
        got = store.get_page(conn, path)
    except store.StoreError as exc:
        raise HTTPException(500, str(exc))
    if got is None:
        raise HTTPException(404, "没有这一页")
    return got


# --- 写侧（只读模式下三条一律 403，见 _readonly_gate） -----------------------


@router.post("/api/wiki", dependencies=[Depends(_readonly_gate)])
async def create(body: CreateBody, conn: sqlite3.Connection = Conn, user=RequiredUser):
    """建一页新页：path=None，目录与 slug 由 store 在创建锁内按 title + page_type 发。

    同标题的页已存在 → 400「这一页已存在」，信封里多带一个 "path" = 撞上的那一页。
    正文直接落盘（来源写在 frontmatter 的 sources 里，不再有句子级引注）。
    """
    _check_caps(body.title)
    try:
        res = await store.write_page(
            conn, path=None, title=body.title, page_type=body.page_type,
            body_md=body.body_md,
            actor=user["name"], action=None, note="人建",
            tags=body.tags, sources=body.sources, confidence=body.confidence,
            contested=body.contested, contradictions=body.contradictions,
        )
    except store.ConflictError as exc:  # 建页不带 base_commit，理论到不了；形状与 edit 对齐
        return JSONResponse(
            status_code=409, content={"error": exc.reason, "diff": exc.diff_text}
        )
    except store.StoreError as exc:
        raise HTTPException(500, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not res["ok"]:
        return _write_error(res, path=_clash_path(res))
    return res


@router.post("/api/wiki/{path:path}/edit", dependencies=[Depends(_readonly_gate)])
async def edit(path: str, body: EditBody, conn: sqlite3.Connection = Conn, user=RequiredUser):
    """改一页：title/type 不动，只换正文（可选换引注/标签/来源），乐观锁 base_commit。

    只读期间整条路由先被 _readonly_gate 挡成 403（裁定 R-S4-20）：不解析请求体、
    不看 base_commit，409/400 都不会先冒出来。这一页不存在就是 404——建页走 POST /api/wiki。
    """
    _check_caps(note=body.note)
    try:
        current = store.get_page(conn, path)
    except store.StoreError as exc:
        raise HTTPException(500, str(exc))
    if current is None:
        raise HTTPException(404, "没有这一页（建页用 POST /api/wiki，path 由服务端发）")
    tags = body.tags if body.tags is not None else current["tags"]
    sources = body.sources if body.sources is not None else current["sources"]
    try:
        res = await store.write_page(
            conn, path=current["path"], title=current["title"], page_type=current["page_type"],
            body_md=body.body_md,
            actor=user["name"], action=None, note=body.note or "人改",
            base_commit=body.base_commit,
            tags=tags, sources=sources,
            confidence=current.get("confidence") or "",
            contested=bool(current.get("contested")),
            contradictions=current.get("contradictions") or [],
        )
    except store.ConflictError as exc:
        # N5：409 带 diff，前端把"别人先改的那段"摊开给正在编辑的人。
        return JSONResponse(
            status_code=409, content={"error": exc.reason, "diff": exc.diff_text}
        )
    except store.StoreError as exc:
        raise HTTPException(500, str(exc))
    except ValueError as exc:
        # path 形状不合法（store._canonical_write_path）一类的程序错/攻击，翻 400 人话。
        raise HTTPException(400, str(exc))
    if not res["ok"]:
        return _write_error(res)
    return res


@router.post("/api/wiki/{path:path}/revert", dependencies=[Depends(_readonly_gate)])
async def revert(path: str, sha: str, conn: sqlite3.Connection = Conn, user=RequiredUser):
    """回滚到某个旧版本（旧版全文重走一遍写路径，引注照样逐字校验）。

    只读期间同样先被 _readonly_gate 挡成 403（裁定 R-S4-20）。
    """
    try:
        res = await store.revert(conn, path, sha, user["name"])
    except store.StoreError as exc:
        # 裁定 R75：磁盘/git 一类的运行期故障翻 500 中文信封，不把裸 traceback 甩给前端。
        raise HTTPException(500, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not res["ok"]:
        # 裁定 R79：回滚没写成与 edit 同形——400 {"error": 人话, "failed": [...]}。
        return _write_error(res)
    return res


@router.get("/api/log")
def log(conn: sqlite3.Connection = Conn, user=RequiredAdmin):
    """全站改动日志（wiki 仓库 git log 的摊平，新到旧，最多 200 条）。管理员专属。

    日志会摊开"谁改了什么、改了哪一页"（2026-10-30 裁定：普通成员不可见），所以依赖
    是 RequiredAdmin——匿名 401，成员 403 {"error": 人话}。
    一个提交都没有的空仓库返 200 []：仓库由 lifespan 的 repo.ensure_repo() 幂等建出。
    500 只剩运行期故障——读日志不需要写权限。
    """
    try:
        return [asdict(e) for e in repo.log(limit=200)]
    except (repo.GitError, OSError) as exc:
        raise HTTPException(500, f"版本库没跑通：{exc}")


# 与 /api/log 相邻：同一份"后台发生了什么"的信息，可见性也必须同一条线（管理员）。
@router.get("/api/log/outcomes")
def log_outcomes(conn: sqlite3.Connection = Conn, user=RequiredAdmin):
    """会话成果（管理员日志页）：有过 outcome 的会话 + 计数，按最近动静倒序。

    对话列表（GET /api/chat/sessions）不再附带这类计数（2026-10-30 裁定）：它们是
    "这一轮改了几页/几条引注"的运维信息，只在管理员日志页展示。形状固定为
    {"outcomes": [{id, title, owner_name, status, at, outcome}]}，outcome 的三个键
    见 services.chat._last_outcome（lint_errors 可以缺，老事件没有这一项）。
    """
    return {"outcomes": chat_service.list_session_outcomes(conn)}
