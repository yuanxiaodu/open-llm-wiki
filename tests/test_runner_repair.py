"""收尾修复轮（runner._repair_lint）：lint 有 error 就再要一次修复，修不掉就记待修状态。

这里不碰真上游：`_stream_once` 与 `lint_errors` 都打桩，只验**决策与留痕**——
- 没写过 wiki 的回合不花这笔钱；
- 写过、lint 有 error → 起一次修复轮，输入里带全部 error 的 path/message；
- 修完干净 → 静默（不往正文/事件里塞"错误没修"）；
- 修完还有 → 记一条 system 事件的待修状态（数据层），而不是让模型在回复里提一嘴。
"""

from __future__ import annotations

import asyncio
import json
import threading

from app.agent import runner
from app.agent.context import AgentContext

ERROR = {"severity": "error", "path": "concepts/offer.md", "message": "[[page-2026-10-26-2]] 找不到"}


class _Result:
    """RunResult 的最小替身：修复轮只需要它的 to_input_list()。"""

    def to_input_list(self) -> list[dict]:
        return [{"role": "user", "content": "上一轮的输入"}]


def _turn(conn, session_id: int) -> runner._Turn:
    return runner._Turn(
        conn=conn, session_id=session_id, db_lock=threading.Lock(),
        message_id=0, history=[],
    )


def _ctx(conn, session_id: int, *, wrote: bool) -> AgentContext:
    return AgentContext(conn=conn, session_id=session_id, actor_name="张三",
                        progress={"wrote": wrote})


def _capture_stream(monkeypatch) -> list[tuple]:
    calls: list[tuple] = []

    async def fake_stream(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(runner, "_stream_once", fake_stream)
    return calls


def _system_events(conn, session_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT payload_json FROM agent_events"
        " WHERE session_id = ? AND type = 'system' ORDER BY seq",
        (session_id,),
    ).fetchall()
    return [json.loads(row["payload_json"]) for row in rows]


def test_repair_runs_once_with_all_errors_and_stays_quiet_when_clean(conn, member, monkeypatch):
    calls = _capture_stream(monkeypatch)
    replies = [[ERROR], []]          # 修复前有一条，修复后干净
    monkeypatch.setattr(runner, "lint_errors", lambda _conn: replies.pop(0))
    ctx = _ctx(conn, member.session_id, wrote=True)

    asyncio.run(runner._repair_lint(
        ctx, _turn(conn, member.session_id), _Result(),
    ))
    # 收尾成果卡要用这个数：修干净了记 0。
    assert ctx.progress["lint_errors"] == 0

    assert len(calls) == 1
    assert {tool.name for tool in calls[0][0][0].tools} == {"read", "list_wiki", "search", "edit_page", "lint"}
    inputs = calls[0][0][6]          # _stream_once(agent, ctx, turn, config, max_turns, hooks, inputs)
    prompt = inputs[-1]["content"]
    assert "只修这些" in prompt and "concepts/offer.md" in prompt
    # 修干净了就不该有"待修"事件（正文里也不该出现——那是修复轮到模型自己说）。
    assert _system_events(conn, member.session_id) == []


def test_repair_records_pending_state_when_errors_remain(conn, member, monkeypatch):
    _capture_stream(monkeypatch)
    monkeypatch.setattr(runner, "lint_errors", lambda _conn: [ERROR])
    ctx = _ctx(conn, member.session_id, wrote=True)

    asyncio.run(runner._repair_lint(
        ctx, _turn(conn, member.session_id), _Result(),
    ))
    assert ctx.progress["lint_errors"] == 1

    events = _system_events(conn, member.session_id)
    assert len(events) == 1
    assert "还有 1 处" in events[0]["text"] and "concepts/offer.md" in events[0]["text"]


def test_repair_is_skipped_when_the_turn_did_not_touch_the_wiki(conn, member, monkeypatch):
    calls = _capture_stream(monkeypatch)
    linted: list[int] = []
    monkeypatch.setattr(runner, "lint_errors", lambda _c: linted.append(1) or [])

    asyncio.run(runner._repair_lint(
        _ctx(conn, member.session_id, wrote=False),
        _turn(conn, member.session_id), _Result(),
    ))

    assert calls == [] and linted == []


def test_repair_is_skipped_without_a_run_result(conn, member, monkeypatch):
    """收尾那一次连 RunResult 都没拿到（理论上不会）时，不自检、不修。"""
    calls = _capture_stream(monkeypatch)
    linted: list[int] = []
    monkeypatch.setattr(runner, "lint_errors", lambda _c: linted.append(1) or [])

    asyncio.run(runner._repair_lint(
        _ctx(conn, member.session_id, wrote=True),
        _turn(conn, member.session_id), None,
    ))

    assert calls == [] and linted == []


# --- 收尾成果事件（outcome） -------------------------------------------------


def _tool_result(conn, turn, name: str, summary: str, *, ok: bool = True) -> None:
    from app.agent.hooks import append_event

    append_event(conn, turn.session_id, "tool-result",
                 {"name": name, "ok": ok, "summary": summary}, message_id=turn.message_id)


def _outcomes(conn, session_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT payload_json FROM agent_events"
        " WHERE session_id = ? AND type = 'system' ORDER BY seq",
        (session_id,),
    ).fetchall()
    payloads = [json.loads(r["payload_json"]) for r in rows]
    return [p for p in payloads if p.get("kind") == "outcome"]


def test_outcome_is_skipped_when_the_turn_did_not_touch_the_wiki(conn, member):
    turn = _turn(conn, member.session_id)
    _tool_result(conn, turn, "write_page", "已生效：path=concepts/a.md v1")
    runner._record_outcome(_ctx(conn, member.session_id, wrote=False), turn)
    assert _outcomes(conn, member.session_id) == []


# --- 零写入声明核对（2026-10-30 会话 27 的假合并） ----------------------------


def _agent_message(conn, session_id: int, content: str) -> int:
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM messages WHERE session_id = ?",
        (session_id,),
    ).fetchone()["n"]
    cur = conn.execute(
        "INSERT INTO messages (session_id, seq, role, content, created_at, truncated)"
        " VALUES (?, ?, 'agent', ?, '2026-10-30T00:00:00+00:00', 0)",
        (session_id, seq, content),
    )
    return int(cur.lastrowid)


def test_claim_guard_flags_claims_with_zero_page_writes(conn, member):
    mid = _agent_message(
        conn, member.session_id,
        "monica 三页已经合并完成：monica.md 升到 v2，另外两页已删除。",
    )
    turn = runner._Turn(
        conn=conn, session_id=member.session_id, db_lock=threading.Lock(),
        message_id=mid, history=[],
    )
    runner._claim_guard(turn, None)   # outcome=None = 这一轮没有任何页改动

    events = _system_events(conn, member.session_id)
    assert len(events) == 1
    assert "没有实际写入知识库" in events[0]["text"]
    assert "model" not in events[0]["text"]   # 人话，不暴露内部词


def test_claim_guard_stays_quiet_when_pages_were_really_written(conn, member):
    mid = _agent_message(conn, member.session_id, "monica 三页已经合并完成。")
    turn = runner._Turn(
        conn=conn, session_id=member.session_id, db_lock=threading.Lock(),
        message_id=mid, history=[],
    )
    runner._claim_guard(turn, {
        "kind": "outcome",
        "pages": [{"path": "entities/monica.md", "action": "edit"}],
        "citations": 3,
    })
    assert _system_events(conn, member.session_id) == []


def test_claim_guard_ignores_plain_answers(conn, member):
    mid = _agent_message(conn, member.session_id, "体检做完了：0 error，182 warn。")
    turn = runner._Turn(
        conn=conn, session_id=member.session_id, db_lock=threading.Lock(),
        message_id=mid, history=[],
    )
    runner._claim_guard(turn, None)
    assert _system_events(conn, member.session_id) == []
