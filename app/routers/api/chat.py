"""会话接口：建会话、列表、详情、改名、归档、发消息（并触发一轮智能体跑）、SSE 尾读、上传。

三条老规矩（S1 立的），改这个文件时都得守着：
1. 归属只管改名/归档（403），可见与可发言的范围由 site_settings.chat_visibility 决定
   （2026-10-30）：`all`（默认，与迁移前一致）时全员可见可发言，`own` 时成员只看得到
   自己的会话、别人的一律 404；管理员始终看全部。七条入口统一走
   _visible_session_or_404，别在各处另写一份判据。
2. **注册顺序**：`GET /api/chat/sessions` 必须排在 `GET /api/chat/{session_id}` 之前。
   FastAPI 按注册顺序匹配路径，反过来的话 "sessions" 会被当成 session_id 去转 int，
   侧栏拿到的不是列表而是一条 422。所以下面这几条追加在文件末尾时，顺序不能颠倒。
3. 归档的会话只读（裁定 R67）：`status='archived'` 时 POST messages/upload 一律 400
   「这个会话已归档，不能再发言」；读（列表/详情/搜索/引注）不受影响。这道闸放在**路由**、
   不放进 append_human——S2 的智能体回复也走 append_human，在服务层拦会连"给归档会话
   补一条智能体回复"一起堵死。spec 对"归档后能否发言"沉默，按"删除=软归档"的直觉裁。

S2 起这个文件还多了"对话的数据面"这一半（spec §6.5）：POST messages 落完人话就把
`runner.run_turn` 丢进事件循环，`GET /api/chat/{id}/stream` 从**另一条连接**尾读
runner 与 hooks 写下的两张表（messages 里那条 agent 行的 content、agent_events 的行），
一帧一帧吐 SSE。两头只通过库说话，不共享内存里的队列，所以断线重连能按 Last-Event-ID
接着读，服务重启之后也还能把上一轮重放出来。

S11 起附件是两段式（裁定见 task-24）：`POST /api/uploads` 只暂存（不解析、不建消息、
不起轮），点发送时 `POST /api/chat/{id}/messages` 带 `upload_ids`——先把附件的
message_id 挂到这条人话上并提交，再逐个 `finalize_upload`（解析/视觉）→ raw 归档，
最后才起一轮。旧的 `POST /api/chat/{id}/upload` 保留成"暂存 + 归档闸"的兼容壳，
不再建播报消息（413 时也没有消息要撤了）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from ...agent import runner
from ...db import connect
from ...deps import Conn, RequiredUser
from ...services import access
from ...services import chat as svc
from ...services import search, uploads

logger = logging.getLogger("openllmwiki.chat")

router = APIRouter()

# 进消息正文的文件名要挡两样（裁定 R61）：控制字符（回车能把前端的一行播报撕成多行、
# \x00 一类能噎住下游的 C 工具）与超长（多 KB 的文件名会原样躺进 messages.content 与
# fts_shared 的正文，挤掉真正的话）。原件不提供下载（2026-10-30 裁定），
# 这里只需要保证进消息正文的文件名是干净、够短的一行。
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_NAME_MAX = 120


def _display_name(filename: str | None) -> str:
    return _CTRL.sub("", filename or "").strip()[:_NAME_MAX] or "未命名文件"


def _visible_session_or_404(conn: sqlite3.Connection, session_id: int, user) -> sqlite3.Row:
    """取一场"这个用户看得见"的会话行；不存在或不可见一律 404「没有这个会话」。

    2026-10-30 的对话可见范围（site_settings.chat_visibility）：`all` 模式与管理员看
    全站，`own` 模式成员只看自己的会话。**不可见与不存在给同一个 404**——403 会泄漏
    "这个 id 存在、只是不给你看"，等于把别人的会话 id 变成了可枚举的事实。

    七条入口（详情/流/发言/上传/改名/归档/取消归档）都先过这里，口径只写一份：
    各处自己判的话，很容易出现"列表挡住了、详情 404、SSE 还能看"这种半拉子隔离
    （access 模块 docstring 里点名的正是这种漏法）。
    """
    row = svc.session_row(conn, session_id)
    if row is None or not access.can_view_session(conn, user, row):
        raise HTTPException(404, "没有这个会话")
    return row


def _require_writable(conn: sqlite3.Connection, session_id: int, user) -> None:
    """写前置闸：会话得在且可见（否则 404）、且没归档（否则 400，裁定 R67）。

    只有 POST messages/upload 两条写路由走这里。rename/archive 各有各的口径
    （404 + 403，且归档幂等），读（列表/详情/搜索/引注）压根不经过这条——归档的
    会话只读，写不进去，但照样能读、能被 verify_quote 引注。

    可见性闸（2026-10-30）也在这里：`own` 模式下别人的会话写不进去，与详情/流
    同一个 404，不先落库再拒。
    """
    row = _visible_session_or_404(conn, session_id, user)
    if row["status"] == "archived":
        raise HTTPException(400, "这个会话已归档，不能再发言")


def _require_ingest_permission(user) -> None:
    """摄入材料要建设权限（2026-10-30 裁定）：没有就 403 人话。

    只挡"往对话里带附件"这两条入口（POST messages 带 upload_ids、旧 POST upload）；
    纯文字的提问仍然人人可发——查资料不需要建设权限。
    """
    if not access.can_build_wiki(user):
        raise HTTPException(403, "当前账号只能使用 wiki，不能摄入材料或修改知识库")


@router.post("/api/chat/{session_id}/messages")
async def post_message(
    session_id: int,
    content: str = Form(""),
    upload_ids: list[int] = Form(default=[]),
    conn: sqlite3.Connection = Conn,
    user=RequiredUser,
):
    """发一条人话，并**触发一轮智能体跑**（S2 起这条路由是对话的入口）。

    可见范围内的会话谁都能发（`all` 模式全员平等、`own` 模式只看得到自己的，见
    _visible_session_or_404）：归属只表示谁开的。消息记的 user_id 是发言人自己的，
    详情里 user_name 因此显示的是发言人，不是会话归属者。
    空消息由 append_human 抛 ValueError → 400，不入库也不进检索。
    裁定 R86：正文超 search.MAX_BODY（20000 字，FTS 一行只索引前 20000）是 400 人话，
    不截断入库——存进去的比索引到的多，引注校验与检索会各看各的一半。
    归档的会话只读（裁定 R67）：_require_writable 先挡下，轮不到 append_human。

    **改成 async 的理由**（原来是同步 def，跑得也挺好）：触发这一轮要
    `asyncio.create_task`，而它只在"当前线程有跑着的事件循环"时成立——同步 def 的路由
    跑在 FastAPI 的线程池工作线程里，那儿没有循环，只能去 app.state 里捞 lifespan 那条
    再 run_coroutine_threadsafe，绕一圈还得给下面那道 409 的闸自己加一把线程锁。改 async
    之后，闸的检查与 append_human 之间没有一个 await，单线程的事件循环天然把这一段变成
    原子的：两条并发的 POST 不可能都过闸。
    代价是这几条 SQL 跑在事件循环线程上：一句 SELECT + 两句写 + 一句 FTS，毫秒级；
    哪天真变成重活（解析上传件那种），照 app/routers/api/wiki.py 的口径包进
    asyncio.to_thread，别改回同步 def（那会把触发这条链一起拆掉）。

    **前端配套（裁定 R-S2-30）**：响应里的 `id` 就是这条人话的消息 id，客户端应当
    拿它去开流——`GET /api/chat/{id}/stream?after=<这个 id>`。带了 after，尾读只等
    seq 大于这条人话的**新** agent 行，不会先把这场会话里上一轮（可能早已封口）的
    旧回复重放一遍；不带 after 的行为不变（钉住最新一条 agent 行）。响应形状刻意
    不加提示键，口径就写在这里。

    **附件（S11 两段式）**：`upload_ids` 是前端选文件时 POST /api/uploads 拿到的暂存
    id。附件在**发送这一刻**才解析（含视觉）并挂到这条人话上：先 append_human，再把
    upload_files.message_id 指过来，然后逐个 `finalize_upload`（to_thread，不阻塞循环），
    解析成功再 ingest_raw 归档。空文字+只有附件也允许（内容自动写成"上传了《…》"）；
    空文字+无附件仍 400。解析失败不挡发送。响应 {id, attachments:[…]}。

    **可见范围与建设权限（2026-10-30）**：`own` 模式下别人的会话是 404（_require_writable
    经 _visible_session_or_404 判）；带附件发消息要有建设权限（摄入材料），非管理员且
    can_build_wiki=0 时 403——两道闸都排在 append_human 之前，不先落库再拒。
    """
    _require_writable(conn, session_id, user)
    if upload_ids:
        _require_ingest_permission(user)
    if len(content) > search.MAX_BODY:
        raise HTTPException(
            400, f"这条消息太长了（最长 {search.MAX_BODY} 字），拆成几条再发"
        )
    if session_id in PARSING:
        # 上一条消息的附件还在解析（PDF/视觉可能几分钟）。这一段没有 RUNNING 那么久，
        # 但同样不能放第二条消息进来：第二条会立刻起一轮，智能体看到的附件还是
        # parse_status='none'，于是把"还没解析完"说成"这类不解析"，摄入就假失败了。
        raise HTTPException(409, "上一条消息的附件还在解析，等它完成再发")
    if session_id in RUNNING:
        # R-S2-1 的重入闸：这场会话上一轮还在写。闸在 append_human **之前**——先落库再
        # 409 会在库里留一条永远没人回的话（在跑的那一轮早就取过上下文了，看不见后到的
        # 这句），侧栏的标题与"最近动静"还会跟着变，人以为发出去了。409 的意思就是
        # "这一条没受理"：话还留在输入框里，等它说完再发。
        raise HTTPException(409, "智能体还在写，等它说完")

    staged = _claim_staged_uploads(conn, upload_ids, user["id"])
    if not content.strip() and not staged:
        raise HTTPException(400, "空消息不入库")
    if not content.strip():
        # 只有附件：替人写一句播报（与旧上传路由同一句话），附件由 _history 追加成
        # [附件] 行给模型，模型据此摄入。
        names = "》、《".join(_display_name(row["orig_filename"]) for row in staged)
        content = f"上传了《{names}》"

    try:
        mid = svc.append_human(conn, session_id, user["id"], content)
    except ValueError:
        raise HTTPException(400, "空消息不入库")

    if staged:
        marks = ",".join("?" * len(staged))
        cur = conn.execute(
            f"UPDATE upload_files SET message_id = ?"
            f" WHERE id IN ({marks}) AND user_id = ? AND message_id IS NULL",
            (mid, *[int(row["id"]) for row in staged], user["id"]),
        )
        if cur.rowcount != len(staged):
            # 并发窗口里有人撤了/被别人挂了：整条消息回滚（get_conn 的 rollback），
            # 让人回到输入框重发，而不是留一条"附件丢了"的消息。
            raise HTTPException(400, "有些附件已失效，重新上传")

    # 自己提交一次，别等 get_conn 的收尾：后台任务在**另一条**连接上读上下文
    # （runner._history），而 create_task 起的任务可能比那次收尾先跑起来（收尾要过一趟
    # 线程池，那是一次 await）。不提交的话，这一轮压根看不见刚发的这句人话，回的是上一句。
    # 附件也在这之前挂好 message_id：_history 就是按它批量取 [附件] 行的。
    conn.commit()

    attachments: list[dict] = []
    if staged:
        PARSING.add(session_id)
        try:
            attachments = await _finalize_attachments(conn, staged, user["name"])
        finally:
            PARSING.discard(session_id)

    if session_id in RUNNING:
        # 解析附件期间另一条消息可能已经起了一轮：不并发起第二轮。这一条消息已经落库，
        # 在飞那一轮的 history 是在它之后取的（会看到它，包括 [附件] 行）。
        logger.warning(
            "消息已入库，但会话 %s 已有一轮在跑，这一轮不再另起（附件已入库）", session_id
        )
    else:
        # actor 里带齐这一轮的两个能力/可见性快照（2026-10-30）：runner 把它们塞进
        # AgentContext，tools 据此拒绝越权写、search 据此过滤消息命中。
        # message_owner_id 是 `own` 模式下才有的 user id（all/管理员为 None）。
        _start_turn(session_id, {
            "id": user["id"],
            "name": user["name"],
            "can_build_wiki": access.can_build_wiki(user),
            "message_owner_id": access.message_owner_filter(conn, user),
        })
    if attachments:
        return {"id": mid, "attachments": attachments}
    return {"id": mid}


def _claim_staged_uploads(conn: sqlite3.Connection, upload_ids, user_id: int) -> list:
    """校验并收下这次要发的暂存附件：必须是本人、还没挂消息的件。

    任何一个 id 不合格（不存在/别人的/已经发过的）→ 400「有些附件已失效，重新上传」，
    让前端回到"重新选择附件"的状态；不静默丢附件。
    """
    ids: list[int] = []
    for raw in upload_ids or []:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(400, "有些附件已失效，重新上传") from None
        if value not in ids:
            ids.append(value)
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT * FROM upload_files"
        f" WHERE id IN ({marks}) AND user_id = ? AND message_id IS NULL",
        (*ids, user_id),
    ).fetchall()
    by_id = {int(row["id"]): row for row in rows}
    if len(by_id) != len(ids):
        raise HTTPException(400, "有些附件已失效，重新上传")
    return [by_id[value] for value in ids]


async def _finalize_attachments(conn: sqlite3.Connection, staged: list, actor: str) -> list[dict]:
    """发送时才解析附件（含视觉/转换），解析成功再归档 raw；失败不挡发送。

    解析是阻塞活（pypdf/视觉/soffice 可能几十秒），逐个丢进 asyncio.to_thread，不占
    事件循环。同一连接串行用：store 的写路径与 ingest_raw 都在 to_thread 里跑，await
    期间事件循环不碰这条连接（与 wiki 写路由同口径）。
    """
    out: list[dict] = []
    for row in staged:
        upload_id = int(row["id"])
        try:
            fin = await asyncio.to_thread(uploads.finalize_upload, conn, upload_id)
        except Exception as exc:  # noqa: BLE001 - 解析异常不挡发送
            logger.warning("附件解析没做成（upload_id=%s）：%r", upload_id, exc)
            fin = {"ok": False, "parse_status": row["parse_status"], "chars": 0}
        conn.commit()
        raw_path = None
        if fin.get("ok") and fin.get("parse_status") == "done":
            raw_path = await asyncio.to_thread(
                uploads.ingest_raw, conn, upload_id, row["orig_filename"], actor
            )
        out.append({
            "upload_id": upload_id,
            "orig_filename": row["orig_filename"],
            "parse_status": fin.get("parse_status"),
            "chars": fin.get("chars", 0),
            "raw_path": raw_path,
        })
    conn.commit()
    return out


# --- 触发一轮跑（R-S2-1 的重入闸在这里） ----------------------------------------

# 一场会话同一时刻只许一轮在跑。键是 session_id，进程内的集合。
# 为什么不用库里的形状判（"有一条 usage_json IS NULL AND truncated=0 的 agent 行"）：
# 那个形状在进程被杀之后会一直留着（要等下次启动的 db.seal_orphan_agent_messages 才封），
# 拿它当闸的话，那场会话在重启之前每条 POST 都是 409，而人只看见"智能体还在写"。
# 与 store/repo/throttle 的锁同一个前提：进程内状态只对 --workers 1 正确（DEPLOY.md 写着）。
RUNNING: set[int] = set()

# 附件解析中的会话。解析发生在 POST messages 里、起轮之前（可能要几分钟：大 PDF +
# 视觉描述），这段时间 RUNNING 还不该置位（没有一轮在跑），但**也不能让第二条消息
# 起一轮**——第二条会读到一个 parse_status='none' 的附件，把"还在解析"误报成
# "这类不解析"（2026-10-30 测试账号的真实现象）。与 RUNNING 分开的另一个集合，
# 422/409 文案才能说清是"附件还在解析"而不是"智能体还在写"。
PARSING: set[int] = set()

# asyncio 只对任务持弱引用：不留一份强引用，跑到一半的任务可能被 GC 掐掉（官方的
# create_task 文档就点明了这一条）。done 回调里摘掉，不随会话越攒越多。
_TASKS: set[asyncio.Task] = set()


def _start_turn(session_id: int, actor: dict[str, Any]) -> None:
    """开一条独立连接，把这一轮丢给事件循环当后台任务。

    连接现开：请求那条随请求结束就关（get_conn 的 finally），而这一轮要跑几十秒——
    S1 审查立下的规矩，请求连接不交给后台任务。run_turn 不关它（谁开谁关），所以关在
    _drive_turn 的尾巴上。

    **这个函数是承重的注入口子**：tests/conftest.py 的 autouse fixture 把它换成空转，
    否则每条 POST messages 的用例都会拿假 key 往真上游发一次请求，还会往会话里塞一条
    agent 行（"详情里消息条数"一类断言当场红）。要真跑一轮的用例带 conftest 的
    real_agent fixture。改这个函数的名字/签名要连着改 conftest。
    """
    run_conn = connect()          # 先开连接：开不出来就别把 id 塞进闸里
    RUNNING.add(session_id)
    task = asyncio.create_task(_drive_turn(run_conn, session_id, actor))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def _drive_turn(conn: sqlite3.Connection, session_id: int, actor: dict[str, Any]) -> None:
    """后台任务的本体：跑一轮，跑完把闸摘掉、把连接关掉。

    **今天没有任何一条路径取消这个任务，保持这样。** 取消一轮跑要小心的地方在连接上：
    run_turn 把 CancelledError 原样抛回来，而工具体跑在 asyncio.to_thread 的 worker
    线程上、不受取消影响，它可能还在这条连接上写着（T4 留给 T5 的那句话）。所以下面
    conn.close() 只在没被取消时走到——CancelledError 是 BaseException，except Exception
    接不住它，它带着 finally 直接穿出去，close 被跳过。这条连接宁可漏（进程回收），
    也不要在在飞的工具脚底下关掉；真要支持取消，得先等在飞的工具线程落地。
    """
    try:
        await runner.run_turn(conn, session_id, actor)
    except Exception:   # noqa: BLE001 —— run_turn 承诺不抛；真抛了也不能把异常留在没人取的任务里
        logger.exception("这一轮跑抛出了 run_turn 承诺不抛的异常（session_id=%s）", session_id)
    finally:
        # 正常、异常、取消都要摘：留着的话这场会话从此每条 POST 都是 409
        RUNNING.discard(session_id)
    conn.close()


# --- SSE：尾读数据面 -------------------------------------------------------------

POLL_SECONDS = 0.25      # 尾读的轮询间隔（计划口径）。一拍 = 一次读 + 一次 sleep。

# 尾读的墙钟上限（I6）：一条流挂多久也得有个头。正常形状下尾读到 done 就自己关；
# 但"等一轮永远不封口的跑"（进程被杀留下的半截行、触发链断在 create_task 之前、
# 客户端连上来就再没人发消息）会让生成器无限期挂着——连接占着、人也一直看着转圈。
# 到点发一帧 error{code=stream_timeout} 再关，前端能按 code 提示"刷新或重发"。
# 10 分钟对一轮跑（几十秒）与"先连流再发消息"（人打字的速度）都是几十倍余量。
STREAM_TIMEOUT_SECONDS = 600
STREAM_TIMEOUT_TEXT = "这条流等得太久了，先断开。刷新一下看最新内容，或把刚才那句重发一次。"

# id 空间的分界：id = seq * ID_STRIDE + 正文偏移。两个上界都留了几十倍余量——
# 一轮回复的正文再长也就几万字（模型的 max output tokens 就那么大），一场会话的事件
# 也到不了一百万行，所以 id 恒小于 10^12，前端拿 JS 的 Number 存也不掉精度
# （超过 2^53 ≈ 9×10^15 才会）。详见 _frame_id。
ID_STRIDE = 1_000_000

# 上面那两个"余量"不是自动成立的，是 _frame_id 与 _decode_cursor 两道闸守着的
# （裁定 R-S2-23：越界要响亮，不许静默撞车）。id 必须还能被 JS 的 Number 精确表示，
# 否则前端一 parseInt 就悄悄掉位，续读解出来的游标是个谁也说不清的数。
_MAX_SAFE_ID = 2**53 - 1                  # JS 的 Number.MAX_SAFE_INTEGER
_MAX_SEQ = _MAX_SAFE_ID // ID_STRIDE      # 高位还能涨到多少（≈ 9×10^9，永远到不了）


def _frame_id(seq: int, offset: int) -> int:
    """一帧的 id：把续读要恢复的两件状态压进一个整数 —— `seq * ID_STRIDE + offset`。

    **一个 id 空间，不是两个。** Last-Event-ID 只有一个数，而续读要恢复的是两件事：
    正文发到第几个字（messages.content 的偏移，text-delta 那一半）、事件发到第几行
    （agent_events.seq，tool-call/tool-result/system/error 那一半）。于是把游标
    (seq, offset) 按字典序编成一个整数：高位是已发过的最大 seq，低位是已发过的正文字数。
    两件各自单调不减，而每发一帧至少有一件严格变大（text-delta 让 offset 变大、
    事件帧让 seq 变大），所以 id 严格递增；反过来 _decode_cursor 就把游标原样
    解出来，服务端不必为每个客户端存一点状态。

    事件帧的高位**就是 agent_events.seq**（id // ID_STRIDE == seq），于是 text-delta 的
    id 恒大于此前任何事件帧的 id——"合成 id 要大于已见事件 id"是这套编码的自然结果，
    不是另加的一条规则。done 用游标再 +1（它没有自己的状态可编），解回来是"正文与事件
    都发完了"，所以断线重连撞上 done 之后只会再收到一个 done，不会重放正文。

    **越界是响亮的（裁定 R-S2-23）。** 编码是"低位当进制位用"，一旦 offset 顶穿
    ID_STRIDE，它就吃进下一个 seq 的号段——两个不同的游标编出同一个 id，续读从此错乱，
    而错乱是安静的（前端只是从半截开始收）。所以这里不留 clamp、不留 modulo：
    撑不住就抛，让人看见"这一版的假设不成立了"（一轮回复超一百万字、或一场会话的事件
    超九百万条），而不是发一个撞车的 id 出去。
    """
    if not 0 <= offset < ID_STRIDE:
        raise ValueError(
            f"帧 id 的低位越界：正文偏移 {offset} 不在 [0, {ID_STRIDE}) 内，"
            "会与相邻 seq 的号段撞车（续读会解出错乱的游标）"
        )
    if not 0 <= seq <= _MAX_SEQ:
        raise ValueError(
            f"帧 id 的高位越界：事件 seq {seq} 不在 [0, {_MAX_SEQ}] 内，"
            f"铸出来的 id 会超过 JS 能精确表示的整数（{_MAX_SAFE_ID}），前端会掉精度"
        )
    return seq * ID_STRIDE + offset


def _decode_cursor(last_event_id: int) -> tuple[int, int]:
    """Last-Event-ID → 游标 `(seq, offset)`（_frame_id 的逆运算）。

    divmod 出来的 offset 天然落在 [0, ID_STRIDE) 里，所以这一侧要挡的不是撞车，而是
    "这个数压根不在本端点的 id 空间里"：负数，或者超过 _MAX_SAFE_ID——_frame_id 铸不出
    这种数，只有人手搓的 Last-Event-ID、或前端把 id 存进 JS Number 掉了精度才会出现。
    这种值当 0 处理也是静默错乱（它可能落在某一轮的正中间，续读就从那儿发半截正文），
    所以按 R-S2-23 响亮地抛。

    **抛出去的是 400，不是 500（裁定 R-S2-27）**：路由入口（stream_events）在返回
    StreamingResponse 之前先调一遍这个函数，ValueError 在那里翻成 400「续读位置不合法」
    ——等 _tail 在流中间才抛，响应头早已是 200 text/event-stream，客户端拿到的只能是
    半截流加一条 traceback。_tail 里那次调用保留，是服务端自己的最后一道闸。

    offset 那一道（`0 <= offset < ID_STRIDE`）在 floor division 下数学上到不了
    （余数恒小于除数），写出来是为了让"合法游标"的三条条件齐在代码里而不是散在注释里：
    哪天解码的算术被改掉，先红在这儿，而不是静默按一个撞车的游标续读。
    """
    if not 0 <= last_event_id <= _MAX_SAFE_ID:
        raise ValueError(
            f"Last-Event-ID {last_event_id} 不在本端点的 id 空间 [0, {_MAX_SAFE_ID}] 内，"
            "这个数不可能是本端点铸出来的帧 id"
        )
    seq, offset = divmod(last_event_id, ID_STRIDE)
    if not 0 <= offset < ID_STRIDE:
        raise ValueError(
            f"Last-Event-ID {last_event_id} 解出的正文偏移 {offset} 越出 [0, {ID_STRIDE})，"
            "会与相邻 seq 的号段撞车（floor division 下本不该出现，见到即解码算术被改过）"
        )
    return seq, offset


def _parse_last_event_id(raw: str) -> int:
    """`Last-Event-ID` 头的值 → 整数；解不动的一律当 0（整条重放当前那一轮）。

    用 try/except 而不是 `raw.isdigit()`（裁定 R-S2-23）：isdigit 认的不只是 ASCII 的
    0-9——`"²".isdigit()` 与 `"⑤".isdigit()` 都是 True，而 `int("²")` 抛 ValueError。
    按 isdigit 判就会把这种头当"能解"送进 int()，一条 500 顶在流上。反过来 int() 认得的
    东西（前后空白、正负号）isdigit 又不认——两个谓词谁也不是谁的子集，让 int() 自己说
    是唯一不打架的口径。
    """
    try:
        return int(raw)
    except ValueError:
        return 0


def _frame(frame_id: int, event: str, data: dict | str) -> str:
    """一帧 SSE：`id: <n>\\nevent: <type>\\ndata: <json>\\n\\n`（spec §6.5 的 schema）。

    data 给了 str 就原样发：事件行的 payload_json 已经是 JSON 了，解一遍再拼一遍只是
    多一次往返、还会把键序改掉。json.dumps 出来的串里没有裸换行（控制字符都转义过），
    所以一帧就是一行 data，不会把帧撕开。
    """
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"id: {frame_id}\nevent: {event}\ndata: {payload}\n\n"


def _agent_row(conn: sqlite3.Connection, session_id: int,
               message_id: int | None = None,
               after_seq: int | None = None) -> sqlite3.Row | None:
    """当前那条 agent 行：给了 message_id 就按 id 取（钉住），没给就取最新一条。

    钉住是防漂：一轮封口之后紧接着又有人发了一句（第二轮开口），"最新一条"就换成新的了，
    而尾读手里的 offset 是上一条的正文长度，拿它去切新那条等于从半截开始发。

    after_seq（裁定 R-S2-30，只在还没钉住 message_id 时起作用）："取最新一条"再往前
    收窄成"取 seq 大于 after_seq 的最新一条"——客户端带着 ?after=<上一条人话的 id>
    连上来时，比它老的 agent 行（早已封口的上一轮）一概不算，没有更新的行就继续等。
    不带 after 的老行为一个字不变。
    """
    if message_id is not None:
        return conn.execute(
            "SELECT id, content, created_at, truncated, usage_json FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()
    sql = ("SELECT id, content, created_at, truncated, usage_json FROM messages"
           " WHERE session_id = ? AND role = 'agent'")
    params: tuple = (session_id,)
    if after_seq is not None:
        sql += " AND seq > ?"
        params += (after_seq,)
    sql += " ORDER BY seq DESC, id DESC LIMIT 1"
    return conn.execute(sql, params).fetchone()


def _pending_events(conn: sqlite3.Connection, session_id: int, after_seq: int,
                    since: str, message_id: int) -> list[sqlite3.Row]:
    """这一轮里还没发过的事件行（按 seq 升序）。

    `seq > after_seq` 是续读的那一半；一轮的边界按 **message_id** 划（裁定 R-S2-20，
    迁移 0006 给 agent_events 加的那一列）：`message_id = 当前 agent 行的 id` 精确命中
    这一轮的事件，同秒内连着跑的两轮也不会把上一轮的尾巴再发一遍（旧口径靠秒级的
    created_at 划界，正是这里串的）。

    `message_id IS NULL` 的老行（迁移 0006 之前落的，没有归属可补）**退回旧的
    created_at 口径**：`created_at >= 那条 agent 行的 created_at` 就算这一轮的。
    这是 legacy 兼容，不是并行的第二条规则——新写的事件一律带 message_id
    （hooks 与 runner._event 都传），NULL 行只会越来越少。
    """
    return conn.execute(
        "SELECT seq, type, payload_json FROM agent_events"
        " WHERE session_id = ? AND seq > ?"
        " AND (message_id = ? OR (message_id IS NULL AND created_at >= ?))"
        " ORDER BY seq",
        (session_id, after_seq, message_id, since),
    ).fetchall()


async def _tail(session_id: int, last_event_id: int, after_seq: int | None = None):
    """尾读一场会话的数据面，一帧一帧吐 SSE，发完 done 就结束。

    after_seq（裁定 R-S2-30）：路由把 ?after=<消息 id> 换算成那条消息的 seq 交进来，
    _agent_row 据此只认"比它新"的 agent 行；None 就是老行为（最新一条 agent 行）。

    读的是**自己**那条连接（现开，finally 里关），不是请求那条、更不是 runner 那条：
    请求那条在响应开始流之前就被 FastAPI 收走了（带 yield 的依赖在响应发出前退出，
    0.106 起的口径），runner 那条正被后台任务写着，两条都不能拿。
    `isolation_level = None` 是承重的：这条连接只读，可 Python 的 sqlite3 在默认隔离
    级别下会在第一次 DML 前开隐式事务——真开着一个长事务的话，WAL 下它读到的是那个
    快照，runner 之后写的增量一概看不见，尾读就成了死的。设成 None 之后每条 SELECT
    自成一个读事务，每一拍都是新快照。
    睡觉（await asyncio.sleep）的时候手里没有任何锁：这条连接是只读的，也不去碰
    runner 那把 db_lock，所以尾读在睡、在读，都拦不住写侧。

    一拍干三件事，顺序有讲究：

    1. 这一拍新长出来的正文发一帧 text-delta（一拍里的增量合成一帧，前端只管往尾巴上接）；
    2. 没发过的事件行按 seq 逐条发（tool-call/tool-result/system/error，payload_json
       原样当 data，不加工、不补键）；
    3. 看封口：`usage_json IS NOT NULL OR truncated=1` 就是这一轮结束了（"还在跑"的唯一
       记号是 usage_json IS NULL AND truncated=0，与 db.seal_orphan_agent_messages 的
       判据互为反面），发 done 收工。

    1 在 2 之前，因为"先说一句再调工具"是模型最常见的形状；反过来的情形（工具结果之后
    才说的话）只要两件事不落在同一拍里就不会搞错顺序——一拍 0.25 秒，而一次工具往返
    加上模型再开口通常是秒级。真落在同一拍里时顺序无从得知：数据面只留了正文的终态与
    事件的 seq，没留"事件落库那一刻正文有多长"，所以事后重放一整轮时，正文会合成一帧
    排在事件之前（tests/test_stream_api.py 把这个形状钉住了）。这是尾读的固有近似，
    不是 bug，要精确就得给 agent_events 记当时的正文偏移。

    done 判据一满足就退出，**不等 tool-call 有没有配对的 tool-result**：白名单拒掉在飞的
    兄弟工具、这一轮被取消、上游失败，SDK 都不会调 on_tool_end（T3 留给 T5 的那句），
    等配对就永远等不到。反过来也不用"再等一拍"去看解释行：封口是一轮的最后一次写
    （R-S2-18），看到 done 时 system/error 行一定已经在库里了。

    **墙钟上限（I6）**：等不到可钉的行、或钉住的行永远不封口（进程被杀留下的半截、
    连上来就再没人发消息），到 STREAM_TIMEOUT_SECONDS 就发一帧
    error{code=stream_timeout} 然后关流——这个 code 不落 agent_events，只活在流上
    （那时可能压根没有 agent 行可归属），词汇表的注记见 runner.py 那段 M18 注释。
    """
    seq, offset = _decode_cursor(last_event_id)
    message_id: int | None = None
    deadline = time.monotonic() + STREAM_TIMEOUT_SECONDS
    conn = connect()
    conn.isolation_level = None
    try:
        while True:
            if time.monotonic() >= deadline:
                # 墙钟到点（I6）：不管是"等不到可钉的行"还是"钉住的行永远不封口"，
                # 都发一帧 stream_timeout 收工，别把连接无限期挂着。id 用 offset+1
                # （与 done 同一个手法）：保证严格大于此前发过的任何一帧。
                yield _frame(_frame_id(seq, offset + 1), "error",
                             {"code": "stream_timeout", "text": STREAM_TIMEOUT_TEXT})
                return
            row = _agent_row(conn, session_id, message_id, after_seq)
            if row is None:
                # 还没有可钉的 agent 行：要么这一轮还没被触发（**无历史**的会话先连流、
                # 再发消息是正常用法），要么带了 after 而新一轮还没开口。等着——SSE
                # 本来就是长连接，客户端断开时 starlette 会取消这个生成器；等穿了墙钟
                # 上限就发 stream_timeout 收工（见上）。
                await asyncio.sleep(POLL_SECONDS)
                continue
            message_id = int(row["id"])
            content = row["content"] or ""
            if offset > len(content) + 1:
                # 陈旧游标（裁定 R-S2-22）：这个 Last-Event-ID 是**上一轮**铸的，而上一轮
                # 的正文比这一轮长。id 里没有 message_id，所以那个 offset 对新钉住的这一行
                # 没有意义；不归零的话 `len(content) > offset` 永远不成立（正文只会长、
                # offset 不会自己变小），这一轮的开头就被**永久跳过**——人只看见回复从
                # 半截开始，而且刷新之前一直是这样。这不是边角形状：done 之后服务端关流，
                # 浏览器 EventSource 的默认行为就是带着最后那个 id 重连，正好把上一轮的
                # 游标交到下一轮手上。
                # +1 是给"done 之后的合法重连"留的口子：done 的 id 用的是 offset+1，
                # 落在阈值之内，不归零，所以那次重连只再收到一个 done，不会把已发完的
                # 正文重放一遍（前端就会看见同一段话说两遍）。
                offset = 0
            if len(content) > offset:
                chunk = content[offset:]
                offset = len(content)
                yield _frame(_frame_id(seq, offset), "text-delta", {"text": chunk})
            for event in _pending_events(conn, session_id, seq, row["created_at"], message_id):
                seq = int(event["seq"])
                yield _frame(_frame_id(seq, offset), event["type"], event["payload_json"])
            if row["usage_json"] is not None or row["truncated"]:
                # usage 恒为 null：SDK 的流式结果不暴露 token 用量，S2 的 usage_json
                # 只是"这一轮跑完了"的记号（'{}' 或 NULL，见 runner.DONE_USAGE 那张表），
                # 不是用量。token 用量去 DeepSeek 控制台看（计划 Global Constraints）。
                yield _frame(_frame_id(seq, offset + 1), "done",
                             {"message_id": message_id, "usage": None})
                return
            await asyncio.sleep(POLL_SECONDS)
    finally:
        conn.close()


@router.get("/api/chat/{session_id}/stream")
async def stream_events(
    request: Request,
    session_id: int,
    after: int | None = None,
    conn: sqlite3.Connection = Conn,
    user=RequiredUser,
):
    """SSE：尾读这一场会话的数据面（spec §6.5 的六种帧）。

    匿名是 401 JSON（RequiredUser 抛 NeedsLogin，走 main.py 那套信封），查无此会话是
    404——两条都在返回 StreamingResponse 之前判完，所以出错时是 JSON，不是半截流。
    这里那条 conn 只用来判 404（依赖链本来就为鉴权开了一条），尾读另开自己的。

    **支持的连法（I2 更正后的口径，S3 照这一段写）**：

    1. 先 POST messages、再连流（带不带 after 都行）：这一轮已开口的话从头重放并
       接着尾读，没开口就等着。**这是有历史的会话唯一正确的顺序。**
    2. 先连流、再 POST messages：**只对这场会话还没有任何 agent 行时成立**（崭新的
       会话）。有历史时，不带 after 连上来会立刻把上一轮（可能早已封口）整条重放、
       发 done、关流——新一轮压根收不到；带 ?after=<刚 POST 返回的人话 id> 才等得到
       新一轮（裁定 R-S2-30）。
    3. 一轮已经说完之后再连上来（不带 Last-Event-ID）：**把那一轮整条重放一遍
       （正文 + 事件 + done）然后关流**——这是设计如此，不是 bug：尾读不区分"直播"
       与"重放"，钉住最新一条 agent 行从头读起。只想等新一轮的，用 ?after=。
       带着 done 帧的 Last-Event-ID 重连则只再收到一个 done（游标已在末尾）。

    after=<message_id>：可选。给了就只认 seq 大于那条消息的 agent 行（旧轮不重放，
    新轮没开口就等着，等穿 STREAM_TIMEOUT_SECONDS 发 stream_timeout 关流）。消息
    不存在或不属于这场会话是 400，在返回 StreamingResponse 之前判掉（口径同下面的
    Last-Event-ID 越界）。

    Last-Event-ID 是上一趟最后那帧的 id（浏览器断线重连时 EventSource 自己带上），
    解出 (seq, offset) 从它后面续；没有、或解不动，就当 0——把当前那条 agent 行整条
    重放一遍再发 done。前端重连之后拿到的是完整的最新一轮，不会缺中间一段。
    解**得动**但越界的数（负数、≥ 2^53、偏移分量越出 ID_STRIDE）是 400
    「续读位置不合法」（裁定 R-S2-27），在路由入口判完，不会拖到流中间炸成半截流。

    **前端契约（裁定 R-S2-25，S3 照这一段写）**：这条流发到 `done` 就自己关，一轮到此
    为止，`done` 之后**不要**再带着 Last-Event-ID 重连——续读只为"一轮中间断线"准备
    （重连后从断掉那帧之后接着发，正文与事件都不重复）；一轮已经说完之后再连上来，
    拿到的是整轮重放加一个 done（见上面第 3 条），浏览器 EventSource 的默认自动重连
    在这儿就是空转，还会把上一轮的陈旧游标交到下一轮手上（_tail 里那道 R-S2-22 的
    守卫就是为兜住这个形状而加的）。所以前端读到 `done` 要主动 `close()`，下一轮发言
    时再新开一条流（带上 after），别指望一条流长挂着跨轮复用。服务端也**故意不发**
    `retry:` 字段：重连间隔保持浏览器的默认值，我们不用它调节奏——这条流的正确形状
    是"一轮一条、done 即止"，发 retry 反而像是在鼓励重连。
    **可见范围（2026-10-30）**：`own` 模式下别人的会话在这条流上是 404，与详情/发言
    同一个判据——不能出现"列表挡住了、详情 404、SSE 还能看"的半拉子隔离。

    跨轮重连一律**不带** Last-Event-ID 的原因要说透（裁定 R-S2-28）：id 空间里没有
    message_id，服务端无法分辨"这个中段游标铸自另一轮"，所以续读契约只覆盖同一轮内
    的断线重连。
    """
    _visible_session_or_404(conn, session_id, user)
    after_seq: int | None = None
    if after is not None:
        row = conn.execute(
            "SELECT seq FROM messages WHERE id = ? AND session_id = ?",
            (after, session_id),
        ).fetchone()
        if row is None:
            # 与 Last-Event-ID 越界同一个口径（R-S2-27）：输入不合法要在响应头发出
            # 之前判掉，JSON 信封到得了客户端；拖进 _tail 就只能是半截流。
            raise HTTPException(400, "after 指着的消息不在这场会话里")
        after_seq = int(row["seq"])
    last_event_id = _parse_last_event_id(request.headers.get("last-event-id") or "")
    try:
        _decode_cursor(last_event_id)
    except ValueError:
        # R-S2-27：客户端交来的数越界是**输入不合法**，得在返回 StreamingResponse 之前
        # 判掉——那时响应头还没发出去，HTTPException 走得到 main.py 的 JSON 信封；
        # 拖到 _tail 里才抛，客户端拿到的就是一条 200 的半截流加 traceback。
        # 解不动（非数字）的不在此列：_parse_last_event_id 已把它归成 0（整条重放）。
        raise HTTPException(400, "续读位置不合法") from None
    return StreamingResponse(
        _tail(session_id, last_event_id, after_seq),
        media_type="text/event-stream",
        # 反代（nginx）默认攒缓冲，SSE 攒了就成一次性交付，前端看着像卡死；no-cache 是
        # 别让任何一层缓存这条流。S1 的旧 stream 端点就是这两条，照抄。
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/chat/{session_id}/upload")
def post_upload(
    session_id: int,
    conn: sqlite3.Connection = Conn,
    user=RequiredUser,
    file: UploadFile = File(...),
    note: str = Form(""),
):
    """**兼容旧前端**：这里只暂存（不解析、不建消息、不起轮）。

    S11 两段式之后，选文件不该立刻发给智能体——新前端请用 `POST /api/uploads` 暂存，
    点发送时把 upload_id 放进 `POST /api/chat/{sid}/messages` 的 upload_ids。这条旧路由
    保留成"暂存 + 会话可写校验"的薄壳，避免旧客户端 404；note 不再参与（附件说明由
    发送时的消息正文承担）。

    暂存也是一次摄入准备（2026-10-30）：没有建设权限的成员 403，闸排在 stage_upload
    之前——别先把文件收下来再拒。可见性同样先判（别人的会话 404）。
    """
    _require_writable(conn, session_id, user)   # 可见 + 归档只读（裁定 R67）
    _require_ingest_permission(user)
    try:
        return uploads.stage_upload(conn, user["id"], file)
    except uploads.FileTooLargeError:
        raise HTTPException(413, "文件超过 50 MB")


# ------------------------------------------------------------------ 会话本身
# 以下五条追加在文件末尾，但 GET /api/chat/sessions 与 GET /api/chat/{session_id}
# 的相对顺序不能换（见模块 docstring 第 2 条）。


@router.post("/api/chat/sessions")
def create_session(conn: sqlite3.Connection = Conn, user=RequiredUser):
    """一点即建。标题留空，首条消息的前 20 字自动当名字。"""
    return {"id": svc.create_session(conn, user["id"])}


@router.get("/api/chat/sessions")
def list_sessions(conn: sqlite3.Connection = Conn, user=RequiredUser):
    """会话列表（带 owner_name 与 status），按对话可见范围过滤。

    `all` 模式（默认，与迁移前一致）与管理员看全站，`own` 模式成员只看自己的会话
    （access.message_owner_filter 给出"按谁过滤"，None 表示不过滤）。侧栏还要按 owner
    分组、"我的"排最上，所以每条都带 owner_name。
    """
    return svc.list_sessions(conn, access.message_owner_filter(conn, user))


@router.get("/api/chat/{session_id}")
def session_detail(session_id: int, conn: sqlite3.Connection = Conn, user=RequiredUser):
    """消息列表（按 seq），每条带 user_name、附件元数据与 events。归档的会话照样能读。

    看不见的会话与不存在的会话同一个 404（_visible_session_or_404）。

    events（裁定 R-S3-8）：agent 行按 message_id 挂上它这一轮的 agent_events
    （`[{"type": …, "payload": <对象>}]`，按 seq 升序），其余行恒为空列表。前端据此在
    **刷新之后**仍能把工具卡片与横幅渲染出来——这些帧原来只活在 SSE 流里，见
    services/chat._message_events 的三条边界（NULL message_id 的老行不挂、正文不重复交）。
    """
    _visible_session_or_404(conn, session_id, user)
    got = svc.get_session(conn, session_id)
    if got is None:
        raise HTTPException(404, "没有这个会话")
    return got


@router.patch("/api/chat/{session_id}")
def rename_session(
    session_id: int, payload: dict, conn: sqlite3.Connection = Conn, user=RequiredUser
):
    """改名。只有归属者能改（403）；会话不存在或看不见是 404，不跟 403 混。

    先判可见再判归属：`own` 模式下别人的会话答 404（不泄漏"它存在"），
    能看见但不是自己开的才答 403「只有开这个会话的人能改名」。
    """
    _visible_session_or_404(conn, session_id, user)
    try:
        svc.rename(conn, session_id, user["id"], payload.get("title", ""))
    except svc.NotOwner:
        raise HTTPException(403, "只有开这个会话的人能改名")
    except ValueError:
        raise HTTPException(400, "标题不能是空的")
    return {"ok": True}


@router.post("/api/chat/{session_id}/archive")
def archive_session(session_id: int, conn: sqlite3.Connection = Conn, user=RequiredUser):
    """归档（= 软删除）。只有归属者能归档；归档后仍可搜索、仍可被引注。

    看不见的会话 404（_visible_session_or_404），看得见但不是自己的才 403。
    """
    _visible_session_or_404(conn, session_id, user)
    try:
        svc.archive(conn, session_id, user["id"])
    except svc.NotOwner:
        raise HTTPException(403, "只有开这个会话的人能归档")
    return {"ok": True}


@router.post("/api/chat/{session_id}/unarchive")
def unarchive_session(session_id: int, conn: sqlite3.Connection = Conn, user=RequiredUser):
    """取消归档：从折叠区回到活跃列表。只有归属者能操作；幂等。

    可见性口径与归档同一条：看不见的会话 404，不是自己的 403。
    """
    _visible_session_or_404(conn, session_id, user)
    try:
        svc.unarchive(conn, session_id, user["id"])
    except svc.NotOwner:
        raise HTTPException(403, "只有开这个会话的人能取消归档")
    return {"ok": True}
