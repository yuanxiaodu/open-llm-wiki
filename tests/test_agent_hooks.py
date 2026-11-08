"""提示词与 RunHooks：白名单第三道防线 + 工具事件落 agent_events（裁定 R-S2-9）。

两个回调直接 await 着打，不起真 runner：起 runner 要一个会说 chat-completions SSE 的
假模型服务器（那是 Task 4 的活），而 SDK 0.24.0 交进这两个回调的入参是死的，实证在
`agents/run_internal/tool_execution.py`：

- `_execute_single_tool_body` 给 on_tool_start 的 `context` 就是 ToolContext
  （runner 的 `context=` 那个对象挂在 `.context` 上）、`tool` 就是 FunctionTool 本尊；
- 同一条链尾的 `on_tool_end(tool_context, agent, func_tool, final_result)` 里
  `final_result` 就是工具函数的返回值本身（我们五个工具一律返 str）。

所以这里用 ToolContext 包一个 AgentContext 照着真跑的形状打，测的就是真行为。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from types import SimpleNamespace

import pytest
from agents.tool_context import ToolContext

from app.agent.hooks import WikiHooks
from tests.conftest import make_session

LOGGER = "openllmwiki.agent"


class _BoomConn:
    """一条一碰就炸的连接：库锁死、连接被关、表被人删了，都是这个样子。"""

    def execute(self, *_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    def commit(self):
        raise sqlite3.OperationalError("database is locked")


def _ctx(conn, session_id: int, tool_name: str = "search", actor: str = "张三",
         arguments: dict | None = None, raw_arguments: str | None = None,
         message_id: int | None = None, call_id: str = "t1") -> ToolContext:
    """照着 runner 真跑时的形状造一个 ToolContext：context= 交的是 AgentContext。

    `arguments` 是 dict（照工具入参的形状 json.dumps 一遍，SDK 交进来的就是这串）；
    `raw_arguments` 给了就原样用——无参工具那条要交一个**空串**，那不是"没造参数"，
    是"上游真给了个空的"，两种形状在 hooks 里走的是同一条 getattr。
    `message_id` 默认 None（AgentContext 的默认）；真跑时 runner 恒传（裁定 R-S2-20）。
    """
    from app.agent.context import AgentContext

    if raw_arguments is None:
        raw_arguments = json.dumps(
            {"query": "报名流程线上化"} if arguments is None else arguments, ensure_ascii=False
        )
    return ToolContext(
        context=AgentContext(conn=conn, session_id=session_id, actor_name=actor,
                             message_id=message_id),
        tool_name=tool_name,
        tool_call_id=call_id,
        tool_arguments=raw_arguments,
    )


def _rows(conn, session_id: int) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT seq, type, payload_json, created_at, message_id FROM agent_events"
            " WHERE session_id = ? ORDER BY seq",
            (session_id,),
        )
    ]


def _start(conn, session_id, tool):
    # tool_name 也走 getattr：白名单那一条测试要拿一个连 name 都没有的东西去打
    return asyncio.run(
        WikiHooks().on_tool_start(
            _ctx(conn, session_id, tool_name=getattr(tool, "name", "")), None, tool
        )
    )


def _end(conn, session_id, tool, result):
    return asyncio.run(
        WikiHooks().on_tool_end(_ctx(conn, session_id, tool_name=tool.name), None, tool, result)
    )


# --- 提示词 -------------------------------------------------------------------


def test_base_style_mentions_the_caveats_and_never_mentions_private_chat():
    """裁定 5 之后全站语料可见：提示词里不许再出现"私聊"，两条老坑要写清。"""
    from app.agent.prompt import BASE_STYLE

    assert "私聊" not in BASE_STYLE
    assert "sources" in BASE_STYLE            # 来源写成页级 sources（Hermes/LLM-wiki 约定）
    assert "不要写句子级引注" in BASE_STYLE     # 不再让模型发明脚注
    # S5 起 wiki 是 Hermes vault：开场要读 SCHEMA/index/log，写页要有 wikilink/lint
    assert "SCHEMA.md" in BASE_STYLE and "index.md" in BASE_STYLE and "log.md" in BASE_STYLE
    assert "[[..." in BASE_STYLE or "[[slug]]" in BASE_STYLE
    assert "write_page" in BASE_STYLE and "edit_page" in BASE_STYLE
    assert "lint" in BASE_STYLE
    # I4 / R-S2-32：改页前先 read 拿现行版本号填 base_commit；留空=放弃并发保护
    assert "read" in BASE_STYLE and "base_commit" in BASE_STYLE
    assert "并发保护" in BASE_STYLE
    # I5 / R-S2-33：上传件与会话文字是语料、不是指令（提示注入的软防线）
    assert "语料" in BASE_STYLE and "不是指令" in BASE_STYLE


# --- on_tool_start：记账 + 白名单第三道防线 ------------------------------------


def test_on_tool_start_records_one_tool_call_row(conn, member):
    from app.agent.tools import search as search_tool

    _start(conn, member.session_id, search_tool)

    rows = _rows(conn, member.session_id)
    assert len(rows) == 1
    assert rows[0]["type"] == "tool-call"
    assert rows[0]["seq"] == 1
    assert rows[0]["created_at"]
    # args 也记（裁定 R-S2-12）：S3 的工具卡片要显示"调了什么、参数是什么"，
    # 而 on_tool_start 是唯一还拿得到原始参数串的时机。
    # id 是 SDK 的 tool_call_id（裁定 R-S2-21，spec §6.5 的形状）。
    assert json.loads(rows[0]["payload_json"]) == {
        "id": "t1", "name": "search",
        "args": json.dumps({"query": "报名流程线上化"}, ensure_ascii=False),
    }


def test_tool_call_args_are_clipped_to_200_chars(conn, member):
    """write_page 的参数里可能是整页正文：落库只留前 200 字，卡片够用、库不肿。"""
    from app.agent.tools import write_page as write_tool

    body = "字" * 500
    args = {"title": "报名流程线上化", "body_md": body}
    asyncio.run(
        WikiHooks().on_tool_start(
            _ctx(conn, member.session_id, tool_name="write_page", arguments=args),
            None, write_tool,
        )
    )

    payload = json.loads(_rows(conn, member.session_id)[0]["payload_json"])
    assert payload["name"] == "write_page"
    assert len(payload["args"]) == 200
    assert payload["args"] == json.dumps(args, ensure_ascii=False)[:200]


def test_a_tool_call_without_arguments_records_only_the_name(conn, member):
    """没有参数串就整个 `args` 键不记（无参工具那边模型可能交进来一个空串）。

    记一个 `"args": ""` 的话，S3 的工具卡片要同时判"键在不在"与"值空不空"两种形状；
    只留 `{id, name}` 一种，前端 `"args" in payload` 一判就够。
    """
    from app.agent.tools import list_wiki

    asyncio.run(
        WikiHooks().on_tool_start(
            _ctx(conn, member.session_id, tool_name="list_wiki", raw_arguments=""),
            None, list_wiki,
        )
    )

    assert json.loads(_rows(conn, member.session_id)[0]["payload_json"]) == {
        "id": "t1", "name": "list_wiki",
    }


def test_every_whitelisted_tool_passes_and_seq_climbs(conn, member):
    """五个工具一个不落；每次新建一个 WikiHooks 也不会把 seq 数回 1（seq 在库里定）。"""
    from app.agent.tools import WIKI_TOOLS

    for tool in WIKI_TOOLS:
        _start(conn, member.session_id, tool)

    rows = _rows(conn, member.session_id)
    assert [r["seq"] for r in rows] == list(range(1, len(WIKI_TOOLS) + 1))
    assert [json.loads(r["payload_json"])["name"] for r in rows] == [t.name for t in WIKI_TOOLS]


def test_on_tool_start_rejects_a_tool_outside_the_whitelist(conn, member, caplog):
    """第三道防线：store 与工具内部各有一道，这里再挡一次并留日志，然后抛给 runner 兜底。

    抛的是中文 RuntimeError（runner 的 catch-all 会把它记成一条 error 事件并封口），
    拒了就不许留下一条 tool-call 行——不然 SSE 上会出现"正在调 delete_everything"。
    """
    alien = SimpleNamespace(name="delete_everything")

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        with pytest.raises(RuntimeError) as caught:
            _start(conn, member.session_id, alien)

    message = str(caught.value)
    assert "白名单" in message and "delete_everything" in message
    assert _rows(conn, member.session_id) == []
    ours = [r for r in caplog.records if r.name == LOGGER]
    assert ours and ours[0].levelno == logging.ERROR
    assert "delete_everything" in ours[0].getMessage()


def test_a_tool_without_a_name_is_rejected_too(conn, member):
    """连名字都没有的东西更不该跑：getattr 兜住 AttributeError，仍走白名单那一道。"""
    with pytest.raises(RuntimeError):
        _start(conn, member.session_id, SimpleNamespace())


# --- on_tool_end：ok 与 summary ------------------------------------------------


def test_on_tool_end_records_ok_and_a_clipped_summary(conn, member):
    from app.agent.tools import FAIL_PREFIX, search as search_tool

    _end(conn, member.session_id, search_tool, "搜到 3 条（范围 all）：\n【消息 12】张三：…")
    _end(conn, member.session_id, search_tool, f"{FAIL_PREFIX}：这段引文和原话对不上")
    _end(conn, member.session_id, search_tool, "已生效：slug=baoming v1\n" + "字" * 500)

    rows = _rows(conn, member.session_id)
    assert [r["type"] for r in rows] == ["tool-result"] * 3
    assert [r["seq"] for r in rows] == [1, 2, 3]
    payloads = [json.loads(r["payload_json"]) for r in rows]
    assert payloads[0] == {
        "id": "t1", "name": "search", "ok": True,
        "summary": "搜到 3 条（范围 all）：\n【消息 12】张三：…",
    }
    # ok 的判据就是"返回串不以 tools.FAIL_PREFIX 开头"（Task 2 定死的约定，runner 也用同一条）
    assert payloads[1]["ok"] is False
    assert payloads[1]["summary"].startswith(f"{FAIL_PREFIX}：")
    assert payloads[2]["ok"] is True and len(payloads[2]["summary"]) == 200


def test_a_real_tool_failure_string_is_judged_not_ok(conn, member):
    """裁定 R-S2-14：hooks 判 ok 的前缀与工具真吐出来的失败前缀是**同一个常量**。

    上一条里的失败串是照约定拼的（前缀取自 tools.FAIL_PREFIX）；这一条不拼：真调一次
    edit_page 让它自己在 slug 形状闸上失败（碰不到 git，也不需要 tmp_wiki），把它的返回
    串原样交给 on_tool_end。前缀若在 tools 与 hooks 之间漂了（两边各写一份字面量的老毛病），
    一次写砸了就会被记成 ok=True——SSE 上那一格从此不亮红，而且没有任何测试会红。
    """
    from app.agent.tools import FAIL_PREFIX, edit_page

    args = {"path": "../x.md", "body_md": "正文"}
    ctx = _ctx(conn, member.session_id, tool_name="edit_page", arguments=args)
    failure = asyncio.run(edit_page.on_invoke_tool(ctx, json.dumps(args, ensure_ascii=False)))
    assert failure.startswith(FAIL_PREFIX) and "没有这一页" in failure

    _end(conn, member.session_id, edit_page, failure)

    payload = json.loads(_rows(conn, member.session_id)[0]["payload_json"])
    assert payload == {"id": "t1", "name": "edit_page", "ok": False, "summary": failure[:200]}


def test_a_non_str_result_is_stringified_not_crashed(conn, member):
    """SDK 的 result 类型标的是 object（结构化输出的工具可以返别的形状）：不许炸。"""
    from app.agent.tools import search as search_tool

    _end(conn, member.session_id, search_tool, None)

    payload = json.loads(_rows(conn, member.session_id)[0]["payload_json"])
    assert payload["ok"] is True and payload["summary"] == ""


# --- seq 的口径 ----------------------------------------------------------------


def test_seq_is_numbered_per_session(conn, member):
    """两场会话各自从 1 数：SSE 尾读的 `id:` 是 (session_id, seq)，跨会话不共用一条数。"""
    from app.agent.tools import list_wiki

    other = make_session(conn, member.user_id, "第二场")
    for session_id in (member.session_id, other, member.session_id):
        _start(conn, session_id, list_wiki)

    assert [r["seq"] for r in _rows(conn, member.session_id)] == [1, 2]
    assert [r["seq"] for r in _rows(conn, other)] == [1]


def test_events_are_committed_so_another_connection_sees_them(member):
    """hooks 自己 commit：runner 是后台任务，没有 get_conn 那道"请求结束才提交"的收尾。

    不 commit 的话 SSE 尾读走的是另一条连接，一整轮跑完之前一条事件都看不见，
    前端只能干等——这正是 Task 2 的工具自己 commit 的同一个理由。

    **写侧必须是 `connect()`（默认隔离级别），不能用 conftest 的 `conn` fixture**：
    那条是 isolation_level=None 的 autocommit，INSERT 一执行行就落盘了，把 append_event
    末尾那句 conn.commit() 删掉这条测试照样绿（fix round 1 之前就是这个空转形状）。
    Task 4 的 runner 用的正是这种默认隔离级别的连接，尾读另开一条——下面照着这个形状打。
    """
    from app.agent.tools import list_wiki
    from app.db import connect

    writer = connect()          # 默认 isolation_level：DML 之后行还压在事务里
    reader = connect()          # SSE 尾读那一侧：另一条连接
    try:
        writer.execute("DELETE FROM agent_events")
        writer.commit()
        # 这两条断言是承重的：写侧若不是 autocommit，"另一条连接看得见"才等于"commit 跑了"
        assert writer.isolation_level is not None
        assert reader.execute("SELECT COUNT(*) AS n FROM agent_events").fetchone()["n"] == 0

        _start(writer, member.session_id, list_wiki)   # 只在 append_event 返回之后才读

        rows = reader.execute(
            "SELECT seq, type FROM agent_events WHERE session_id = ? ORDER BY seq",
            (member.session_id,),
        ).fetchall()
        assert [(r["seq"], r["type"]) for r in rows] == [(1, "tool-call")]
    finally:
        writer.close()
        reader.close()


# --- 记账失败绝不拖垮这一轮跑 --------------------------------------------------


def test_a_broken_event_write_does_not_break_the_run(member, caplog):
    """agent_events 写不成（库锁死/表被删/连接被关）只记日志，工具照跑、这一轮照跑。"""
    from app.agent.tools import search as search_tool

    boom = _BoomConn()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _start(boom, member.session_id, search_tool)
        _end(boom, member.session_id, search_tool, "已生效：slug=baoming v1")

    ours = [r for r in caplog.records if r.name == LOGGER]
    assert len(ours) == 2, [r.getMessage() for r in caplog.records]
    assert all(r.levelno == logging.WARNING for r in ours)
    assert all("agent_events" in r.getMessage() for r in ours)


def test_the_whitelist_still_fires_when_the_event_write_is_broken(member):
    """上一条例外的那一条：白名单的 RuntimeError 不许被"记账失败"的 except 一起吞掉。"""
    with pytest.raises(RuntimeError):
        _start(_BoomConn(), member.session_id, SimpleNamespace(name="delete_everything"))


def test_a_context_that_is_not_agent_context_is_logged_not_raised(conn, member, caplog):
    """runner 若把裸 conn 当 context= 交进来：记一条 warning，这一轮跑照旧不断。

    同一轮里工具那一侧会硬读 `.conn` 报 AttributeError 并被 failure_error_function
    翻成中文（见 tools._conn 与下一条测试），两头都不静默，看日志就知道是装配错了。
    """
    from app.agent.hooks import WikiHooks
    from app.agent.tools import search as search_tool

    ctx = ToolContext(context=conn, tool_name="search", tool_call_id="t1", tool_arguments="{}")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        asyncio.run(WikiHooks().on_tool_start(ctx, None, search_tool))

    ours = [r for r in caplog.records if r.name == LOGGER]
    assert ours and any("AgentContext" in r.getMessage() for r in ours)
    assert _rows(conn, member.session_id) == []


def test_the_tool_side_of_a_mis_wired_context_fails_in_chinese(conn):
    """上一条的另一半：装配错了，工具也不静默——返一句中文，不外抛。

    这两条一起钉住"tools 硬读 .conn"的代价是可观测的：hooks 那头 warning、
    工具这头中文错误串（模型看得到、能重试），不会出现"工具跑成了、事件一条没记"。
    """
    from app.agent.tools import list_wiki

    ctx = ToolContext(
        context=conn, tool_name="list_wiki", tool_call_id="t1", tool_arguments="{}"
    )
    out = asyncio.run(list_wiki.on_invoke_tool(ctx, "{}"))
    assert out.startswith("读不到：") and "AttributeError" in out


# --- call id 与 message_id（裁定 R-S2-21 / R-S2-20） -----------------------------


def test_the_tool_call_and_its_result_share_the_same_call_id(conn, member):
    """R-S2-21：SDK 交进两个回调的是**同一个** ToolContext，call_id 两头都拿得到。

    于是 tool-call 与 tool-result 的 payload 各带一个 `"id"` 键且相等（spec §6.5 的
    形状）：S3 的工具卡片按 id 配对，不用按"名字 + 出现顺序"猜——同名工具一轮里
    调两次时，按顺序猜会在两条 result 之间配错。
    """
    from app.agent.tools import search as search_tool

    ctx = _ctx(conn, member.session_id, tool_name="search", call_id="call_9527")
    asyncio.run(WikiHooks().on_tool_start(ctx, None, search_tool))
    asyncio.run(WikiHooks().on_tool_end(ctx, None, search_tool, "搜到 1 条"))

    rows = _rows(conn, member.session_id)
    assert [r["type"] for r in rows] == ["tool-call", "tool-result"]
    call, result = (json.loads(r["payload_json"]) for r in rows)
    assert call["id"] == result["id"] == "call_9527"


def test_events_carry_the_message_id_from_the_context(conn, member):
    """R-S2-20：事件行归到具体的 agent 消息——ctx 里有 message_id 就写进那一列。

    SSE 尾读按 `message_id = 当前 agent 行的 id` 划一轮的边界；不写这一列，
    同秒内连着跑的两轮就会把上一轮的事件尾巴再发一遍（tests/test_stream_api.py
    钉着那个形状）。
    """
    from app.agent.tools import search as search_tool

    ctx = _ctx(conn, member.session_id, tool_name="search", message_id=77)
    asyncio.run(WikiHooks().on_tool_start(ctx, None, search_tool))
    asyncio.run(WikiHooks().on_tool_end(ctx, None, search_tool, "搜到 1 条"))

    rows = _rows(conn, member.session_id)
    assert [r["message_id"] for r in rows] == [77, 77]


def test_a_context_without_message_id_records_null(conn, member):
    """ctx 里没有 message_id（AgentContext 的默认）→ 那一列落 NULL，不炸。

    NULL 行由尾读退回 created_at 口径（legacy 兼容，chat._pending_events 的注释）；
    hooks 这头只管"有就写、没有就 NULL"，不为此报错——报错会打断这一轮跑，
    而记账失败绝不打断跑是本文件的第一条硬规矩。
    """
    from app.agent.tools import search as search_tool

    _start(conn, member.session_id, search_tool)     # _ctx 的 message_id 默认 None

    rows = _rows(conn, member.session_id)
    assert len(rows) == 1 and rows[0]["message_id"] is None


# --- 清表（裁定 R-S2-11） ------------------------------------------------------


def test_agent_events_is_wiped_between_tests_so_no_local_fixture_is_needed(conn, member):
    """本文件原来那个自己 `DELETE FROM agent_events` 的 events fixture 能删，靠的是这一条。

    两条断言各管一半：TABLES 里有这张表是"清单没漂"，开局 0 行是"清表真跑了"——
    DATA_DIR 整轮测试共用，本文件前面那些测试都往 agent_events 写过行，不清的话
    这里读到的就是上一条测试留下的行数（那时 seq 断言会被顶歪，只能各自 DELETE 一遍）。

    **位置是承重的**：必须排在写过事件的测试之后，否则第二条断言什么也没测到。
    """
    from tests.conftest import TABLES

    assert "agent_events" in TABLES
    assert conn.execute("SELECT COUNT(*) AS n FROM agent_events").fetchone()["n"] == 0
