"""runner 的线格式测试：一个本地 mock 的 OpenAI 兼容服务器 + 三种断流形状。

不花委托人的配额，但验的是我们真正依赖的东西：SDK 0.24.0 把 chat-completions 的 SSE
翻成什么事件、runner 把哪些事件落 messages.content、哪些落 agent_events、
429/断流/内部异常三条兜底各自的封口形状，以及"另一条连接在跑中途看得见增量"（N6）。

**两处与派遣单口径不同的实证结果**（探针记录见 task-4-report.md）：

1. `RawResponsesStreamEvent.data` **不是** openai 的 ChatCompletionChunk，而是 Responses
   形状的语义事件：正文是 `ResponseTextDeltaEvent`（`.delta` 是 str），工具参数是
   `ResponseFunctionCallArgumentsDeltaEvent`（`.delta` 也有，是参数 JSON 串）。所以
   _relay 认的是**类型**而不是 `.delta` 这个属性名——只认属性名的话，工具参数会被
   当成正文追加进 agent 消息（SSE 上就是一段 `{"query": …}` 混进回复里）。
2. `RunConfig` 的字段叫 `tool_execution`，不叫 `tool_execution_config`（后者直接 TypeError）。

断流不用 mock 服务器演：BaseHTTPRequestHandler 无论怎么"硬断"（SO_LINGER reset、
半截 chunked、写完不关），openai 的 SSE 迭代器都当"流干净地结束了"，一行不抛。
所以那两条用 httpx 的自定义 transport 演——字节交给 openai 的解析器之后在流中间抛
`httpx.ReadError`，SDK 那头就是真的 `openai.APIConnectionError`（上游断流）。

**这个文件只测"真装配 + 假上游"的线格式。** 单元形状归各自的单测文件，这里不重复：
`db.seal_orphan_agent_messages` 那套启动封口在 tests/test_seal.py，`llm.make_model`
与错误映射在 tests/test_llm.py。这里唯一碰它们的地方是最后那条"接缝"测试——runner
自己封的口不许被下次启动再封一遍，那是两个文件之间的约定，单测任何一边都测不出来。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import threading
import time

import httpx
import pytest
from agents.exceptions import ModelBehaviorError
from agents.tool_context import ToolContext
from openai import AsyncOpenAI

from app.agent import runner
from app.agent.context import AgentContext
from app.agent.prompt import BASE_STYLE
from app.agent.tools import WIKI_TOOLS
from app.config import settings
from app.db import SEAL_MARK, connect, seal_orphan_agent_messages
from app.services import llm
from app.wiki import store
from tests.conftest import make_human_message, make_session

HUMAN = "漏单降到 1 单，客服投诉也少了"
TURN1_TEXT = ["先查一下。", "这就去。"]
TURN2_TEXT = ["查过了，", "结论在这。"]
WRAP_UP_REPLY = ["就到这儿，", "还差最后一步。"]   # 超轮之后收尾那一次拿到的人话
QUOTA_TEXT = runner.QUOTA_TEXT

# 额度类响应体（429 速率/日额度、402 余额不足）。三处要用（第一轮就撞额度、
# 收尾那一次才撞额度、单独验 402），别抄三遍。
QUOTA_BODY = {"error": {
    "message": "Insufficient balance or rate limit reached.",
    "type": "insufficient_quota", "code": "insufficient_quota"}}


# --- mock 服务器（chat-completions 的 SSE 线格式） ------------------------------


def _sse_chunk(delta: dict, finish: str | None = None) -> dict:
    return {
        "id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "mock",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


class Handler:
    """mock 服务器的状态与剧本。每个测试自己 reset()，别互相串。"""

    mode = "ok"
    tool_name = "list_wiki"
    tool_args: dict = {}
    # 不是 None 时**原样**当 arguments 串发出去，不走 json.dumps。演"模型给无参工具
    # 交了个空串"那种形状用的：json.dumps({}) 会得到 "{}"（非空），演不出 hooks 那边
    # "没有 args 这个键"的分支（裁定 R-S2-12 / T4a §1.3）。
    tool_args_raw: str | None = None
    parallel_calls = 0          # >0 时第一轮一次要这么多个同名工具（试并发闸）
    wrap_up = "tool"            # always_tool 模式下，收尾那一次（请求里没 tools）返什么：
                                # "tool" 再要一次工具（= 收尾失败）、"text" 返人话、"quota" 返 429
    quota_status = 429          # "quota" 模式回哪个状态码：429（速率/日额度）或 402（余额不足）
    requests: list[dict] = []

    @classmethod
    def reset(cls, mode: str = "ok", tool_name: str = "list_wiki", tool_args: dict | None = None,
              tool_args_raw: str | None = None, parallel_calls: int = 0, wrap_up: str = "tool",
              quota_status: int = 429):
        cls.mode = mode
        cls.tool_name = tool_name
        cls.tool_args = tool_args or {}
        cls.tool_args_raw = tool_args_raw
        cls.parallel_calls = parallel_calls
        cls.wrap_up = wrap_up
        cls.quota_status = quota_status
        cls.requests = []

    @classmethod
    def arguments(cls) -> str:
        """这一次工具调用在线上的 arguments 串。"""
        if cls.tool_args_raw is not None:
            return cls.tool_args_raw
        return json.dumps(cls.tool_args, ensure_ascii=False)


def _make_handler(state: type[Handler]):
    from http.server import BaseHTTPRequestHandler

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _json(self, code: int, payload: dict):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _sse(self, chunks: list[dict]):
            body = b"".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n".encode() for c in chunks)
            body += b"data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _tool_call_chunks(self, call_id: str):
            call = {
                "index": 0, "id": call_id, "type": "function",
                "function": {"name": state.tool_name, "arguments": state.arguments()},
            }
            return [
                _sse_chunk({"role": "assistant"}),
                _sse_chunk({"tool_calls": [call]}),
                _sse_chunk({}, finish="tool_calls"),
            ]

        def _text_chunks(self, pieces: list[str]):
            out = [_sse_chunk({"role": "assistant", "content": p}) for p in pieces]
            out.append(_sse_chunk({}, finish="stop"))
            return out

        def do_POST(self):
            length = int(self.headers.get("content-length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            state.requests.append(body)
            turn = len(state.requests)
            if state.mode == "quota":
                return self._json(state.quota_status, QUOTA_BODY)
            if state.mode == "boom":
                return self._json(500, {"error": {"message": "kaput", "type": "server_error"}})
            if state.mode == "context400":
                # 上下文超限的 400：报文里带 context/length 字样（裁定 R-S2-31 的判据）
                return self._json(400, {"error": {
                    "message": "This model's maximum context length is 131072 tokens. "
                               "However, your messages resulted in 200000 tokens.",
                    "type": "invalid_request_error", "code": "context_length_exceeded"}})
            if state.mode == "bad400":
                # 同样是 400，但报文与上下文无关：不许被误认成 context length
                return self._json(400, {"error": {
                    "message": "Invalid value for 'temperature'.",
                    "type": "invalid_request_error"}})
            if state.mode == "always_tool":
                if not body.get("tools") and state.wrap_up != "tool":
                    # 收尾那一次：runner 超轮之后不带工具再要一句人话（计划 §19）。
                    # 请求里没有 tools 就是它——按剧本回一句纯文本（收尾成功），
                    # 或者回 429（收尾也撞上额度，验 runner 别把它报成"超轮"）。
                    if state.wrap_up == "quota":
                        return self._json(429, QUOTA_BODY)
                    return self._sse(self._text_chunks(WRAP_UP_REPLY))
                # 每轮都换一个 call_id：SDK 会跳过重复的 (call_id, 参数) 调用，
                # 不换的话五次里只有第一次真跑，max_turns 那条测不出东西。
                return self._sse(self._tool_call_chunks(f"call_{turn}"))
            if state.mode == "text_only":
                return self._sse(self._text_chunks(TURN2_TEXT))
            if state.parallel_calls and turn == 1:
                # 一个 chunk 里塞两个工具调用：SDK 默认会把它们并发丢进 to_thread
                calls = [
                    {"index": i, "id": f"call_p{i}", "type": "function",
                     "function": {"name": state.tool_name, "arguments": state.arguments()}}
                    for i in range(state.parallel_calls)
                ]
                return self._sse([
                    _sse_chunk({"role": "assistant"}),
                    _sse_chunk({"tool_calls": calls}),
                    _sse_chunk({}, finish="tool_calls"),
                ])
            if turn == 1:
                # 真模型常见的形状：先说一句"我去查"，再要工具，这一轮不给 finish=stop
                return self._sse(
                    [_sse_chunk({"role": "assistant", "content": p}) for p in TURN1_TEXT]
                    + self._tool_call_chunks("call_1")
                )
            return self._sse(self._text_chunks(TURN2_TEXT))

    return _Handler


@pytest.fixture
def mock_openai():
    """起一个本地 mock OpenAI 服务器，把 settings 指过去（用完由 autouse fixture 还原）。

    指 settings 而不是打桩 runner.make_model：这样 llm.make_model 也在链路里，
    测的是"真装配 + 假上游"，不是"假装配 + 假上游"。
    """
    from http.server import ThreadingHTTPServer

    Handler.reset()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(Handler))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/v1"
    _set_settings(llm_base_url=base, text_model="mock-model")
    try:
        yield Handler
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def fresh_db(client, conn):
    """清干净的库 + 一场会话 + 一条已进检索的人话（人类消息永远先落库再调模型）。

    索引用 search.index_message 补一下：真链路里那一下是 chat.append_human 顺手做的，
    而 conftest 的 make_human_message 是裸 SQL（它只管插行）。不补的话 search 工具
    在这一轮里搜不到任何东西，"工具真跑了"那条断言就成了空的。
    """
    from app.services import auth
    from app.services import search as search_service

    uid = auth.create_member_direct(conn, "张三", "13800000001", "member-local-1")
    conn.commit()
    sid = make_session(conn, uid, "张三的对话")
    mid = make_human_message(conn, sid, uid, HUMAN)
    assert search_service.index_message(conn, mid) is True
    conn.commit()
    return {"user_id": uid, "session_id": sid, "human_id": mid}


# --- httpx transport：按脚本演上游（断流 / 在两段之间停住） ---------------------


def _chunk_line(delta: dict, finish: str | None = None) -> bytes:
    return f"data: {json.dumps(_sse_chunk(delta, finish), ensure_ascii=False)}\n\n".encode()


def _tool_call_line(name: str, args: dict, call_id: str = "call_1") -> bytes:
    call = {"index": 0, "id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
    return _chunk_line({"tool_calls": [call]})


class _ScriptedStream(httpx.AsyncByteStream):
    """按脚本走一条 SSE 流：bytes 就交出去，可调用对象就 await（用来卡在流中间）。"""

    def __init__(self, steps: list):
        self._steps = steps

    async def __aiter__(self):
        for step in self._steps:
            if isinstance(step, bytes):
                yield step
            else:
                await step()


class _ScriptedTransport(httpx.AsyncBaseTransport):
    """一次性的脚本上游：第一个请求按脚本走，后面的请求一律返一段纯文本收尾。

    后面那些请求是工具跑完之后的第二轮（SDK 自己发的）。若把同一个脚本再演一遍，
    同样的 delta 会被 _relay 再追加一次，正文就成了两遍——那不是 runner 的错，
    是脚本上游不像真上游。
    """

    def __init__(self, steps: list):
        self._steps = steps
        self._served = False

    async def handle_async_request(self, request):
        if not self._served:
            self._served = True
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=_ScriptedStream(self._steps))
        tail = b"".join([
            _chunk_line({"role": "assistant", "content": TURN2_TEXT[0]}),
            _chunk_line({"content": TURN2_TEXT[1]}),
            _chunk_line({}, finish="stop"),
            b"data: [DONE]\n\n",
        ])
        return httpx.Response(200, headers={"content-type": "text/event-stream",
                                           "content-length": str(len(tail))},
                              content=tail)


async def _raise_read_error():
    raise httpx.ReadError("Connection closed while receiving data")


def _point_llm_at(monkeypatch, steps: list, transport: httpx.AsyncBaseTransport | None = None):
    """把 llm 里那个 AsyncOpenAI 换成走自定义 transport 的（make_model 其余不动）。

    transport 不给就是 _ScriptedTransport(steps)；要"第一个请求按脚本走、后面的请求先等
    一道闸再回收尾文本"的用例（tests/test_stream_api.py 的帧顺序）自己造一个交进来。
    """
    real = AsyncOpenAI

    def fake(*args, **kwargs):
        kwargs.pop("base_url", None)
        kwargs.pop("api_key", None)
        return real(
            api_key="mock", base_url="http://mock.invalid/v1", max_retries=0,
            http_client=httpx.AsyncClient(transport=transport or _ScriptedTransport(steps)),
        )

    monkeypatch.setattr(llm, "AsyncOpenAI", fake)


# --- 读库的小工具 -------------------------------------------------------------


def _read(sql: str, params: tuple = ()) -> list[dict]:
    """另开一条连接读一次（SSE 尾读的形状），读完就关。

    runner 写、这里读，两条连接：这正是 N6 要盯住的那道缝——写侧不提交，读侧什么都看不见。
    """
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _agent_row(session_id: int) -> dict:
    rows = _read(
        "SELECT * FROM messages WHERE session_id = ? AND role = 'agent' ORDER BY seq",
        (session_id,),
    )
    assert len(rows) == 1, rows
    return rows[0]


def _agent_content(session_id: int) -> str | None:
    rows = _read(
        "SELECT content FROM messages WHERE session_id = ? AND role = 'agent'", (session_id,)
    )
    return rows[0]["content"] if rows else None


def _events(session_id: int) -> list[dict]:
    rows = _read(
        "SELECT seq, type, payload_json FROM agent_events WHERE session_id = ? ORDER BY seq",
        (session_id,),
    )
    return [{"seq": r["seq"], "type": r["type"], "payload": json.loads(r["payload_json"])}
            for r in rows]


def _run(coro):
    return asyncio.run(coro)


def _set_settings(**fields):
    """改 settings 的字段，并登记还原。

    settings 是 frozen dataclass 的模块级单例（全项目 `from ..config import settings`
    拿的是同一个对象），monkeypatch.setattr 走 setattr 会 FrozenInstanceError，
    只能 object.__setattr__（tests/test_startup.py 的 fresh_data_dir 同一个手法）。
    还原交给下面那条 autouse fixture。
    """
    for name, value in fields.items():
        _RESTORE.append((name, getattr(settings, name)))
        object.__setattr__(settings, name, value)


_RESTORE: list[tuple[str, object]] = []


@pytest.fixture(autouse=True)
def _restore_settings():
    yield
    while _RESTORE:
        name, original = _RESTORE.pop()
        object.__setattr__(settings, name, original)


# --- 一轮完整的跑：两轮 SSE、一次工具调用 --------------------------------------


def test_a_full_turn_streams_text_into_the_agent_message(fresh_db, mock_openai):
    sid = fresh_db["session_id"]

    # 先把 last_message_at 摁回一个不可能自然出现的旧值：utcnow 只有秒级分辨率，
    # 而 make_session 落行时就把它设成了现在——原来那句 `>= started_at` 就算把
    # runner 的 chat.touch 调用整个删掉也恒真，是条假断言。摁旧之后"封口推了
    # last_message_at"才测得出来。
    stale = "2000-01-01 00:00:00"
    writer = connect()
    try:
        writer.execute("UPDATE chat_sessions SET last_message_at = ? WHERE id = ?", (stale, sid))
        writer.commit()
        _run(runner.run_turn(writer, sid, {"id": fresh_db["user_id"], "name": "张三"}))
    finally:
        writer.close()

    row = _agent_row(sid)
    # 正文 = 两轮 SSE 的文本拼接（SDK 的 final_output 只有**最后一轮**那句，
    # 拿它覆盖会丢掉工具调用之前流出去的那一段）
    assert row["content"] == "".join(TURN1_TEXT) + "".join(TURN2_TEXT)
    # 封口：跑完之后绝不留 usage_json NULL + truncated=0（尾读会永远等一个不来的结尾）
    assert row["truncated"] == 0
    assert json.loads(row["usage_json"]) == {}
    assert row["user_id"] is None and row["seq"] == 2
    assert row["created_at"]

    # 线上真发过去的形状：system 提示词 + 那条人话（带 [消息id] 前缀），stream=True
    assert len(mock_openai.requests) == 2
    first = mock_openai.requests[0]
    assert first["stream"] is True and first["model"] == "mock-model"
    assert first["messages"][0]["role"] == "system"
    # 系统提示词 = 技能正文 + 每轮现算的库现状（本次修复）；正文本身必须逐字一致。
    assert first["messages"][0]["content"].startswith(runner._base_instructions())
    assert "【库现状】" in first["messages"][0]["content"]
    assert first["messages"][1] == {"role": "user",
                                    "content": f"[消息{fresh_db['human_id']}] {HUMAN}"}
    assert {t["function"]["name"] for t in first["tools"]} == {t.name for t in WIKI_TOOLS}
    # 会话的"最近动静"跟着这条回复推到现在（chat.touch 就是为这里留的）：
    # 必须严格大于开局摁下去的旧值，删掉 _seal 里的 touch 这条就红
    session = _read("SELECT last_message_at FROM chat_sessions WHERE id = ?", (sid,))
    assert session[0]["last_message_at"] > stale, "封口没把 last_message_at 推到现在"


def test_a_full_turn_records_exactly_one_tool_call_and_one_tool_result(fresh_db, mock_openai):
    """工具事件由 hooks 落库（裁定 R-S2-9）：_relay 再写一遍就每条都翻倍。"""
    sid = fresh_db["session_id"]
    mock_openai.reset(tool_name="search", tool_args={"query": "客服投诉", "scope": "all"})

    writer = connect()
    try:
        _run(runner.run_turn(writer, sid, {"id": fresh_db["user_id"], "name": "张三"}))
    finally:
        writer.close()

    events = _events(sid)
    assert [e["type"] for e in events] == ["tool-call", "tool-result"]
    assert [e["seq"] for e in events] == [1, 2]
    # N7/R-S2-12：tool-call 带 args（截 200 字），S3 的工具卡片要显示参数
    assert events[0]["payload"]["name"] == "search"
    assert json.loads(events[0]["payload"]["args"]) == {"query": "客服投诉", "scope": "all"}
    assert events[1]["payload"]["name"] == "search"
    assert events[1]["payload"]["ok"] is True
    assert events[1]["payload"]["summary"].startswith("搜到")
    # R-S2-21：call 与 result 带同一个 tool_call_id（S3 按 id 配对）
    assert events[0]["payload"]["id"] == events[1]["payload"]["id"] == "call_1"
    # R-S2-20：事件行归到这一轮那条 agent 消息（hooks 从 AgentContext 拿 message_id）
    attributed = _read(
        "SELECT message_id FROM agent_events WHERE session_id = ? ORDER BY seq", (sid,)
    )
    assert [r["message_id"] for r in attributed] == [_agent_row(sid)["id"]] * 2
    # 工具真跑了：它读的是 runner 那条连接（AgentContext.conn）
    assert "【消息" in events[1]["payload"]["summary"]


def test_the_wire_carries_the_instructions_and_the_prefixed_history(fresh_db, mock_openai):
    """N10：喂给模型的是最近 24 条人话+智能体回复，人话带 [消息{id}] 前缀。"""
    sid = fresh_db["session_id"]
    conn = connect()
    try:
        for i in range(30):
            make_human_message(conn, sid, fresh_db["user_id"], f"第 {i} 句")
        conn.commit()
    finally:
        conn.close()

    writer = connect()
    try:
        _run(runner.run_turn(writer, sid, {"id": fresh_db["user_id"], "name": "张三"}))
    finally:
        writer.close()

    first = mock_openai.requests[0]
    assert first["model"] == "mock-model" and first["stream"] is True
    messages = first["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"].startswith(runner._base_instructions())
    assert "【库现状】" in messages[0]["content"]
    # 31 条人话只带最近 24 条：最老的 6 条（第 0 到第 5 句）不进上下文，
    # 带上的第一条是第 6 句、最后一条是最新那句；刚发的那句太老，已经掉出窗口了
    assert len(messages) - 1 == runner.HISTORY_MAX == 24
    assert messages[1]["role"] == "user"
    assert messages[1]["content"] == f"[消息{fresh_db['human_id'] + 7}] 第 6 句"
    assert messages[-1]["content"] == f"[消息{fresh_db['human_id'] + 30}] 第 29 句"
    assert all(m["role"] in ("system", "user", "assistant") for m in messages)
    # 前缀里的编号就是引注要填的 source_id：掉出窗口的那条不该再被引
    assert f"[消息{fresh_db['human_id']}]" not in json.dumps(messages, ensure_ascii=False)


def test_history_stops_at_the_character_budget_newest_first(fresh_db, mock_openai):
    """I3 / R-S2-31：历史按新→旧累加字符，到 HISTORY_CHAR_BUDGET 就停（24 条上限仍在）。

    条数上限管不住字数：4 条每条 20009 字（BIGBLOCK + 20000 个"话"）就是 8 万字，
    任何窗口都装不下。累加到第三条正好越过 60000，第四条（最老那条 BIGBLOCK0）被截掉。
    截的是**最老**的——模型要用旧材料还有 search/read 两个工具（"只检索不带头脑"那条路）。
    """
    sid = fresh_db["session_id"]
    conn = connect()
    try:
        for i in range(4):
            make_human_message(conn, sid, fresh_db["user_id"], f"BIGBLOCK{i}" + "话" * 20000)
        conn.commit()
    finally:
        conn.close()

    writer = connect()
    try:
        _run(runner.run_turn(writer, sid, {"id": fresh_db["user_id"], "name": "张三"}))
    finally:
        writer.close()

    sent = mock_openai.requests[0]["messages"]
    blob = json.dumps(sent, ensure_ascii=False)
    # 最新的 3 条进来了，最老那条（BIGBLOCK0）被字符预算截掉
    assert "BIGBLOCK3" in blob and "BIGBLOCK2" in blob and "BIGBLOCK1" in blob
    assert "BIGBLOCK0" not in blob, "最老那条应被字符预算截掉"
    # 交上去的是旧→新（时间序）：BIGBLOCK1 在 2 之前、2 在 3 之前
    assert blob.index("BIGBLOCK1") < blob.index("BIGBLOCK2") < blob.index("BIGBLOCK3")
    # 字数落在"3 条进、4 条不进"的区间里（每条含 [消息id] 前缀的少量开销）
    total = sum(len(m["content"]) for m in sent if m["role"] in ("user", "assistant"))
    assert 3 * 20000 <= total < 4 * 20000 + 200, total
    assert runner.HISTORY_CHAR_BUDGET == 60000



def test_agent_replies_come_back_as_assistant_lines_without_a_prefix(fresh_db, mock_openai):
    sid = fresh_db["session_id"]
    writer = connect()
    try:
        _run(runner.run_turn(writer, sid, {"id": fresh_db["user_id"], "name": "张三"}))
        # 第二轮：上一条智能体回复原样进上下文（没有 [消息id] 前缀，那不是人话）
        _run(runner.run_turn(writer, sid, {"id": fresh_db["user_id"], "name": "张三"}))
    finally:
        writer.close()

    messages = mock_openai.requests[-1]["messages"]
    assert {"role": "assistant",
            "content": "".join(TURN1_TEXT) + "".join(TURN2_TEXT)} in messages


def test_the_agent_reply_is_not_indexed_for_search(fresh_db, mock_openai):
    """fts_shared 只索引 human 消息（search.index_message 的口径）：智能体的回复不进检索。"""
    sid = fresh_db["session_id"]
    writer = connect()
    try:
        _run(runner.run_turn(writer, sid, {"id": fresh_db["user_id"], "name": "张三"}))
    finally:
        writer.close()

    kinds = _read("SELECT kind, ref_id FROM fts_shared")
    assert [k["kind"] for k in kinds] == ["message"]
    assert kinds[0]["ref_id"] == str(fresh_db["human_id"])


# --- N6：另一条连接在跑中途看得见增量 ------------------------------------------


def test_a_second_connection_sees_the_content_grow_mid_run(fresh_db, monkeypatch):
    """runner 那条连接**不是** autocommit（S1/T3 裁定），所以看得见增量就等于提交了。

    上游按脚本走：先停一下（这时另一条连接已经看得见那条空正文的 agent 行 =
    _open_agent_message 提交了），再一段段吐 delta，中间卡在工具调用上。
    卡住时读到的正文只有前半截，而这一轮跑还远没结束——增量是逐段可见的。
    """
    sid = fresh_db["session_id"]
    entered = threading.Event()
    release = threading.Event()
    tool = next(t for t in WIKI_TOOLS if t.name == "list_wiki")
    original = tool.on_invoke_tool

    async def blocking(ctx, input_str):
        entered.set()
        await asyncio.get_running_loop().run_in_executor(None, release.wait, 20)
        return await original(ctx, input_str)

    monkeypatch.setattr(tool, "on_invoke_tool", blocking)

    async def pause():
        """卡在流中间：等工具体进去，再等到测试那头读完为止。"""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, entered.wait, 20)
        await loop.run_in_executor(None, release.wait, 20)

    async def before_anything():
        await asyncio.sleep(0.15)      # 让测试那头有机会读到"行在、正文还空着"

    # 第二轮（工具跑完之后的那次请求）不用写在这里：_ScriptedTransport 对后续请求
    # 一律返 TURN2_TEXT 那段纯文本收尾。
    _point_llm_at(monkeypatch, [
        before_anything,
        _chunk_line({"role": "assistant", "content": TURN1_TEXT[0]}),
        _chunk_line({"content": TURN1_TEXT[1]}),
        _tool_call_line("list_wiki", {}),
        _chunk_line({}, finish="tool_calls"),
        pause,
    ])

    async def scenario():
        writer = connect()
        assert writer.isolation_level is not None, "runner 的连接不该是 autocommit"
        seen = []
        try:
            task = asyncio.create_task(
                runner.run_turn(writer, sid, {"id": fresh_db["user_id"], "name": "张三"})
            )

            async def wait_for(wanted, tries=500):
                for _ in range(tries):
                    got = _agent_content(sid)
                    seen.append(got)
                    if got == wanted:
                        return True
                    await asyncio.sleep(0.01)
                return False

            # 一段正文都还没流出来时，另一条连接就该看得见这条 agent 行
            assert await wait_for("")
            # 前半截流出来就看得见——这时上游正卡在工具调用上，这一轮跑远没结束
            assert await wait_for("".join(TURN1_TEXT))
            assert not task.done(), "读到半截时这一轮必须还在跑，不然这条断言什么也没测到"
            release.set()
            await asyncio.wait_for(task, 30)
        finally:
            release.set()
            writer.close()
        return seen

    seen = _run(scenario())
    assert "" in seen
    row = _agent_row(sid)
    assert row["content"] == "".join(TURN1_TEXT) + "".join(TURN2_TEXT)
    assert row["truncated"] == 0 and json.loads(row["usage_json"]) == {}
    assert [e["type"] for e in _events(sid)] == ["tool-call", "tool-result"]


def test_a_second_connection_sees_the_content_grow_while_a_tool_runs(fresh_db, mock_openai,
                                                                    monkeypatch):
    """真上游（mock 服务器）+ 真工具：工具在跑时，已流出的那半截照样看得见。

    工具体与 hooks 用的是**同一条**连接（AgentContext.conn），而 ToolExecutionConfig
    把并发压到 1（裁定 R-S2-13），所以这里同时也是"串行执行下写不互踩"的一次实跑。
    """
    sid = fresh_db["session_id"]
    tool = next(t for t in WIKI_TOOLS if t.name == "list_wiki")
    original = tool.on_invoke_tool
    release = threading.Event()
    entered = threading.Event()

    async def blocking(ctx, input_str):
        entered.set()
        await asyncio.get_running_loop().run_in_executor(None, release.wait, 20)
        return await original(ctx, input_str)

    monkeypatch.setattr(tool, "on_invoke_tool", blocking)

    async def scenario():
        writer = connect()
        try:
            task = asyncio.create_task(
                runner.run_turn(writer, sid, {"id": fresh_db["user_id"], "name": "张三"})
            )
            await asyncio.get_running_loop().run_in_executor(None, entered.wait, 20)
            mid = _agent_content(sid)
            mid_events = [e["type"] for e in _events(sid)]
            release.set()
            await asyncio.wait_for(task, 30)
            return mid, mid_events
        finally:
            release.set()
            writer.close()

    mid, mid_events = _run(scenario())
    assert mid == "".join(TURN1_TEXT)
    assert mid_events == ["tool-call"], "hooks 在工具跑之前就记了 tool-call"
    assert _agent_row(sid)["content"] == "".join(TURN1_TEXT) + "".join(TURN2_TEXT)
    assert [e["type"] for e in _events(sid)] == ["tool-call", "tool-result"]


def test_tools_run_one_at_a_time_so_the_shared_connection_is_not_written_from_two_threads(
        fresh_db, mock_openai, monkeypatch):
    """裁定 R-S2-13：一轮里模型要了两个工具，也得一个一个跑。

    工具体（写 citation_checks、commit）与 hooks（写 agent_events、commit）用的是
    **同一条** sqlite 连接；SDK 默认把一轮里的工具全部并发丢进 to_thread，两个线程
    同时在这条连接上开事务就会互踩（"cannot start a transaction within a transaction"
    一类），而且踩出来的现象随线程调度漂。max_function_tool_concurrency=1 就是消这个。
    """
    sid = fresh_db["session_id"]
    tool = next(t for t in WIKI_TOOLS if t.name == "list_wiki")
    original = tool.on_invoke_tool
    state = {"live": 0, "max_live": 0, "calls": 0}
    lock = threading.Lock()

    async def watched(ctx, input_str):
        with lock:
            state["live"] += 1
            state["calls"] += 1
            state["max_live"] = max(state["max_live"], state["live"])
        try:
            await asyncio.sleep(0.05)          # 拉开窗口：并发跑的话两个必然重叠
            return await original(ctx, input_str)
        finally:
            with lock:
                state["live"] -= 1

    monkeypatch.setattr(tool, "on_invoke_tool", watched)
    mock_openai.reset(parallel_calls=2)

    writer = connect()
    try:
        _run(runner.run_turn(writer, sid, {"id": fresh_db["user_id"], "name": "张三"}))
    finally:
        writer.close()

    assert state["calls"] == 2, state
    assert state["max_live"] == 1, f"两个工具重叠着跑了：{state}"
    # 两个工具各自的事件都齐：hooks 贴着每次调用的前后各记一条，所以是
    # call→result→call→result（串行执行的形状）；seq 连号，不撞 UNIQUE(session_id, seq)
    events = _events(sid)
    assert [e["type"] for e in events] == ["tool-call", "tool-result",
                                           "tool-call", "tool-result"]
    assert [e["seq"] for e in events] == [1, 2, 3, 4]
    assert all(e["payload"]["name"] == "list_wiki" for e in events)
    assert _agent_row(sid)["truncated"] == 0


def test_relay_and_a_slow_tool_never_collide_on_the_shared_connection(fresh_db, tmp_wiki,
                                                                       monkeypatch):
    """R-S2-17 压力测：大量小正文增量 + 一个真写库的慢工具，两个线程砸同一条连接。

    裁定点名的缝：relay 的 _append_text 在事件循环线程，工具体在 asyncio.to_thread 的
    worker 线程，共用**同一条** sqlite 连接；max_function_tool_concurrency=1 只把工具
    与工具串起来，管不了这条缝。没有 db_lock 时，两个线程同时开隐式事务就是
    "cannot start a transaction within a transaction"，或者一边的 commit 把另一边
    没写完的事务一起带出去。

    不靠真跑一轮去等那个微秒级的碰撞窗口：mock 上游下 relay 把事件队列消化得
    "一片清"（asyncio.Queue 非空时 get 不挂起，探针实测 relay 总在工具开跑前收尾），
    真装配里两端根本碰不上头，"跑一遍没炸"什么也证明不了。所以把缝的两端直接拿
    过来对着砸——全是真函数、真线程形状：

    - 线程 A：800 次 runner._append_text（relay 写正文增量走的就是它，每段一次
      UPDATE+commit = "大量小 text delta"）；
    - asyncio 那头：真的 write_page 工具（tool.on_invoke_tool，SDK 把它丢进
      asyncio.to_thread 的 worker 线程），store.write_page_sync 被打桩成写完真页后
      再按 2ms 一拍补 40 次小写+提交（= 慢工具，模拟长事务，整个窗口 ~180ms）。

    连接换成 _OverlapSpyConn，**直接记录**"两个不同线程同时在这条连接里"的次数：
    db_lock 在位时两端全在锁内，次数恒为 0、写全部落库；锁拿掉，这条断言必红。
    """
    sid = fresh_db["session_id"]
    args = {"title": "压力测试页", "page_type": "entity",
            "body_md": "一句正文，没有引注。", "citations": []}

    # 慢工具：真写页的前后各按 2ms 一拍补 20 次小写+提交，把与 relay 的重叠窗口
    # 铺满工具体的整个生命周期（git 子进程那一段不碰连接，光靠它两头没有缝）
    real_write = store.write_page_sync

    def paced_writes(conn, n=20):
        for _ in range(n):
            time.sleep(0.002)
            conn.execute("UPDATE messages SET content = content WHERE id = -1")
            conn.commit()

    def slow_write(conn, *a, **k):
        paced_writes(conn)
        res = real_write(conn, *a, **k)
        paced_writes(conn)
        return res

    monkeypatch.setattr(store, "write_page_sync", slow_write)

    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)   # 让两个线程真的交错起来，别各跑一大段
    writer = _spy_connect()
    try:
        agent_ctx = AgentContext(conn=writer, session_id=sid, actor_name="张三")
        turn = runner._Turn(
            conn=writer, session_id=sid, db_lock=agent_ctx.db_lock,
            message_id=runner._open_agent_message(writer, sid, agent_ctx.db_lock),
            history=[],
        )

        # 线程 A 一直写到"够数"为止：db_lock 在位时它会被工具整段压在锁外（这正是
        # 串行该有的样子），工具返回后主线程等它补足 MIN_PIECES 段再收——写多少段
        # 不靠运气。没有锁时它在工具跑的整段里都在并发写，碰撞窗口照样铺满。
        MIN_PIECES = 300
        stop = threading.Event()
        relay_errors: list[BaseException] = []
        progress = [0]          # relay 已写了几段（主线程轮询；join 之后读终值）
        written: list[int] = []

        def relay_side():
            try:
                i = 0
                while not stop.is_set():
                    runner._append_text(turn, f"第{i}段。")
                    i += 1
                    progress[0] = i
                written.append(i)
            except BaseException as exc:   # noqa: BLE001 —— 收下来给断言，别死在别的线程里
                relay_errors.append(exc)

        tool = next(t for t in WIKI_TOOLS if t.name == "write_page")
        raw = json.dumps(args, ensure_ascii=False)
        tool_ctx = ToolContext(context=agent_ctx, tool_name=tool.name,
                               tool_call_id="t1", tool_arguments=raw)

        worker = threading.Thread(target=relay_side, daemon=True)
        worker.start()
        try:
            out = _run(tool.on_invoke_tool(tool_ctx, raw))   # 工具体走 to_thread，与线程 A 并发
            deadline = time.monotonic() + 10
            while (progress[0] < MIN_PIECES and not relay_errors
                   and time.monotonic() < deadline):
                time.sleep(0.005)
        finally:
            stop.set()
        worker.join(30)

        assert not relay_errors, f"relay 侧写崩了：{relay_errors!r}"
        assert not worker.is_alive(), "relay 线程没在限时内跑完"
        assert writer.overlaps == 0, (
            f"两个不同线程同时用了这条连接 {writer.overlaps} 次：db_lock 没兜住"
            f"（R-S2-17），撞出 OperationalError 只是时间问题"
        )
        count = written[0] if written else 0
        assert count >= MIN_PIECES, f"relay 只写了 {count} 段，压力不够"
        row = dict(writer.execute(
            "SELECT content, truncated, usage_json FROM messages WHERE id = ?",
            (turn.message_id,),
        ).fetchone())
        assert row["content"] == "".join(f"第{i}段。" for i in range(count)), "正文丢段或串写"
        assert out.startswith("已生效："), out
    finally:
        sys.setswitchinterval(old_interval)
        writer.close()


class _OverlapSpyConn(sqlite3.Connection):
    """记录"两个不同线程同时在这条连接里"的连接（R-S2-17 压力测的眼睛）。

    execute/commit 进门时登记本线程、出门时注销；登记时发现**别的**线程还在里面，
    overlaps 就 +1。db_lock 兜住了所有写路径的话，这个数字恒为 0——比等一个微秒级
    的碰撞窗口炸给你看要可靠得多。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._guard = threading.Lock()
        self._inside: set[int] = set()
        self.overlaps = 0

    def _enter(self):
        tid = threading.get_ident()
        with self._guard:
            if self._inside and tid not in self._inside:
                self.overlaps += 1
            self._inside.add(tid)

    def _leave(self):
        with self._guard:
            self._inside.discard(threading.get_ident())

    def execute(self, *args, **kwargs):
        self._enter()
        try:
            return super().execute(*args, **kwargs)
        finally:
            self._leave()

    def commit(self):
        self._enter()
        try:
            return super().commit()
        finally:
            self._leave()


def _spy_connect() -> _OverlapSpyConn:
    """与 app.db.connect 同参同 pragma，只是换成 _OverlapSpyConn 这个 factory。"""
    conn = sqlite3.connect(settings.db_path, timeout=30, check_same_thread=False,
                           factory=_OverlapSpyConn)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


# --- 三条兜底：429 / 断流 / 内部异常 --------------------------------------------


def test_429_seals_the_message_and_leaves_a_system_event(fresh_db, mock_openai):
    """spec §8：额度用完，人话已落库，流里出 system 事件，这一行标 truncated。"""
    sid = fresh_db["session_id"]
    mock_openai.reset(mode="quota")

    writer = connect()
    try:
        _run(runner.run_turn(writer, sid, {"id": fresh_db["user_id"], "name": "张三"}))
    finally:
        writer.close()

    row = _agent_row(sid)
    assert row["truncated"] == 1 and row["content"] == ""
    assert row["usage_json"] is None, "额度用尽时没有什么用量可记，NULL 才是真话"
    events = _events(sid)
    assert [e["type"] for e in events] == ["system"]
    assert events[0]["payload"] == {"text": QUOTA_TEXT} and "额度" in QUOTA_TEXT
    # llm 的 429 映射（N8）在 translate_openai_error 那条单测里钉；这里钉的是
    # make_model 里那个 client max_retries=0：429 不该被 client 自己重试两轮。
    assert len(mock_openai.requests) == 1


def test_402_insufficient_balance_also_leaves_a_system_event(fresh_db, mock_openai):
    """DeepSeek 的 402（余额不足）与 429 同口径：等额度回来重发，不是上游故障。

    402 要是落进 upstream 分支，人会看到"稍后重发一次"——方向没错但文案不对，
    前端也不会把会话标"待重试"。所以 runner 的 quota 分支认 (429, 402)。
    """
    sid = fresh_db["session_id"]
    mock_openai.reset(mode="quota", quota_status=402)

    writer = connect()
    try:
        _run(runner.run_turn(writer, sid, {"id": fresh_db["user_id"], "name": "张三"}))
    finally:
        writer.close()

    row = _agent_row(sid)
    assert row["truncated"] == 1 and row["content"] == ""
    events = _events(sid)
    assert [e["type"] for e in events] == ["system"]
    assert events[0]["payload"] == {"text": QUOTA_TEXT}
    assert len(mock_openai.requests) == 1      # max_retries=0：402 也不许 client 自己重试


def test_every_turn_request_disables_thinking(fresh_db, mock_openai):
    """DeepSeek 思考默认开，而带 tools 且不回传 reasoning_content 会被 400。

    harness（Agents SDK 的 chat-completions 适配层）不保存思考内容，所以每一轮请求体
    都必须显式带 thinking.type=disabled——这正是 Agent.model_settings → extra_body 的
    落点。这里钉的是"真的到了线上"，不是"代码里调了函数"。
    """
    sid = fresh_db["session_id"]

    writer = connect()
    try:
        _run(runner.run_turn(writer, sid, {"id": fresh_db["user_id"], "name": "张三"}))
    finally:
        writer.close()

    assert mock_openai.requests, "这一轮一个请求都没发"
    for body in mock_openai.requests:
        assert body.get("thinking") == {"type": "disabled"}, body


def test_an_upstream_5xx_seals_the_message_and_leaves_an_error_event(fresh_db, mock_openai):
    sid = fresh_db["session_id"]
    mock_openai.reset(mode="boom")

    writer = connect()
    try:
        _run(runner.run_turn(writer, sid, {"id": fresh_db["user_id"], "name": "张三"}))
    finally:
        writer.close()

    row = _agent_row(sid)
    assert row["truncated"] == 1 and row["usage_json"] is None
    events = _events(sid)
    assert [e["type"] for e in events] == ["error"]
    assert events[0]["payload"]["code"] == "upstream"
    assert events[0]["payload"]["text"] == runner.UPSTREAM_TEXT


def test_a_context_length_400_maps_to_its_own_chinese_text(fresh_db, mock_openai):
    """I3 / R-S2-31：上下文超限的 400（报文含 context/length）→ 独立中文文案。

    它仍归 upstream code（M18 的固定词汇表里没有单独的 context 码），但文案与
    UPSTREAM_TEXT 分开：那句叫人"稍后重发"，而原样重发对窗口超限必然再撞同一堵墙
    （历史只会长不会短）；CONTEXT_TEXT 叫人开新会话或只检索，才是能走通的动作。
    """
    sid = fresh_db["session_id"]
    mock_openai.reset(mode="context400")

    writer = connect()
    try:
        _run(runner.run_turn(writer, sid, {"id": fresh_db["user_id"], "name": "张三"}))
    finally:
        writer.close()

    row = _agent_row(sid)
    assert row["truncated"] == 1 and row["usage_json"] is None
    events = _events(sid)
    assert [e["type"] for e in events] == ["error"]
    assert events[0]["payload"]["code"] == "upstream"
    text = events[0]["payload"]["text"]
    assert text == runner.CONTEXT_TEXT and text != runner.UPSTREAM_TEXT
    assert "会话太长" in text


def test_a_plain_400_is_not_mistaken_for_context_length(fresh_db, mock_openai):
    """只有 400 不够：报文里没有 context/length 字样的 400 仍归 UPSTREAM_TEXT。

    _is_context_length_error 要同时看到两个词才认，否则"参数不合法"一类的 400 会被
    误报成"会话太长"，把人支去开新会话（其实换个参数就好）。
    """
    sid = fresh_db["session_id"]
    mock_openai.reset(mode="bad400")

    writer = connect()
    try:
        _run(runner.run_turn(writer, sid, {"id": fresh_db["user_id"], "name": "张三"}))
    finally:
        writer.close()

    events = _events(sid)
    assert [e["type"] for e in events] == ["error"]
    assert events[0]["payload"]["code"] == "upstream"
    assert events[0]["payload"]["text"] == runner.UPSTREAM_TEXT


def test_a_mid_stream_drop_keeps_what_already_flowed_out(fresh_db, monkeypatch):
    """spec §8：上游断流保存已流出部分 + truncated。"""
    sid = fresh_db["session_id"]
    _point_llm_at(monkeypatch, [
        _chunk_line({"role": "assistant", "content": "先说一半"}), _raise_read_error,
    ])

    writer = connect()
    try:
        _run(runner.run_turn(writer, sid, {"id": fresh_db["user_id"], "name": "张三"}))
    finally:
        writer.close()

    row = _agent_row(sid)
    assert row["content"] == "先说一半", "已流出的那一段要留在库里"
    assert row["truncated"] == 1 and row["usage_json"] is None
    events = _events(sid)
    assert [e["type"] for e in events] == ["error"]
    assert events[0]["payload"]["code"] == "upstream"


def test_an_unexpected_exception_seals_and_keeps_the_traceback_out(fresh_db, monkeypatch, caplog):
    sid = fresh_db["session_id"]

    def boom():
        raise RuntimeError("密钥没配对：DEEPSEEK_API_KEY")

    monkeypatch.setattr(runner, "make_model", boom)

    writer = connect()
    try:
        _run(runner.run_turn(writer, sid, {"id": fresh_db["user_id"], "name": "张三"}))
    finally:
        writer.close()

    row = _agent_row(sid)
    assert row["truncated"] == 1 and row["usage_json"] is None
    events = _events(sid)
    assert [e["type"] for e in events] == ["error"]
    assert events[0]["payload"] == {"code": "internal", "text": runner.INTERNAL_TEXT}
    assert "DEEPSEEK_API_KEY" not in json.dumps(events, ensure_ascii=False)
    # 异常原文**必须**进服务端日志（运维排查全靠它），只是不进落库的 payload。
    # 两边都断言，"不漏 traceback"才不至于变成"连日志都没记"。
    assert "密钥没配对：DEEPSEEK_API_KEY" in caplog.text


def test_the_explanatory_row_is_in_the_db_by_the_time_the_seal_says_done(fresh_db, mock_openai,
                                                                          monkeypatch):
    """R-S2-18：封口是一轮的最后一次写，解释行必须在它之前就落库。

    尾读的 done 判据是 `usage_json IS NOT NULL OR truncated=1`。先封口后写事件，就有
    一段"done 已满足、解释行还没落库"的窗口——Task 5 的循环可能恰好在那一瞬读到
    done 退出，「额度暂时用完…」永远到不了人眼前。这条测试在另一个线程里模仿尾读：
    看见 done 的那一瞬去读 agent_events，quota 的 system 行必须已经在。_event 前面
    垫宽窗口（睡 0.2s）：顺序要是反的，必然当场被逮住。
    """
    sid = fresh_db["session_id"]
    mock_openai.reset(mode="quota")

    real_event = runner._event

    def slow_event(turn, event_type, payload):
        time.sleep(0.2)          # 拉宽窗口：先封口后写事件的话，尾读必然掉进来
        real_event(turn, event_type, payload)

    monkeypatch.setattr(runner, "_event", slow_event)

    writer = connect()
    poller = connect()
    worker = threading.Thread(
        target=lambda: asyncio.run(
            runner.run_turn(writer, sid, {"id": fresh_db["user_id"], "name": "张三"})
        ),
        daemon=True,
    )
    events_at_done = None
    try:
        worker.start()
        for _ in range(5000):
            row = poller.execute(
                "SELECT usage_json, truncated FROM messages"
                " WHERE session_id = ? AND role = 'agent'", (sid,),
            ).fetchone()
            if row and (row["usage_json"] is not None or row["truncated"]):
                # 尾读的顺序：看见 done 之后才去拿事件——解释行必须已经在库里
                events_at_done = _events(sid)
                break
            time.sleep(0.002)
        worker.join(30)
    finally:
        poller.close()
        writer.close()

    assert not worker.is_alive(), "run_turn 没在限时内跑完"
    assert events_at_done is not None, "限时内没读到封口：尾读会永远等 done"
    assert any(
        e["type"] == "system" and e["payload"] == {"text": QUOTA_TEXT}
        for e in events_at_done
    ), f"尾读看见 done 时解释行还没落库（R-S2-18 的顺序反了）：{events_at_done}"


def test_a_model_behavior_error_tells_the_human_to_rephrase(fresh_db, monkeypatch):
    """ModelBehaviorError（模型没按规矩出牌）：说"换个说法再试"，不说"稍后再试"。

    两句文案指向的动作不同：internal 那句叫人等一会儿原样重发，而这种错原样重试
    大概率再撞同一堵墙——模型的行为与时间无关，换个说法才有出路。
    """
    sid = fresh_db["session_id"]

    async def misbehave(*_a, **_k):
        raise ModelBehaviorError("Model tried to call twice with same tool call id: call_1")

    monkeypatch.setattr(runner, "_drive", misbehave)

    writer = connect()
    try:
        _run(runner.run_turn(writer, sid, {"id": fresh_db["user_id"], "name": "张三"}))
    finally:
        writer.close()

    row = _agent_row(sid)
    assert row["truncated"] == 1 and row["usage_json"] is None
    events = _events(sid)
    assert [e["type"] for e in events] == ["error"]
    assert events[0]["payload"]["code"] == "behavior"
    text = events[0]["payload"]["text"]
    assert text == runner.BEHAVIOR_TEXT and "换个说法" in text
    assert text != runner.INTERNAL_TEXT


def test_max_turns_asks_for_a_plain_sentence_then_gives_up(fresh_db, mock_openai):
    """max_turns 超限：先不带工具再要一句人话（计划 §19），还是没有就出兜底文案。"""
    sid = fresh_db["session_id"]
    mock_openai.reset(mode="always_tool", tool_name="list_wiki", tool_args={})

    writer = connect()
    try:
        _run(runner.run_turn(writer, sid, {"id": fresh_db["user_id"], "name": "张三"},
                             max_turns=2))
    finally:
        writer.close()

    # 2 轮工具 + 1 次收尾再要一句
    assert len(mock_openai.requests) == 3
    wrap_up = mock_openai.requests[-1]
    assert wrap_up.get("tools") in (None, []), "收尾那一次不许再给工具"
    assert "直接回答" in wrap_up["messages"][-1]["content"]
    row = _agent_row(sid)
    assert row["truncated"] == 1 and row["usage_json"] is None
    events = _events(sid)
    assert events[-1] == {"seq": len(events), "type": "error",
                          "payload": {"code": "max_turns", "text": runner.MAX_TURNS_TEXT}}
    assert [e["type"] for e in events].count("tool-call") == 2


def test_a_failure_while_assembling_the_agent_leaves_no_unsealed_row(fresh_db, monkeypatch):
    """装配就炸（模型没配好一类）：不留一条没封口的行，也不抛给调用方。"""
    sid = fresh_db["session_id"]

    def boom():
        raise llm.LLMError("DEEPSEEK_API_KEY 没配")

    monkeypatch.setattr(runner, "make_model", boom)
    writer = connect()
    try:
        _run(runner.run_turn(writer, sid, {"id": fresh_db["user_id"], "name": "张三"}))
    finally:
        writer.close()

    row = _agent_row(sid)
    assert row["truncated"] == 1 and row["content"] == ""
    assert _events(sid)[0]["payload"]["code"] == "internal"


def test_run_turn_never_raises(fresh_db, monkeypatch):
    """后台任务抛出去没人接：连"封口本身都写不动"也只许记日志。"""
    sid = fresh_db["session_id"]
    monkeypatch.setattr(runner, "make_model", lambda: (_ for _ in ()).throw(RuntimeError("x")))

    def boom(*_a, **_k):
        raise RuntimeError("封口也写不动")

    monkeypatch.setattr(runner, "_seal", boom)
    writer = connect()
    try:
        _run(runner.run_turn(writer, sid, {"id": fresh_db["user_id"], "name": "张三"}))
    finally:
        writer.close()

    # 这是**唯一**一条封不上口的出口：连兜底都写不动，这一行就停在"还在跑"的形状
    # （usage_json NULL + truncated=0），留给下次启动的 db.seal_orphan_agent_messages
    # 收（N9）。它不该被当成"跑完了"——所以 usage_json 必须还是 NULL。
    row = _agent_row(sid)
    assert (row["content"], row["truncated"], row["usage_json"]) == ("", 0, None)
    # R-S2-18 把事件排在封口之前，所以这条出口上解释行**已经写进去了**（封口随后
    # 才炸）——事件先落库正是为了"封口看得见时解释一定在"。
    events = _events(sid)
    assert [e["type"] for e in events] == ["error"]
    assert events[0]["payload"]["code"] == "internal"


def test_an_event_insert_failure_cannot_skip_the_seal(fresh_db, monkeypatch):
    """I6：_event 的 INSERT 失败（磁盘满/锁死/表被人删）也不许把封口跳过。

    _fail 先记解释事件、再封口，两步曾同在一个 try 里——_event 一抛就连 _seal 一起
    跳过，那一行停在"还在跑"的形状（usage_json NULL + truncated=0），尾读永远等不到
    done，得等下次启动的 seal_orphan_agent_messages 才收。现在 _event 有自己的 try：
    事件写不进只记日志，封口照走（truncated=1）。
    """
    sid = fresh_db["session_id"]
    # 让这一轮走 internal 兜底（装配就炸），从而进 _fail
    monkeypatch.setattr(runner, "make_model",
                        lambda: (_ for _ in ()).throw(RuntimeError("装配炸了")))

    def event_boom(*_a, **_k):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(runner, "_event", event_boom)

    writer = connect()
    try:
        _run(runner.run_turn(writer, sid, {"id": fresh_db["user_id"], "name": "张三"}))
    finally:
        writer.close()

    # 封口没被 _event 的失败拖走：truncated=1、usage_json NULL（失败路径的形状）
    row = _agent_row(sid)
    assert (row["truncated"], row["usage_json"]) == (1, None), row
    # 解释事件确实没写进去（_event 炸了），但那一行照样封了口
    assert _events(sid) == []


# --- 超轮：收尾那一次（计划 §19） -----------------------------------------------


def test_max_turns_recovers_when_the_wrap_up_call_returns_plain_text(fresh_db, mock_openai):
    """收尾那一次真拿到了人话：这一轮算**正常收尾**，不按"断了"渲染。

    超轮不是故障，是模型没收住；不带工具再要一句，它往往就给了。给了就该按跑完了
    封口（usage_json='{}'、truncated=0），正文里那句收尾就是这一轮完整的回复——
    标成 truncated=1 的话前端会补一句「（回复中断）」，人以为回复被掐了。
    """
    sid = fresh_db["session_id"]
    mock_openai.reset(mode="always_tool", tool_name="list_wiki", wrap_up="text")

    writer = connect()
    try:
        _run(runner.run_turn(writer, sid, {"id": fresh_db["user_id"], "name": "张三"},
                             max_turns=2))
    finally:
        writer.close()

    assert len(mock_openai.requests) == 3            # 2 轮工具 + 1 次收尾
    wrap_up = mock_openai.requests[-1]
    assert wrap_up.get("tools") in (None, []), "收尾那一次不许再给工具"
    assert wrap_up["messages"][-1] == {"role": "user", "content": runner.WRAP_UP_TEXT}
    # 收尾也是一个回答：system 指令里要有“聊天正文不是 wiki 页面”的格式约束，
    # 否则超轮后的那句会变成“还没写成页 / 本轮未改动”的内部汇报。
    assert "【回答格式】" in wrap_up["messages"][0]["content"]
    # 收尾那一次喂的是上一跑**真实烧掉的输入**（result.to_input_list()）：原上下文 +
    # 两轮工具调用与结果都在，不是光一句 WRAP_UP_TEXT 的泛泛开场——模型没见过自己
    # 刚才干了什么，就没法把已经查到的内容收成回答。
    roles = [m["role"] for m in wrap_up["messages"]]
    assert roles.count("tool") == 2, wrap_up["messages"]
    assert any(m["role"] == "assistant" and m.get("tool_calls") for m in wrap_up["messages"])
    assert f"[消息{fresh_db['human_id']}]" in wrap_up["messages"][1]["content"]

    row = _agent_row(sid)
    # 工具那两轮一个字正文都没流，所以正文就是收尾那一句
    assert row["content"] == "".join(WRAP_UP_REPLY)
    assert row["truncated"] == 0 and json.loads(row["usage_json"]) == {}
    # 收尾那一次没有工具，所以不许多出工具事件：还是两轮工具各自的 call→result
    assert [e["type"] for e in _events(sid)] == ["tool-call", "tool-result"] * 2
    assert not any(e["type"] in ("system", "error") for e in _events(sid))


def test_a_429_on_the_wrap_up_call_is_reported_as_quota_not_as_max_turns(fresh_db, mock_openai):
    """收尾那一次照样要打上游：它撞 429 时人该看到"额度用完"，不是"工具调得太多次"。

    两句文案指向的动作是相反的——"额度用完"叫人等一会儿原样重发（前端据此把会话标
    "待重试"），"工具调得太多次"叫人换个说法再试，而换说法对额度一点用没有。所以
    _drive 里收尾那一段的 except 不许把 openai 的异常吞成 MaxTurnsExceeded。
    """
    sid = fresh_db["session_id"]
    mock_openai.reset(mode="always_tool", tool_name="list_wiki", wrap_up="quota")

    writer = connect()
    try:
        _run(runner.run_turn(writer, sid, {"id": fresh_db["user_id"], "name": "张三"},
                             max_turns=2))
    finally:
        writer.close()

    assert len(mock_openai.requests) == 3
    row = _agent_row(sid)
    assert row["truncated"] == 1 and row["usage_json"] is None
    events = _events(sid)
    assert events[-1] == {"seq": len(events), "type": "system",
                          "payload": {"text": QUOTA_TEXT}}
    assert not any(e["payload"].get("code") == "max_turns" for e in events), events


# --- 取消：CancelledError 是 BaseException，except Exception 接不住 ---------------


def test_a_cancelled_turn_is_sealed_and_leaves_an_error_event(fresh_db, monkeypatch):
    """进程内被取消（Task 5 的关停一类）：照样封口、照样记事件，然后把取消原样抛回去。

    吞掉取消会让 asyncio 的关停流程以为这个任务已经结束；不封口则留下一行
    usage_json NULL + truncated=0 的"还在跑"，得等下次启动才被 db 那边收掉——
    而这一轮明明是**这一进程**里结束的，没有理由留给下一次启动。
    """
    sid = fresh_db["session_id"]
    entered = threading.Event()

    async def stall():
        entered.set()
        await asyncio.sleep(30)          # 卡在流中间，等测试那头来取消

    _point_llm_at(monkeypatch, [
        _chunk_line({"role": "assistant", "content": "先说一半"}), stall,
    ])

    async def scenario():
        writer = connect()
        try:
            task = asyncio.create_task(
                runner.run_turn(writer, sid, {"id": fresh_db["user_id"], "name": "张三"})
            )
            for _ in range(1000):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entered.is_set(), "上游没走到卡住那一步，这条测试什么也没测到"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            writer.close()

    _run(scenario())

    row = _agent_row(sid)
    assert row["content"] == "先说一半", "已经流出来的那一段要留在库里"
    assert row["truncated"] == 1 and row["usage_json"] is None
    events = _events(sid)
    assert [e["type"] for e in events] == ["error"]
    assert events[0]["payload"] == {"code": "cancelled", "text": runner.CANCELLED_TEXT}


# --- 无参工具：payload 里没有 args 这个键（裁定 R-S2-12 / T4a §1.3） --------------


def test_an_empty_arguments_string_records_a_tool_call_without_an_args_key(fresh_db,
                                                                          mock_openai):
    """线上交来个空 arguments 串：hooks 不记 args 键，runner 也照样把这一轮跑完。

    无参工具（list_wiki）上游就是这么交的。留 `{"name": …}` 一种形状，S3 的工具卡片
    `"args" in payload` 一判就够；runner 这边从不读 payload，所以"少一个键"对它必须
    是完全无感的——这一条钉的就是这个无感（T4a 的关切 2）。
    """
    sid = fresh_db["session_id"]
    mock_openai.reset(tool_name="list_wiki", tool_args_raw="")

    writer = connect()
    try:
        _run(runner.run_turn(writer, sid, {"id": fresh_db["user_id"], "name": "张三"}))
    finally:
        writer.close()

    events = _events(sid)
    assert [e["type"] for e in events] == ["tool-call", "tool-result"]
    assert events[0]["payload"] == {"id": "call_1", "name": "list_wiki"}
    assert "args" not in events[0]["payload"]
    assert events[1]["payload"]["name"] == "list_wiki"
    # 工具真跑了（空串被 SDK 当成 {} 交给无参工具），这一轮也正常收了口
    assert events[1]["payload"]["ok"] is True
    row = _agent_row(sid)
    assert row["content"] == "".join(TURN1_TEXT) + "".join(TURN2_TEXT)
    assert row["truncated"] == 0 and json.loads(row["usage_json"]) == {}
    # 参数增量（ResponseFunctionCallArgumentsDeltaEvent 也有 .delta）一个字都没混进正文
    assert "query" not in row["content"] and "{}" not in row["content"]


# --- 与 db 那边启动封口的接缝（N9 / T4a §4 末那条口径提醒） -----------------------


def test_a_restart_never_re_seals_a_row_the_runner_already_closed(fresh_db, mock_openai):
    """runner 封过的口，下次启动一行都不许再动。

    同一场会话里跑两轮：第一轮正常收尾（usage_json='{}'、truncated=0），第二轮撞 429
    失败（usage_json=NULL、truncated=1）。启动封口只挑 `usage_json IS NULL AND
    truncated=0`，所以这两行它一行都不算"这次新封的"；补文案那句只碰失败那行。
    **区分"跑完了"与"断了"靠 truncated，不靠 usage_json 是不是 NULL**——失败路径
    写 NULL 是有意的（真没有用量可记），它靠 truncated=1 收尾。
    """
    sid = fresh_db["session_id"]
    writer = connect()
    try:
        _run(runner.run_turn(writer, sid, {"id": fresh_db["user_id"], "name": "张三"}))
        mock_openai.reset(mode="quota")
        _run(runner.run_turn(writer, sid, {"id": fresh_db["user_id"], "name": "张三"}))
    finally:
        writer.close()

    sql = ("SELECT content, truncated, usage_json FROM messages"
           " WHERE session_id = ? AND role = 'agent' ORDER BY seq")
    before = _read(sql, (sid,))
    assert [(r["truncated"], r["usage_json"]) for r in before] == [(0, "{}"), (1, None)]
    assert SEAL_MARK not in before[0]["content"]

    sealer = connect()
    try:
        assert seal_orphan_agent_messages(sealer) == 0, "runner 封过的行被当成半截又封了一次"
        sealer.commit()
    finally:
        sealer.close()

    after = _read(sql, (sid,))
    assert [(r["truncated"], r["usage_json"]) for r in after] == [(0, "{}"), (1, None)]
    # 跑完了的那行一个字都不动；失败那行只补上「（回复中断）」
    assert after[0]["content"] == before[0]["content"]
    assert after[1]["content"] == SEAL_MARK
