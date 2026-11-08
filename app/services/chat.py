"""会话与消息的存取。全员可见、全员可发言；归属只表示谁开的。

三条不变量都是"取消私聊隔离"（spec 裁定 5）的直接后果，改这个文件时别改回去：
1. 列表与详情都不按 user_id 过滤——谁开的会话全站都能看、都能发言。user_id 只表示
   归属，归属唯一的作用是"只有这个人能改名/归档"（_require_owner 一处把守）。
2. 归档只翻 chat_sessions.status：消息、附件、fts_shared 一行都不动。所以归档之后
   仍然搜得到、仍然能被引注——verify.verify_quote 只读 messages，压根不看 status。
3. 消息落库与进检索是同一次写（append_human 末尾的 index_message），不留窗口。
"""

from __future__ import annotations

import json
import sqlite3

from ..db import utcnow
from . import search


class NotOwner(Exception):
    """不是这场会话的归属者。路由翻成 403 + 人话。"""


def create_session(conn: sqlite3.Connection, user_id: int) -> int:
    """一点即建。标题留空，等首条消息来自动取名（append_human 里那半条 CASE）。

    last_message_at 也留空——还没人说话，这一列不该撒谎。它在排序上的后果由
    list_sessions 的 COALESCE 兜住。
    """
    cur = conn.execute(
        "INSERT INTO chat_sessions (user_id, title, status, started_at)"
        " VALUES (?, '', 'active', ?)",
        (user_id, utcnow()),
    )
    return int(cur.lastrowid)


def _last_outcome(conn: sqlite3.Connection, session_id: int) -> dict | None:
    """这场会话最近一条 outcome（runner 收尾写的 system 事件）的摘要；没有就 None。

    管理员日志页的「会话成果」用它（"3 页 · 18 引注"）。只取计数，不把整张 pages 列表
    塞进接口——日志是给扫的，不是给渲染的。事件行读不出来/形状不对就当没有（接口不能
    因为一列脏 JSON 整页 500，与 _parse_report 同一条纪律）。
    """
    row = conn.execute(
        "SELECT payload_json FROM agent_events"
        " WHERE session_id = ? AND type = 'system' AND payload_json LIKE '%outcome%'"
        " ORDER BY seq DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload_json"])
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("kind") != "outcome":
        return None
    pages = payload.get("pages") or []
    return {
        "pages": len(pages) if isinstance(pages, list) else 0,
        "citations": int(payload.get("citations") or 0),
        "lint_errors": payload.get("lint_errors"),
    }


def list_sessions(conn: sqlite3.Connection, owner_id: int | None = None) -> list[dict]:
    """会话列表，按最近动静倒序；归档的也在里面（带 status，前端自己压暗）。

    owner_id（2026-10-30，对话可见范围）：None = 全站（`all` 模式与管理员）；给了就只
    返回这个 user 自己的会话（`own` 模式下成员看到的列表）。过滤放在 SQL 里而不是取回来
    再筛：列表本来就是"扫一遍"的接口，把别人的会话读出来再丢掉没有意义。

    COALESCE 是承重的：新会话的 last_message_at 还是 NULL，而 SQLite 的 DESC 把 NULL
    排在最后。不兜这一下，「＋新会话」建出来的那条会掉到列表最底，人找不到自己刚开的会话。

    不再附带 last_outcome（2026-10-30 裁定）：对话列表是给人扫会话的，不该暴露
    "这一轮改了几页/几条引注"这种后台信息；计数只属于管理员日志页
    （见 list_session_outcomes 与 GET /api/log/outcomes）。
    """
    sql = (
        "SELECT s.*, u.name AS owner_name FROM chat_sessions s"
        " JOIN users u ON u.id = s.user_id"
    )
    params: tuple = ()
    if owner_id is not None:
        sql += " WHERE s.user_id = ?"
        params = (int(owner_id),)
    sql += " ORDER BY COALESCE(s.last_message_at, s.started_at) DESC, s.id DESC"
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def list_session_outcomes(conn: sqlite3.Connection) -> list[dict]:
    """管理员日志页的「会话成果」：有过 outcome 的会话 + 归属者 + 最近动静时间。

    与 list_sessions 分开，是因为两处的可见性不同（2026-10-30 裁定）：会话列表全员
    可看、且不再带后台计数；这份计数只随管理员日志下发。只留 last_outcome 非 None
    的会话——runner 对没动过 wiki 的轮次不写 outcome，没有成果的会话在日志里没有
    一行可显示。

    at 取 COALESCE(last_message_at, started_at)，与列表排序同一列：刚建还没说话的
    会话 last_message_at 是 NULL，日志行不能因此没有时间。排序同理按最近动静倒序。
    """
    rows = conn.execute(
        "SELECT s.id, s.title, s.status, u.name AS owner_name,"
        " COALESCE(s.last_message_at, s.started_at) AS at"
        " FROM chat_sessions s JOIN users u ON u.id = s.user_id"
        " ORDER BY COALESCE(s.last_message_at, s.started_at) DESC, s.id DESC"
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        outcome = _last_outcome(conn, int(row["id"]))
        if outcome is None:
            continue
        out.append({
            "id": int(row["id"]),
            "title": row["title"],
            "owner_name": row["owner_name"],
            "status": row["status"],
            "at": row["at"],
            "outcome": outcome,
        })
    return out


def _parse_report(raw: str | None) -> dict | None:
    """库里的 parse_report 是 JSON 文本，接口一律交对象。

    POST /api/chat/{id}/upload 的返回里 parse_report 就是解码后的 dict
    （uploads.save_upload），详情这边不跟着解码就成了同一个字段两种形状，
    前端得记着在哪一处 JSON.parse。坏 JSON 当没有报告——一条详情接口不该因为
    一列脏数据整页 500（与 search.index_page_slug 同口径）。
    """
    if not raw:
        return None
    try:
        report = json.loads(raw)
    except ValueError:
        return None
    return report if isinstance(report, dict) else None


def _parse_payload(raw: str | None) -> dict | None:
    """一条 agent_events 的 payload_json → 对象；解不出/不是对象 → None。

    与 _parse_report 同一条纪律：详情接口不因为一列脏数据整页 500。None 也不是
    "悄悄把这条事件抹掉"——事件行还在列表里，前端按 sse.ts 那套形状校验把它认成
    坏帧、摆一条「服务端帧坏了」的 err 横幅，与流上收到坏 payload 时同口径。
    """
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _message_events(conn: sqlite3.Connection, role: str, message_id: int) -> list[dict]:
    """这条消息的工具/系统/错误事件（裁定 R-S3-8），按 seq 升序。

    为什么要它：SSE 那条流上的 tool-call/tool-result/system/error 帧只活在流里，
    刷新页面之后工具卡片全没了（人只看见一段正文，看不出智能体查过什么、哪一步失败）。
    这些行本来就落在 agent_events 里，详情接口把它们按 message_id 交出去，前端就能用
    **同一套**帧语义把历史回合渲染出来。

    三条边界，都是有意的：
    - 只有 role='agent' 的行有事件；human 行一律交空列表（形状恒定，前端不必判 None）；
    - `message_id IS NULL` 的老行（迁移 0006 之前落的）**不**往任何一条消息上挂。
      SSE 尾读对 NULL 行有 created_at 的兜底口径，那是为了直播不断片；历史这边拿
      时间去猜归属，猜错就是把上一轮的工具卡片钉进这一轮的记录里，宁可不显示；
    - text-delta 不在这里面：正文就是 messages.content 那一列，不重复交一遍。

    type 原样透出（tool-call/tool-result/system/error，库里 CHECK 白名单）：将来加了
    新类型，认不认得是前端 toUiEvents 的事（不认得的返空数组），接口不筛不改名。

    按 message_id 查是每条消息一次 SELECT，与下面附件那次同一个 N+1 形状；
    7 人规模下一场会话几十条消息，无所谓，而迁移 0006 建的
    idx_agent_events_message_id 正是为这条查询准备的（那次迁移的注释写着"给 S3 用"）。
    """
    if role != "agent":
        return []
    rows = conn.execute(
        "SELECT type, payload_json FROM agent_events WHERE message_id = ? ORDER BY seq",
        (message_id,),
    )
    return [
        {"type": r["type"], "payload": _parse_payload(r["payload_json"])} for r in rows
    ]


def get_session(conn: sqlite3.Connection, session_id: int) -> dict | None:
    """一场会话 + 它的消息（按 seq）+ 每条消息挂着的附件元数据与事件。不存在返 None。

    附件只挑前端要显示的五列：id / orig_filename / size / parse_status / parse_report。
    stored_path 与 sha256 不给——存盘名是 uuid，泄出去只是让人能猜目录结构；
    原件也不提供下载（2026-10-30 裁定）：人类面能读的是解析正文与 wiki 页。

    events（裁定 R-S3-8）：每条消息一行 `{"type": …, "payload": <解码后的对象>}`，
    agent 行是它这一轮的 agent_events，其余行恒为空列表。见 _message_events。
    """
    row = conn.execute("SELECT * FROM chat_sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        return None
    msgs = conn.execute(
        "SELECT m.*, u.name AS user_name FROM messages m"
        " LEFT JOIN users u ON u.id = m.user_id WHERE m.session_id = ?"
        " ORDER BY m.seq", (session_id,),
    ).fetchall()
    out = []
    for m in msgs:
        d = dict(m)
        d["attachments"] = [
            {**dict(a), "parse_report": _parse_report(a["parse_report"])}
            for a in conn.execute(
                "SELECT id, orig_filename, size, parse_status, parse_report"
                " FROM upload_files WHERE message_id = ?", (m["id"],)
            )
        ]
        d["events"] = _message_events(conn, m["role"], int(m["id"]))
        out.append(d)
    return {**dict(row), "messages": out}


def session_row(conn: sqlite3.Connection, session_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM chat_sessions WHERE id = ?", (session_id,)
    ).fetchone()


def _require_owner(conn: sqlite3.Connection, session_id: int, user_id: int) -> None:
    """归属闸。查无此会话也并进来（NotOwner）：服务层不许拿一个不存在的 id 往下走。

    路由那边先用 session_row 判在不在，把"不存在"翻成 404、"人不对"翻成 403；
    这一条是给直接调服务的调用方（S2 的智能体工具）兜底的。
    """
    row = conn.execute("SELECT user_id FROM chat_sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None or row["user_id"] != user_id:
        raise NotOwner()


def rename(conn: sqlite3.Connection, session_id: int, user_id: int, title: str) -> None:
    """改名。只有归属者能改；标题去空白后不许为空，最长 60 字。

    空标题不是"改回未命名"：它会把 append_human 里那半条 `CASE WHEN title = ''`
    重新武装起来，下一条消息又把标题改成它的前 20 字，人明明改过名却看着标题自己变。
    60 字是侧栏一行的量；截断而不是报错——多打的字丢掉无伤，改名失败才恼人。
    """
    _require_owner(conn, session_id, user_id)
    title = (title or "").strip()
    if not title:
        raise ValueError("标题不能是空的")
    conn.execute("UPDATE chat_sessions SET title = ? WHERE id = ?", (title[:60], session_id))


def archive(conn: sqlite3.Connection, session_id: int, user_id: int) -> None:
    """归档 = 软删除（spec：会话只可归档，不做删除）。只有归属者能归档。

    只翻 status，消息/附件/fts_shared 一行都不动——归档之后仍可搜索、仍可被引注。
    归档是**可逆**的（2026-10-30 加 unarchive）：侧栏把归档会话收进折叠区，随时可取消。
    """
    _require_owner(conn, session_id, user_id)
    conn.execute("UPDATE chat_sessions SET status = 'archived' WHERE id = ?", (session_id,))


def unarchive(conn: sqlite3.Connection, session_id: int, user_id: int) -> None:
    """取消归档：回到活跃列表。只有归属者能操作；幂等（已 active 再调也 200）。

    只翻 status，不动消息/标题/最近动静——恢复之后它还是原来那一场会话。
    """
    _require_owner(conn, session_id, user_id)
    conn.execute("UPDATE chat_sessions SET status = 'active' WHERE id = ?", (session_id,))


def append_human(
    conn: sqlite3.Connection, session_id: int, user_id: int, content: str
) -> int:
    content = content.strip()
    if not content:
        raise ValueError("空消息不入库")
    # seq 用单条 INSERT ... SELECT 原子定（裁定 R68）：COALESCE(MAX(seq),0)+1 与插入
    # 在同一条写语句里算完。SQLite 下单条写语句自始至终持写锁，读 MAX 与写 INSERT
    # 之间插不进第二个写者，所以并发的两条各自看到对方已提交的 seq，永不撞
    # UNIQUE(session_id, seq)。旧的两步（先 SELECT MAX 再 INSERT）两语句间没有锁，
    # 在"全员都能往任何会话发言"变成一等公民之后，多人挤同一场会话就会读到同一个
    # seq、第二个 INSERT 撞唯一键 → 500。不带 GROUP BY 的聚合 SELECT 恒返一行
    # （空会话时 MAX 为 NULL，COALESCE 兜成 1），因此这条恰好插一行，lastrowid 可靠。
    cur = conn.execute(
        "INSERT INTO messages (session_id, seq, role, user_id, content, created_at)"
        " SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ?, ?"
        " FROM messages WHERE session_id = ?",
        (session_id, "human", user_id, content, utcnow(), session_id),
    )
    conn.execute(
        "UPDATE chat_sessions SET last_message_at = ?, title = CASE WHEN title = '' THEN ? ELSE title END"
        " WHERE id = ?",
        (utcnow(), content[:20], session_id),
    )
    search.index_message(conn, int(cur.lastrowid))
    return int(cur.lastrowid)


def touch(conn: sqlite3.Connection, session_id: int) -> None:
    """只把 last_message_at 推到当下，不动标题。

    给 S2 的智能体回复用：那条 agent 消息不经 append_human（它有 usage_json 要写、
    要能流式续写），列表的排序却看这一列。本任务里没有调用方——human 消息那一次
    append_human 已经顺手做了，所以这条现在只有 S2 会用到。
    """
    conn.execute("UPDATE chat_sessions SET last_message_at = ? WHERE id = ?", (utcnow(), session_id))
