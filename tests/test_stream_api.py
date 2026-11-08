"""SSE 端点与触发（Task 5）：GET /api/chat/{id}/stream 尾读数据面，POST messages 起一轮跑。

数据面是 runner 与 hooks 落的两张表——messages 里那条 role='agent' 的行（正文随 text-delta
一段段长出来）与 agent_events（tool-call/tool-result/system/error 行）。这个文件测的是
"从**另一条**连接尾读它们"的那一头：帧的顺序与形状、id 空间、Last-Event-ID 续读、
不成对的 tool-call、匿名 401、同会话重入 409（R-S2-1）。

假上游不另写一份：mock OpenAI 服务器与脚本 transport 的那套助手从 tests/test_runner_stream.py
import 过来——那边是"真装配 + 假上游"的线格式，这边是"真装配 + 假上游 + 真 HTTP 面"，
两边的上游是同一个。

conftest 的 autouse fixture 把 chat._start_turn 换成空转（不然每条 POST messages 的用例
都会拿假 key 往真上游发一次请求），所以下面凡是要真跑一轮的，都带 real_agent 这条 fixture。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import httpx
import pytest

from app.agent import hooks, runner
from app.agent.tools import WIKI_TOOLS
from app.db import connect, utcnow
from app.routers.api import chat as chat_router
from tests.conftest import api_login, make_human_message
from tests.test_runner_stream import (  # noqa: F401 —— 前三条是 fixture，import 进来才在本模块生效
    HUMAN,
    TURN1_TEXT,
    TURN2_TEXT,
    _chunk_line,
    _point_llm_at,
    _read,
    _restore_settings,
    _run,
    _ScriptedStream,
    _tool_call_line,
    fresh_db,
    mock_openai,
)

SEARCH_ARGS = {"query": "客服投诉", "scope": "all"}
MEMBER_PHONE = "13800000001"
MEMBER_PASSWORD = "member-local-1"


# --- 读帧的小工具 --------------------------------------------------------------


def _parse_frame(raw: str) -> dict:
    """一帧的文本 → {"id": int, "event": str, "data": dict}。三行缺一不可（spec §6.5）。"""
    got: dict[str, str] = {}
    for line in raw.split("\n"):
        if not line:
            continue
        key, _, value = line.partition(": ")
        got[key] = value
    assert set(got) == {"id", "event", "data"}, raw
    return {"id": int(got["id"]), "event": got["event"], "data": json.loads(got["data"])}


def _frames(resp):
    """把一条 SSE 响应拆成一帧一帧。

    按 "\\n\\n" 切、不按 chunk 切：TestClient 一次交出来多少字节是不保证的（一帧可能被
    拆开、两帧也可能并在一起），自己攒缓冲区才稳。
    """
    buf = ""
    for text in resp.iter_text():
        buf += text
        while "\n\n" in buf:
            raw, buf = buf.split("\n\n", 1)
            yield _parse_frame(raw)


def _wait_for(predicate, timeout: float = 60.0, what: str = "") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"等超时了：{what}")


def _ids(frames: list[dict]) -> list[int]:
    return [frame["id"] for frame in frames]


def _run_a_turn(sid: int, user_id: int) -> None:
    """在当前线程里跑完一整轮（真 runner + mock 上游），跑完这一轮已封口。"""
    writer = connect()
    try:
        _run(runner.run_turn(writer, sid, {"id": user_id, "name": "张三"}))
    finally:
        writer.close()


def _agent_row(sid: int) -> dict:
    rows = _read(
        "SELECT id, content, usage_json, truncated FROM messages"
        " WHERE session_id = ? AND role = 'agent' ORDER BY seq", (sid,)
    )
    assert len(rows) == 1, rows
    return rows[0]


# --- 脚本上游：第一轮按脚本走，第二轮先等一道闸 ----------------------------------


class _GatedTransport(httpx.AsyncBaseTransport):
    """第一个请求按脚本走，后面的请求先等一道闸再回一段纯文本收尾。

    与 test_runner_stream 的 _ScriptedTransport 只差那道闸。帧顺序那条测试要用它把
    "工具结果已落库、第二轮正文还没来"这个瞬间钉住：不钉的话两件事会落进尾读的同一拍，
    而同一拍里正文先于事件（尾读的固有近似，见 chat._tail 的注释），断言出来的就成了
    尾读的猜测，而不是这一轮真的发生顺序。
    """

    def __init__(self, steps: list, gate: threading.Event):
        self._steps = steps
        self._gate = gate
        self._served = False

    async def handle_async_request(self, request):
        if not self._served:
            self._served = True
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                stream=_ScriptedStream(self._steps),
            )
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._gate.wait, 60)
        tail = b"".join([
            _chunk_line({"role": "assistant", "content": TURN2_TEXT[0]}),
            _chunk_line({"content": TURN2_TEXT[1]}),
            _chunk_line({}, finish="stop"),
            b"data: [DONE]\n\n",
        ])
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "content-length": str(len(tail))},
            content=tail,
        )


def _wait_step(gate: threading.Event):
    """脚本里的一步：卡在这儿，直到测试那头放行（60 秒兜底，别把套件挂死）。"""

    async def wait():
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, gate.wait, 60)

    return wait


# --- 帧顺序：一轮真跑打在真 HTTP 面上 --------------------------------------------


def test_frames_come_in_the_order_the_run_produced_them(
    fresh_db, client, monkeypatch, real_agent
):
    """POST 触发一轮跑，流上看到的顺序就是这一轮真的发生顺序。

    text-delta… → tool-call → tool-result → text-delta… → done，id 严格递增，
    done 之后流自己关掉（尾读不是挂在那儿不断开的）。
    """
    sid = fresh_db["session_id"]
    api_login(client, MEMBER_PHONE, MEMBER_PASSWORD)
    text_read = threading.Event()
    tail_gate = threading.Event()
    steps = [
        _chunk_line({"role": "assistant", "content": TURN1_TEXT[0]}),
        _chunk_line({"content": TURN1_TEXT[1]}),
        _wait_step(text_read),                     # 等测试读完那段正文再要工具
        _tool_call_line("search", SEARCH_ARGS),
        _chunk_line({}, finish="tool_calls"),
    ]
    _point_llm_at(monkeypatch, steps, transport=_GatedTransport(steps, tail_gate))

    posted = client.post(f"/api/chat/{sid}/messages", data={"content": HUMAN})
    assert posted.status_code == 200, posted.text

    seen: list[dict] = []
    with client.stream("GET", f"/api/chat/{sid}/stream") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["cache-control"] == "no-cache"
        frames = _frames(resp)

        # 工具之前那段正文。可能一帧也可能几帧（一拍一帧），所以攒到撞上别的类型为止。
        head = ""
        while True:
            frame = next(frames)
            seen.append(frame)
            if frame["event"] != "text-delta":
                break
            head += frame["data"]["text"]
            text_read.set()                        # 读到了就放行上游（Event.set 幂等）
        assert head == "".join(TURN1_TEXT)

        assert frame["event"] == "tool-call"
        assert frame["data"]["name"] == "search"
        assert json.loads(frame["data"]["args"]) == SEARCH_ARGS
        # R-S2-21：payload 带 SDK 的 tool_call_id（spec §6.5 的 id 键）
        assert frame["data"]["id"] == "call_1"

        result = next(frames)
        seen.append(result)
        assert result["event"] == "tool-result"
        assert result["data"]["name"] == "search"
        assert result["data"]["ok"] is True
        assert result["data"]["summary"].startswith("搜到")
        assert result["data"]["id"] == "call_1"      # 与 tool-call 同 id，S3 按它配对

        tail_gate.set()                            # 到此才放第二轮的正文出来
        tail = ""
        while True:
            frame = next(frames)
            seen.append(frame)
            if frame["event"] != "text-delta":
                break
            tail += frame["data"]["text"]
        assert tail == "".join(TURN2_TEXT)

        assert frame["event"] == "done"
        assert frame["data"] == {"message_id": _agent_row(sid)["id"], "usage": None}
        assert next(frames, None) is None, "done 之后流该关掉"

    assert [f["event"] for f in seen] == [
        "text-delta", "tool-call", "tool-result", "text-delta", "done",
    ], [f["event"] for f in seen]
    ids = _ids(seen)
    assert ids == sorted(set(ids)), ids            # 严格递增，不重号


# --- 尾读的收尾与重放 -----------------------------------------------------------


def test_a_finished_turn_is_replayed_then_the_stream_closes(fresh_db, mock_openai, client,
                                                            real_agent):
    """连上来时这一轮已经跑完：整条重放一遍（正文合成一帧 + 事件行 + done），然后关。

    这一条同时钉住"重放时正文先于事件"：数据面只留了正文的终态与事件的 seq，
    没留"事件落库那一刻正文有多长"，所以事后重放拿不回工具前后那两段的分界
    （见 chat._tail 的注释与 task-5-report.md）。
    """
    sid = fresh_db["session_id"]
    api_login(client, MEMBER_PHONE, MEMBER_PASSWORD)
    mock_openai.reset(tool_name="search", tool_args=SEARCH_ARGS)
    _run_a_turn(sid, fresh_db["user_id"])
    row = _agent_row(sid)

    with client.stream("GET", f"/api/chat/{sid}/stream") as resp:
        assert resp.status_code == 200
        seen = list(_frames(resp))

    assert [f["event"] for f in seen] == ["text-delta", "tool-call", "tool-result", "done"]
    assert seen[0]["data"] == {"text": row["content"]}
    assert row["content"] == "".join(TURN1_TEXT) + "".join(TURN2_TEXT)
    assert seen[1]["data"]["name"] == "search"
    assert json.loads(seen[1]["data"]["args"]) == SEARCH_ARGS
    assert seen[2]["data"]["ok"] is True
    # R-S2-21：事件帧的 data 就是 payload_json 原样，里头带 call id 且 call/result 同 id
    assert seen[1]["data"]["id"] == seen[2]["data"]["id"] == "call_1"
    assert seen[3]["data"] == {"message_id": row["id"], "usage": None}
    assert _ids(seen) == sorted(set(_ids(seen))), _ids(seen)


def test_a_tool_call_without_its_result_still_ends_at_done(fresh_db, client, conn, real_agent):
    """tool-call 没有配对的 tool-result，尾读照样发 done 收工，不等那一对。

    真跑出来就是这个形状：工具在飞的时候这一轮被取消、或白名单拒掉了在飞的兄弟工具，
    SDK 就不会调 on_tool_end（T3 留给 T5 的那句话）。这里直接把库摆成那个形状
    （封口用失败路径的那一种：usage_json NULL + truncated=1），测的是尾读的容忍度。
    """
    sid = fresh_db["session_id"]
    api_login(client, MEMBER_PHONE, MEMBER_PASSWORD)
    cur = conn.execute(
        "INSERT INTO messages (session_id, seq, role, content, created_at, truncated)"
        " SELECT ?, COALESCE(MAX(seq), 0) + 1, 'agent', '我去查一下。', ?, 0"
        " FROM messages WHERE session_id = ?",
        (sid, utcnow(), sid),
    )
    mid = int(cur.lastrowid)
    hooks.append_event(conn, sid, "tool-call", {"name": "search", "args": "{}"})
    conn.execute("UPDATE messages SET usage_json = NULL, truncated = 1 WHERE id = ?", (mid,))

    with client.stream("GET", f"/api/chat/{sid}/stream") as resp:
        seen = list(_frames(resp))

    assert [f["event"] for f in seen] == ["text-delta", "tool-call", "done"], seen
    assert seen[-1]["data"] == {"message_id": mid, "usage": None}


# --- 事件归到具体的一条 agent 消息（裁定 R-S2-20，迁移 0006） ----------------------


def test_same_second_turns_do_not_replay_the_previous_turns_events(
    fresh_db, mock_openai, client
):
    """两轮挤在同一秒里：第二轮的流**只**发第二轮的事件，不重发第一轮的尾巴。

    旧口径拿 `created_at >= 当前 agent 行的 created_at` 划一轮的边界，而 created_at 是
    秒级（db.utcnow）——mock 上游一轮是毫秒级，两轮几乎必然同秒，旧口径下第一轮的
    tool-call/tool-result 会再发一遍（前端多出一张属于上一轮的工具卡片）。现在按
    `message_id = 当前 agent 行的 id` 过滤；为了不把结论押在"恰好没跨秒"的运气上，
    跑完两轮之后把两张表的 created_at 统一改写成同一个值，**确保**踩中旧 bug 的
    触发条件（把 message_id 过滤拿掉，这条必红）。
    """
    sid = fresh_db["session_id"]
    api_login(client, MEMBER_PHONE, MEMBER_PASSWORD)
    mock_openai.reset(tool_name="search", tool_args=SEARCH_ARGS)
    _run_a_turn(sid, fresh_db["user_id"])
    # Handler 的剧本按"第几个请求"走（turn==1 才出工具），不清状态的话第二轮没有工具事件
    mock_openai.reset(tool_name="search", tool_args=SEARCH_ARGS)
    _run_a_turn(sid, fresh_db["user_id"])

    rows = _read(
        "SELECT id FROM messages WHERE session_id = ? AND role = 'agent' ORDER BY seq", (sid,)
    )
    assert len(rows) == 2, rows
    msg1, msg2 = rows[0]["id"], rows[1]["id"]
    events = _read(
        "SELECT seq, type, message_id FROM agent_events WHERE session_id = ? ORDER BY seq",
        (sid,),
    )
    assert [e["type"] for e in events] == ["tool-call", "tool-result"] * 2, events
    # 接线本身就是被测的一半：runner 把开口的 message_id 交进 AgentContext，hooks 写进列
    assert [e["message_id"] for e in events] == [msg1, msg1, msg2, msg2], events
    turn2_seqs = [e["seq"] for e in events if e["message_id"] == msg2]

    # 强制同秒：把两张表的 created_at 全改成同一个值（旧口径串轮子的必要条件）
    ts = utcnow()
    writer = connect()
    try:
        writer.execute("UPDATE messages SET created_at = ? WHERE session_id = ?", (ts, sid))
        writer.execute("UPDATE agent_events SET created_at = ? WHERE session_id = ?", (ts, sid))
        writer.commit()
    finally:
        writer.close()

    with client.stream("GET", f"/api/chat/{sid}/stream") as resp:
        seen = list(_frames(resp))

    # 尾读钉住最新那条 agent 行（msg2）：正文是它的，事件也只有归它的那两条。
    # 事件帧的 id 高位就是 agent_events.seq（chat._frame_id），据此核对发了哪几条。
    kinds = ("tool-call", "tool-result", "system", "error")
    got_seqs = [f["id"] // chat_router.ID_STRIDE for f in seen if f["event"] in kinds]
    assert got_seqs == turn2_seqs, seen
    assert [f["event"] for f in seen] == ["text-delta", "tool-call", "tool-result", "done"], seen
    assert seen[-1]["data"] == {"message_id": msg2, "usage": None}


def test_legacy_null_message_id_events_fall_back_to_the_created_at_rule(
    fresh_db, client, conn
):
    """legacy 兼容：没有 message_id 的老行（NULL）仍按 created_at 口径投递。

    迁移 0006 之前落的事件行没有归属可补，尾读对它们保留旧规则——`created_at >=
    当前 agent 行的 created_at` 就算这一轮的，更早的照旧挡在外面；新写的事件一律带
    message_id（hooks 与 runner._event 都传），NULL 行只会越来越少。时间戳全用写死的
    常量，断言不押在真实时钟上。
    """
    sid = fresh_db["session_id"]
    api_login(client, MEMBER_PHONE, MEMBER_PASSWORD)
    ts, older = "2026-10-22T10:00:00+00:00", "2026-10-22T09:59:59+00:00"
    cur = conn.execute(
        "INSERT INTO messages (session_id, seq, role, content, created_at, truncated,"
        " usage_json) SELECT ?, COALESCE(MAX(seq), 0) + 1, 'agent', '好。', ?, 0, '{}'"
        " FROM messages WHERE session_id = ?",
        (sid, ts, sid),
    )
    mid = int(cur.lastrowid)

    def _event(event_type: str, payload: dict, created_at: str, message_id: int | None) -> None:
        conn.execute(
            "INSERT INTO agent_events (session_id, seq, type, payload_json, created_at,"
            " message_id) SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ?, ?"
            " FROM agent_events WHERE session_id = ?",
            (sid, event_type, json.dumps(payload, ensure_ascii=False), created_at, message_id,
             sid),
        )

    # 同秒的 NULL 老行 → 旧规则判"属于这一轮"，照发
    _event("tool-call", {"id": "c0", "name": "search"}, ts, None)
    # 更早的 NULL 老行 → 旧规则照样挡在外面
    _event("tool-result", {"id": "c0", "name": "search", "ok": True, "summary": "旧"}, older, None)
    # 归**别的**消息的行，就算 created_at 同秒也不发（message_id 口径优先于时间）
    _event("tool-result", {"id": "c9", "name": "search", "ok": True, "summary": "别人的"},
           ts, mid + 1000)

    with client.stream("GET", f"/api/chat/{sid}/stream") as resp:
        seen = list(_frames(resp))

    assert [f["event"] for f in seen] == ["text-delta", "tool-call", "done"], seen
    assert seen[1]["data"] == {"id": "c0", "name": "search"}
    assert seen[-1]["data"] == {"message_id": mid, "usage": None}


def test_the_tail_waits_for_a_turn_that_has_not_started_yet(fresh_db, monkeypatch):
    """连上来时这场会话还没有 agent 行（前端先连流、再发消息的顺序）：等着，不退出也不报错。

    直接驱动 _tail 而不走 HTTP：一条 TestClient 的流读起来是阻塞的，测试没有第二个
    线程能一边读流一边发消息。顺带把帧的线格式逐字钉住（id/event/data 三行 + 空行收尾）。
    """
    sid = fresh_db["session_id"]
    monkeypatch.setattr(chat_router, "POLL_SECONDS", 0.02)

    async def scenario() -> list[str]:
        got: list[str] = []

        async def collect():
            async for frame in chat_router._tail(sid, 0):
                got.append(frame)

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.2)                   # 这几拍库里还什么都没有
        assert got == [], got
        writer = connect()
        try:
            cur = writer.execute(
                "INSERT INTO messages (session_id, seq, role, content, created_at, truncated,"
                " usage_json) SELECT ?, COALESCE(MAX(seq), 0) + 1, 'agent', '', ?, 0, NULL"
                " FROM messages WHERE session_id = ?",
                (sid, utcnow(), sid),
            )
            mid = int(cur.lastrowid)
            hooks.append_event(writer, sid, "system", {"text": runner.QUOTA_TEXT})
            writer.execute(
                "UPDATE messages SET usage_json = NULL, truncated = 1 WHERE id = ?", (mid,)
            )
            writer.commit()
        finally:
            writer.close()
        await asyncio.wait_for(task, 10)
        return got

    frames = asyncio.run(scenario())
    assert len(frames) == 2, frames
    assert frames[0].startswith("id: ") and frames[0].endswith("\n\n")
    assert "\nevent: system\ndata: " in frames[0]
    assert _parse_frame(frames[0])["data"] == {"text": runner.QUOTA_TEXT}
    assert "\nevent: done\ndata: " in frames[1]


def test_the_tail_gives_up_after_the_wall_clock_ceiling(fresh_db, conn, monkeypatch):
    """I6：一轮永远不封口（进程被杀留下的半截行）时，流不许无限期挂着。

    到 STREAM_TIMEOUT_SECONDS 就发一帧 error{code=stream_timeout} 再关。上限收进模块
    常量，monkeypatch 成毫秒级即可触发；那行摆成"还在跑"的唯一记号
    （usage_json NULL + truncated=0），尾读的 done 判据永远不满足。
    """
    sid = fresh_db["session_id"]
    monkeypatch.setattr(chat_router, "POLL_SECONDS", 0.02)
    monkeypatch.setattr(chat_router, "STREAM_TIMEOUT_SECONDS", 0.3)
    conn.execute(
        "INSERT INTO messages (session_id, seq, role, content, created_at, truncated,"
        " usage_json) SELECT ?, COALESCE(MAX(seq), 0) + 1, 'agent', '半截话', ?, 0, NULL"
        " FROM messages WHERE session_id = ?",
        (sid, utcnow(), sid),
    )

    async def scenario() -> list[str]:
        got: list[str] = []
        async for frame in chat_router._tail(sid, 0):
            got.append(frame)
        return got

    # wait_for 是"不许挂死"的兜底：上限生效的话 _tail 约 0.3s 就自己收工
    frames = _run(asyncio.wait_for(scenario(), 10))
    assert frames, "至少该发一帧（超时 error 帧）"
    last = _parse_frame(frames[-1])
    assert last["event"] == "error", frames
    assert last["data"]["code"] == "stream_timeout"
    assert any("\u4e00" <= ch <= "\u9fff" for ch in last["data"]["text"]), last["data"]
    # 超时前把已流出的半截正文照常发了（text-delta），最后一帧才是 error
    assert _parse_frame(frames[0])["event"] == "text-delta"


# --- after=<message_id>：只认比它新的那一轮（裁定 R-S2-30） ----------------------


def test_after_query_skips_an_older_sealed_turn_and_replays_the_newer(fresh_db, client, conn):
    """I1 / R-S2-30：GET /stream?after=<人话 id> 只认 seq 大于那条消息的 agent 行。

    场景就是前端配套的正确用法：POST 拿到人话 id，再用它开流。库里已有一轮封好的
    旧回复（turn A），以及刚发的这条人话（H2）之后跑出来的一轮新回复（turn B）。
    带 after=H2 → 只重放 turn B，旧 turn A 一个字都不出现；不带 after → 老行为不变
    （仍钉住最新一条 agent 行 = turn B）。
    """
    sid = fresh_db["session_id"]
    api_login(client, MEMBER_PHONE, MEMBER_PASSWORD)

    def seal_agent(content: str) -> int:
        cur = conn.execute(
            "INSERT INTO messages (session_id, seq, role, content, created_at, truncated,"
            " usage_json) SELECT ?, COALESCE(MAX(seq), 0) + 1, 'agent', ?, ?, 0, '{}'"
            " FROM messages WHERE session_id = ?",
            (sid, content, utcnow(), sid),
        )
        return int(cur.lastrowid)

    seal_agent("这是旧的一轮回复。")                                   # turn A（seq 2）
    h2 = make_human_message(conn, sid, fresh_db["user_id"], "再问一句")  # 人话 H2（seq 3）
    new_id = seal_agent("这是新的一轮回复。")                           # turn B（seq 4）

    # 带 after=H2：只认 seq > 3 的 agent 行 → 只重放 turn B
    with client.stream("GET", f"/api/chat/{sid}/stream", params={"after": h2}) as resp:
        assert resp.status_code == 200
        seen = list(_frames(resp))
    assert [f["event"] for f in seen] == ["text-delta", "done"], seen
    assert seen[0]["data"] == {"text": "这是新的一轮回复。"}
    assert seen[-1]["data"] == {"message_id": new_id, "usage": None}
    assert "旧的一轮" not in json.dumps(seen, ensure_ascii=False), "旧 turn 不该被重放"

    # 不带 after：老行为不变，仍钉住最新一条 agent 行（同样是 turn B）
    with client.stream("GET", f"/api/chat/{sid}/stream") as resp:
        plain = list(_frames(resp))
    assert plain[-1]["data"] == {"message_id": new_id, "usage": None}


def test_after_waits_for_a_newer_turn_instead_of_replaying_the_old_one(
    fresh_db, client, conn, monkeypatch
):
    """after 的另一半语义：新一轮还没开口时**等着**，而不是退回去重放旧轮。

    只有一轮封好的旧回复（turn A）与刚发的人话 H2。带 after=H2 连上来时，比 H2 新的
    agent 行还不存在，尾读该挂着等（SSE 长连接）；等 turn B 落库并封口，才把它发出来。
    不带 after 的老行为则是立刻重放旧 turn A 再关（这正是 after 要修的形状）。

    等待那一段直接驱动 _tail（传 after_seq），与 test_the_tail_waits… 同一个手法：
    TestClient 的流读起来是阻塞的，测试没有第二个线程能一边读流一边落库。
    """
    sid = fresh_db["session_id"]
    api_login(client, MEMBER_PHONE, MEMBER_PASSWORD)
    monkeypatch.setattr(chat_router, "POLL_SECONDS", 0.02)

    def seal_agent(content: str) -> int:
        cur = conn.execute(
            "INSERT INTO messages (session_id, seq, role, content, created_at, truncated,"
            " usage_json) SELECT ?, COALESCE(MAX(seq), 0) + 1, 'agent', ?, ?, 0, '{}'"
            " FROM messages WHERE session_id = ?",
            (sid, content, utcnow(), sid),
        )
        return int(cur.lastrowid)

    old_id = seal_agent("这是旧的一轮回复。")                          # turn A（seq 2）
    h2 = make_human_message(conn, sid, fresh_db["user_id"], "再问一句")  # 人话 H2（seq 3）
    h2_seq = conn.execute("SELECT seq FROM messages WHERE id = ?", (h2,)).fetchone()["seq"]

    # 不带 after（走 HTTP）：立刻重放旧 turn A（老行为，正是 after 要修的形状）
    with client.stream("GET", f"/api/chat/{sid}/stream") as resp:
        plain = list(_frames(resp))
    assert plain[-1]["data"] == {"message_id": old_id, "usage": None}

    # 带 after_seq（直接驱动 _tail）：比 H2 新的 agent 行还没有 → 尾读等着，不发旧 turn
    async def scenario() -> list[str]:
        got: list[str] = []

        async def collect():
            async for frame in chat_router._tail(sid, 0, h2_seq):
                got.append(frame)

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.2)                   # 这几拍 after 挡着旧 turn，流上什么都没有
        assert got == [], got
        new_id = seal_agent("这是新的一轮回复。")   # turn B 落库并封口（seq 4 > h2_seq）
        await asyncio.wait_for(task, 10)
        return got, new_id

    frames, new_id = _run(scenario())
    assert len(frames) == 2, frames
    parsed = [_parse_frame(f) for f in frames]
    assert [p["event"] for p in parsed] == ["text-delta", "done"], parsed
    assert parsed[0]["data"] == {"text": "这是新的一轮回复。"}
    assert parsed[-1]["data"] == {"message_id": new_id, "usage": None}



def test_after_pointing_outside_the_session_is_400(fresh_db, client):
    """after 指着不存在/不属于这场会话的消息：400 JSON，在返回 StreamingResponse 之前判掉。"""
    sid = fresh_db["session_id"]
    api_login(client, MEMBER_PHONE, MEMBER_PASSWORD)
    resp = client.get(f"/api/chat/{sid}/stream", params={"after": 999999})
    assert resp.status_code == 400, resp.text
    assert resp.json() == {"error": "after 指着的消息不在这场会话里"}
    assert resp.headers["content-type"].startswith("application/json")


# --- Last-Event-ID 续读 ---------------------------------------------------------


def test_last_event_id_resumes_after_the_frame_the_client_already_saw(
    fresh_db, mock_openai, client, real_agent
):
    """续读的两半都要对：正文的偏移与事件的 seq。

    id 是一个数（Last-Event-ID 只有一个），所以它编的是游标 (seq, offset)：
    高位 = 已发过的最大 agent_events.seq，低位 = 已发过的正文字数（chat._frame_id）。
    """
    sid = fresh_db["session_id"]
    api_login(client, MEMBER_PHONE, MEMBER_PASSWORD)
    mock_openai.reset(tool_name="search", tool_args=SEARCH_ARGS)
    _run_a_turn(sid, fresh_db["user_id"])
    row = _agent_row(sid)
    url = f"/api/chat/{sid}/stream"

    with client.stream("GET", url) as resp:
        whole = list(_frames(resp))
    text_frame, call_frame, result_frame, done_frame = whole

    # 从 tool-call 那帧之后续：正文与 tool-call 都不再重发，只剩 tool-result 与 done
    with client.stream("GET", url, headers={"Last-Event-ID": str(call_frame["id"])}) as resp:
        resumed = list(_frames(resp))
    assert [f["event"] for f in resumed] == ["tool-result", "done"], resumed
    assert resumed[0]["data"] == result_frame["data"]
    assert resumed[-1]["data"] == done_frame["data"]
    assert all(f["id"] > call_frame["id"] for f in resumed), _ids(resumed)

    # 只有正文偏移（高位是 0）：正文从第 5 个字起接着发，事件一条不漏
    with client.stream("GET", url, headers={"Last-Event-ID": "5"}) as resp:
        partial = list(_frames(resp))
    assert [f["event"] for f in partial] == [
        "text-delta", "tool-call", "tool-result", "done",
    ], partial
    assert partial[0]["data"] == {"text": row["content"][5:]}
    assert partial[1]["data"] == call_frame["data"]

    # 解不动的 Last-Event-ID 当 0 处理：整条重放，不是 4xx、也不是空流
    with client.stream("GET", url, headers={"Last-Event-ID": "not-a-number"}) as resp:
        assert resp.status_code == 200
        assert _ids(list(_frames(resp))) == _ids(whole)


def test_a_stale_cursor_from_a_longer_previous_turn_still_delivers_the_next_reply(
    fresh_db, mock_openai, client
):
    """跨轮的陈旧 Last-Event-ID 不许把下一轮回复的开头**永久跳过**（裁定 R-S2-22）。

    id 里没有 message_id，所以一个 Last-Event-ID 只在"同一条 agent 消息"内有意义。
    浏览器 EventSource 的默认行为又恰恰是"连接断了就带着最后那个 id 重连"，而 done
    之后是服务端主动关流——于是下一轮开口时，前端手里那个 id 是**上一轮**铸的，
    offset 是上一轮的正文长度。上一轮比这一轮长时，`len(content) > offset` 永远不成立
    （正文只会长、offset 不会自己变小），这一轮的开头就再也不会发出来：不是延迟，
    是刷新之前一直缺一段。守卫是"offset > len(content) + 1 就归零重放本轮"。

    形状刻意摆成上一轮长（带工具的完整一轮 18 字）、下一轮短（纯文本 9 字），
    并且用**上一轮 done 帧的真 id** 重连，不手搓数字。
    """
    sid = fresh_db["session_id"]
    api_login(client, MEMBER_PHONE, MEMBER_PASSWORD)
    url = f"/api/chat/{sid}/stream"

    mock_openai.reset(tool_name="search", tool_args=SEARCH_ARGS)
    _run_a_turn(sid, fresh_db["user_id"])
    with client.stream("GET", url) as resp:            # 第一轮读到底，记下浏览器手里的 id
        first = list(_frames(resp))
    assert first[-1]["event"] == "done"
    done_id = first[-1]["id"]

    mock_openai.reset(mode="text_only")                # 第二轮只回一段短正文，没有工具事件
    _run_a_turn(sid, fresh_db["user_id"])
    rows = _read(
        "SELECT id, content FROM messages WHERE session_id = ? AND role = 'agent' ORDER BY seq",
        (sid,),
    )
    assert len(rows) == 2, rows
    msg1, msg2 = rows
    # 触发守卫的前提：陈旧 offset 确实超出了新一轮的正文长度（不然这条测试是空的）
    stale_offset = done_id % chat_router.ID_STRIDE
    assert len(msg1["content"]) > len(msg2["content"]), (msg1["content"], msg2["content"])
    assert stale_offset > len(msg2["content"]) + 1, (stale_offset, len(msg2["content"]))

    with client.stream("GET", url, headers={"Last-Event-ID": str(done_id)}) as resp:
        resumed = list(_frames(resp))

    # offset 归零 → 第二轮的正文**从头**发全（没有守卫时这里只剩一个 done）
    assert [f["event"] for f in resumed] == ["text-delta", "done"], resumed
    assert resumed[0]["data"] == {"text": msg2["content"]}
    assert resumed[0]["data"]["text"] == "".join(TURN2_TEXT)
    assert resumed[-1]["data"] == {"message_id": msg2["id"], "usage": None}
    assert _ids(resumed) == sorted(set(_ids(resumed))), _ids(resumed)   # 这一趟内部仍严格递增

    # 守卫的 +1 是给"done 之后的合法重连"留的：同一轮再拿 done 的 id 连一次，
    # 正文一个字都不该重放（前端会看见同一段话说两遍），只再收到一个 done。
    with client.stream("GET", url, headers={"Last-Event-ID": str(resumed[-1]["id"])}) as resp:
        again = list(_frames(resp))
    assert [f["event"] for f in again] == ["done"], again
    assert again[-1]["data"] == {"message_id": msg2["id"], "usage": None}


# --- id 空间的边界（裁定 R-S2-23） -------------------------------------------------


def test_a_last_event_id_that_only_looks_like_a_number_is_treated_as_zero():
    """"²"、"⑤" 一类：isdigit() 说 True，int() 抛 ValueError（裁定 R-S2-23）。

    按 isdigit 判就会把这种头当"能解"送进 int()，一条 500 顶在流上；解析统一走 int() +
    try。HTTP 头的值只能是 ASCII，所以这种字节走不到 client.stream 上，只能在这一层钉。
    """
    assert "²".isdigit() and "⑤".isdigit()             # 前提：isdigit 真的认它们
    assert chat_router._parse_last_event_id("²") == 0
    assert chat_router._parse_last_event_id("⑤") == 0
    assert chat_router._parse_last_event_id("not-a-number") == 0
    assert chat_router._parse_last_event_id("") == 0
    assert chat_router._parse_last_event_id("12") == 12
    # int() 比 isdigit 宽的那一侧（前后空白）也一并归 int() 说了算，口径只有一个
    assert chat_router._parse_last_event_id(" 12 ") == 12


def test_the_id_space_bounds_are_loud_instead_of_silently_colliding():
    """越界要响亮：offset 顶穿 ID_STRIDE 就会吃掉下一个 seq 的号段（两个游标同一个 id），
    seq 顶穿上界就会超过 JS 能精确表示的整数（前端 parseInt 悄悄掉位）。两种都抛。
    """
    stride = chat_router.ID_STRIDE
    assert chat_router._frame_id(3, 42) == 3 * stride + 42      # 正常路径没被闸拦着
    assert chat_router._decode_cursor(3 * stride + 42) == (3, 42)

    for seq, offset in ((0, stride), (1, stride + 7), (0, -1)):
        with pytest.raises(ValueError) as info:
            chat_router._frame_id(seq, offset)
        assert "越界" in str(info.value), str(info.value)
        assert any("\u4e00" <= ch <= "\u9fff" for ch in str(info.value)), str(info.value)

    with pytest.raises(ValueError) as info:
        chat_router._frame_id(chat_router._MAX_SEQ + 1, 0)
    assert "越界" in str(info.value), str(info.value)

    # 解码那一侧挡的是"这个数压根不在 id 空间里"：负数、或超过 _MAX_SAFE_ID
    for bad in (-1, chat_router._MAX_SAFE_ID + 1):
        with pytest.raises(ValueError) as info:
            chat_router._decode_cursor(bad)
        assert "id 空间" in str(info.value), str(info.value)


def test_an_out_of_range_last_event_id_is_400_before_the_stream_starts(
    fresh_db, client, conn, monkeypatch
):
    """R-S2-27：越界的 Last-Event-ID 在**路由入口**判掉（400 + /api 的 JSON 信封），
    不是流开起来之后在 _tail 里抛（那是 200 的半截流加一条 traceback，客户端没法看）。

    三种不合法各来一遍：

    1. 负数（人手搓的头）；
    2. >= 2^53（前端把 id 存进 JS Number 掉了精度之后就是这个量级的怪数）；
    3. 偏移分量 >= ID_STRIDE——这一种在今天的 floor division 下**数学上到不了**
       （余数恒小于除数），_decode_cursor 里那道闸是防御性的（合法游标的条件齐在
       代码里），所以用换掉 builtins.divmod 的办法触发：真触发时也得是同一条 400。

    两个不该被这道闸误伤的口径也钉在这儿：解不动的（"not-a-number"）仍当 0 整条重放
    （浏览器拿着垃圾重连是既定行为，不是错误）；上边界本身（_MAX_SAFE_ID = 2^53 - 1）
    是合法 id，照样流——它解出的 offset 远超任何正文长度，由 _tail 的陈旧游标守卫
    （R-S2-22）归零，本轮从头重放再发 done。
    """
    sid = fresh_db["session_id"]
    api_login(client, MEMBER_PHONE, MEMBER_PASSWORD)
    url = f"/api/chat/{sid}/stream"

    # 一条已封口的 agent 行：让"该流"的用例拿得到 done 自己收工（不然会一直等着）
    conn.execute(
        "INSERT INTO messages (session_id, seq, role, content, created_at, truncated,"
        " usage_json) SELECT ?, COALESCE(MAX(seq), 0) + 1, 'agent', '好。', ?, 0, '{}'"
        " FROM messages WHERE session_id = ?",
        (sid, utcnow(), sid),
    )

    # 1+2：负数与 >= 2^53，全是 400 JSON，不是流
    for bad in ("-1", str(-(2**53)), str(2**53), str(2**53 + 42)):
        resp = client.get(url, headers={"Last-Event-ID": bad})
        assert resp.status_code == 400, (bad, resp.text)
        assert resp.json() == {"error": "续读位置不合法"}
        assert resp.headers["content-type"].startswith("application/json")

    # 不该误伤的口径 A：解不动的仍当 0（整条重放），不落 400
    with client.stream("GET", url, headers={"Last-Event-ID": "not-a-number"}) as resp:
        assert resp.status_code == 200
        assert [f["event"] for f in _frames(resp)] == ["text-delta", "done"]

    # 不该误伤的口径 B：上边界 _MAX_SAFE_ID 本身合法，照流（陈旧游标守卫归零重放）。
    # 必须排在下面换掉 divmod 之前跑——换了之后连合法值也会被那道防御闸拦下。
    with client.stream(
        "GET", url, headers={"Last-Event-ID": str(chat_router._MAX_SAFE_ID)}
    ) as resp:
        assert resp.status_code == 200
        seen = list(_frames(resp))
    assert [f["event"] for f in seen] == ["text-delta", "done"], seen
    assert seen[0]["data"] == {"text": "好。"}

    # 3：把"按 ID_STRIDE 除"临时换成回一个越界偏移的假 divmod，触发那道防御闸。
    # 只劫持除数恰为 ID_STRIDE 的调用，别的除法原样走真的（不脏 TestClient 的内部路径）。
    real_divmod = divmod

    def broken_divmod(a, b):
        if b == chat_router.ID_STRIDE:
            return (0, chat_router.ID_STRIDE)      # offset 分量越界
        return real_divmod(a, b)

    monkeypatch.setattr("builtins.divmod", broken_divmod)
    resp = client.get(url, headers={"Last-Event-ID": "5"})
    assert resp.status_code == 400, resp.text
    assert resp.json() == {"error": "续读位置不合法"}


# --- 422 的中文兜底（裁定 R-S2-24） ------------------------------------------------


def test_a_path_param_that_is_not_a_number_says_the_format_is_wrong(client, fresh_db):
    """`*_parsing`（int/float 的 path/query 错）→「<field> 的格式不对」，字段名留着。

    GET /api/chat/abc/stream 是整个 API 面上最容易撞到的 422：路径上的 session_id 是
    int，"abc" 解不成数字。原来这种落到老口径，人拿到的是一句 pydantic 英文原文。
    """
    api_login(client, MEMBER_PHONE, MEMBER_PASSWORD)
    resp = client.get("/api/chat/abc/stream")
    assert resp.status_code == 422, resp.text
    assert resp.json() == {"error": "session_id 的格式不对"}

    # 同一条口径在别的路由上也成立（不是给 stream 单开的洞）
    detail = client.get("/api/chat/1.5")
    assert detail.status_code == 422, detail.text
    assert detail.json() == {"error": "session_id 的格式不对"}

    # 数字的路径照样是原来的语义：查无此会话仍是 404，不是 422
    missing = client.get("/api/chat/987654/stream")
    assert missing.status_code == 404, missing.text


# --- 门禁与闸门 ------------------------------------------------------------------


def test_anonymous_stream_is_401_json(client, fresh_db):
    """没登录不许尾读（错误信封与全站一致，不是半截流）。"""
    resp = client.get(f"/api/chat/{fresh_db['session_id']}/stream")
    assert resp.status_code == 401
    assert resp.json() == {"error": "请先登录"}
    assert resp.headers["content-type"].startswith("application/json")


def test_stream_for_an_unknown_session_is_404(client, fresh_db):
    """查无此会话是 404，不是挂着不断开的空流（口径同会话详情）。"""
    api_login(client, MEMBER_PHONE, MEMBER_PASSWORD)
    resp = client.get("/api/chat/987654/stream")
    assert resp.status_code == 404
    assert resp.json() == {"error": "没有这个会话"}


def test_posting_again_while_the_agent_is_writing_is_409(
    fresh_db, mock_openai, client, real_agent, monkeypatch
):
    """R-S2-1 的重入闸：一场会话同一时刻只许一轮在跑。

    工具卡在飞的时候发第二条 → 409 + 那句中文（逐字）；这一轮落地之后再发 → 200。
    闸在 append_human **之前**，所以 409 那一条一个字都没落库（话还留在人的输入框里，
    而不是库里多一条永远没人回的消息）。
    """
    sid = fresh_db["session_id"]
    api_login(client, MEMBER_PHONE, MEMBER_PASSWORD)
    mock_openai.reset(tool_name="search", tool_args=SEARCH_ARGS)
    entered, release = threading.Event(), threading.Event()
    tool = next(t for t in WIKI_TOOLS if t.name == "search")
    original = tool.on_invoke_tool

    async def blocking(ctx, input_str):
        entered.set()
        await asyncio.get_running_loop().run_in_executor(None, release.wait, 60)
        return await original(ctx, input_str)

    monkeypatch.setattr(tool, "on_invoke_tool", blocking)
    url = f"/api/chat/{sid}/messages"

    assert client.post(url, data={"content": HUMAN}).status_code == 200
    assert entered.wait(60), "这一轮没跑到工具上，闸没被撑开"
    assert sid in chat_router.RUNNING

    resp = client.post(url, data={"content": "再问一句"})
    assert resp.status_code == 409
    assert resp.json() == {"error": "智能体还在写，等它说完"}
    # fresh_db 那条 + 刚才 POST 的那条 = 2；409 这一条一个字都没落库
    assert _read(
        "SELECT COUNT(*) AS n FROM messages WHERE session_id = ? AND role = 'human'", (sid,)
    )[0]["n"] == 2, "409 那一条不该落库"

    release.set()
    _wait_for(lambda: sid not in chat_router.RUNNING, what="这一轮跑完该把闸摘掉")
    # 跑完就又能发了（release 已经 set，第二轮的工具不再卡）
    assert client.post(url, data={"content": "换个说法再问一次"}).status_code == 200
    _wait_for(lambda: sid not in chat_router.RUNNING, what="第二轮也该把闸摘掉")
    # 两次 POST = 两轮跑，两轮都正常封了口（409 那次没起第三轮）
    rows = _read(
        "SELECT truncated, usage_json FROM messages WHERE session_id = ? AND role = 'agent'"
        " ORDER BY seq", (sid,)
    )
    assert len(rows) == 2, rows
    assert all(row["truncated"] == 0 for row in rows), rows


def test_the_gate_is_freed_even_when_the_run_blows_up(
    fresh_db, client, real_agent, monkeypatch
):
    """run_turn 承诺不抛，真抛了也不能把 session_id 留在闸里（否则这场会话从此 409）。"""
    sid = fresh_db["session_id"]
    api_login(client, MEMBER_PHONE, MEMBER_PASSWORD)

    async def boom(conn, session_id, actor, **kwargs):
        raise RuntimeError("炸了")

    monkeypatch.setattr(runner, "run_turn", boom)
    url = f"/api/chat/{sid}/messages"
    assert client.post(url, data={"content": HUMAN}).status_code == 200
    _wait_for(lambda: sid not in chat_router.RUNNING, what="炸了也得把闸摘掉")
    assert client.post(url, data={"content": "再试一次"}).status_code == 200
    _wait_for(lambda: sid not in chat_router.RUNNING, what="第二次也该把闸摘掉")


def test_the_human_message_is_committed_before_the_run_reads_the_history(
    fresh_db, mock_openai, client, real_agent, monkeypatch
):
    """触发之前必须自己提交一次：后台任务在另一条连接上读上下文（runner._history）。

    get_conn 的收尾提交发生在路由返回之后，而 create_task 起的任务可能比那次收尾先跑
    起来——不提交的话，这一轮压根看不见刚发的这句人话，回的是上一句。
    """
    sid = fresh_db["session_id"]
    api_login(client, MEMBER_PHONE, MEMBER_PASSWORD)
    mock_openai.reset(mode="text_only")            # 一句纯文本就收尾，不要工具
    seen: list[list[dict]] = []
    real = runner.run_turn

    async def spy(conn, session_id, actor, **kwargs):
        # 就在这一轮开头把它此刻能看见的人话记下来（另一条连接，跟 runner 同一条）
        seen.append(_read(
            "SELECT content FROM messages WHERE session_id = ? AND role = 'human'"
            " ORDER BY seq", (session_id,)
        ))
        await real(conn, session_id, actor, **kwargs)

    monkeypatch.setattr(runner, "run_turn", spy)
    assert client.post(
        f"/api/chat/{sid}/messages", data={"content": "把这段整理进 wiki"}
    ).status_code == 200
    _wait_for(lambda: seen, what="这一轮该起来了")
    assert [r["content"] for r in seen[0]][-1] == "把这段整理进 wiki"
    _wait_for(lambda: sid not in chat_router.RUNNING, what="这一轮该跑完")
