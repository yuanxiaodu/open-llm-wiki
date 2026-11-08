"""启动封口（计划 N9 / spec §8）：上一次没写完的 agent 行不许留成"永远在等结尾"的形状。

`usage_json IS NULL AND truncated = 0` 是"这一轮还在跑"的唯一记号，SSE 尾读据此决定
要不要发 done；进程在生成中途被杀，这个记号就再没人来清了，重启时由
`db.seal_orphan_agent_messages` 全量封掉（启动时没有活着的生成任务，所以安全）。

封口是两件事，分开验：

1. 标 `truncated = 1`（尾读不再等）；
2. 正文尾巴补「（回复中断）」（spec §8：前端要看得出这一行是断的，不是智能体真说了半句）。

单测那几条用 conftest 的 `conn`（autocommit，插进去的行立刻可见；那几句 `conn.commit()`
是保险，将来 fixture 改了隔离级别也不塌）。最后一条走真形状：`connect()` 写、
lifespan 里 `connect()` 封、另开一条连接读——与 Task 5 的 SSE 尾读同一条缝。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.db import SEAL_MARK, connect, seal_orphan_agent_messages, utcnow
from app.main import app
from tests.conftest import make_human_message


def _agent(conn, session_id: int, content: str, *, truncated: int = 0,
           usage_json: str | None = None) -> int:
    """裸 SQL 插一条 agent 行（seq 接在这条会话最后），摆出各种"上次没写完"的形状。"""
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM messages WHERE session_id = ?",
        (session_id,),
    ).fetchone()["n"]
    cur = conn.execute(
        "INSERT INTO messages (session_id, seq, role, content, created_at, truncated, usage_json)"
        " VALUES (?, ?, 'agent', ?, ?, ?, ?)",
        (session_id, seq, content, utcnow(), truncated, usage_json),
    )
    return int(cur.lastrowid)


def _row(conn, message_id: int) -> dict:
    return dict(
        conn.execute(
            "SELECT content, truncated, usage_json FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
    )


def test_a_half_finished_agent_row_is_sealed_and_marked(conn, member):
    """半截行：标 truncated、补 usage_json、正文尾巴补「（回复中断）」；返回值数的是真封了几条。"""
    sid = member.session_id
    half = _agent(conn, sid, "先说一半")
    finished = _agent(conn, sid, "说完了", usage_json="{}")
    conn.commit()

    assert seal_orphan_agent_messages(conn) == 1
    conn.commit()

    assert _row(conn, half) == {
        "content": f"先说一半{SEAL_MARK}", "truncated": 1, "usage_json": "{}",
    }
    # 跑完了的行（usage_json 已写）一个字都不该动
    assert _row(conn, finished) == {"content": "说完了", "truncated": 0, "usage_json": "{}"}
    # 人话也一个字都不该动：人话永远先落库再调模型，不存在"半截的人话"
    human = _row(conn, member.message_id)
    assert human["content"] == member.content and human["truncated"] == 0


def test_a_row_the_runner_already_truncated_only_gets_the_mark(conn, member):
    """上一轮跑失败时 runner 已经标了 truncated=1：重启只补文案，不算"这次新封的"。"""
    sid = member.session_id
    failed = _agent(conn, sid, "额度用尽", truncated=1)
    empty = _agent(conn, sid, "", truncated=1)
    conn.commit()

    assert seal_orphan_agent_messages(conn) == 0
    conn.commit()

    assert _row(conn, failed)["content"] == f"额度用尽{SEAL_MARK}"
    assert _row(conn, empty)["content"] == SEAL_MARK


def test_sealing_twice_does_not_stack_the_mark(conn, member):
    """幂等：每次启动都跑一遍，标记不许补成两个，也不许再算出新封口的行。"""
    sid = member.session_id
    half = _agent(conn, sid, "先说一半")
    conn.commit()

    assert seal_orphan_agent_messages(conn) == 1
    conn.commit()
    assert seal_orphan_agent_messages(conn) == 0
    conn.commit()

    content = _row(conn, half)["content"]
    assert content == f"先说一半{SEAL_MARK}"
    assert content.count(SEAL_MARK) == 1


def test_an_empty_orphan_row_still_gets_marked(conn, member):
    """一个字都没流出来就被杀（模型还没回第一帧）：正文只有那个标记，前端据此显示"断了"。"""
    orphan = _agent(conn, member.session_id, "")
    conn.commit()

    assert seal_orphan_agent_messages(conn) == 1
    conn.commit()

    assert _row(conn, orphan) == {"content": SEAL_MARK, "truncated": 1, "usage_json": "{}"}


def test_lifespan_seals_on_startup(conn, member):
    """封口挂在 lifespan 上（与 init_db 同一路径）：进程一重启就真跑了，不靠谁记得调。

    conftest 的 `client` fixture 已经进过一次 TestClient（那次 lifespan 时库还是干净的），
    这里再进一次，验的是"插完半截行之后启动"这个顺序。
    """
    _agent(conn, member.session_id, "半截")
    make_human_message(conn, member.session_id, member.user_id, "这句人话不该被动")
    conn.commit()

    with TestClient(app):
        pass

    reader = connect()
    try:
        rows = [
            dict(r)
            for r in reader.execute(
                "SELECT role, content, truncated FROM messages WHERE session_id = ? ORDER BY seq",
                (member.session_id,),
            )
        ]
    finally:
        reader.close()

    assert [(r["role"], r["content"], r["truncated"]) for r in rows] == [
        ("human", member.content, 0),
        ("agent", f"半截{SEAL_MARK}", 1),
        ("human", "这句人话不该被动", 0),
    ]
