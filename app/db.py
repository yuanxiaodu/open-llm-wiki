from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from .config import settings

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    # check_same_thread=False：一条连接要能跨线程用。
    # FastAPI 的 get_conn 依赖（同步生成器）在线程池的工作线程里建连接，而
    # store.write_page 是 async 的，把 git 子进程的活交给 asyncio.to_thread —— 那是
    # **另一个**工作线程。默认的同线程检查会让每次写页都 ProgrammingError。
    # 安全前提：连接仍是"一次请求一条"，且调用方在 to_thread 期间是 await 着的，
    # 同一时刻只有一个线程碰它；底层 SQLite 本身是 serialized 模式
    # （sqlite3.threadsafety == 3），store 还按 slug 上锁、repo 对索引上锁。
    conn = sqlite3.connect(db_path or settings.db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(db_path: Path | None = None) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    settings.extracted_text_dir.mkdir(parents=True, exist_ok=True)
    settings.backups_dir.mkdir(parents=True, exist_ok=True)

    conn = connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS _migrations ("
            "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {row["name"] for row in conn.execute("SELECT name FROM _migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            conn.executescript(path.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO _migrations(name, applied_at) VALUES (?, ?)",
                (path.name, utcnow()),
            )
        conn.commit()
    finally:
        conn.close()


SEAL_MARK = "（回复中断）"


def seal_orphan_agent_messages(conn: sqlite3.Connection) -> int:
    """启动时把"没写完"的 agent 行封口，返回这次新封了几条。

    进程在生成中途被杀会留下 usage_json 为空、truncated=0 的 agent 行；不封口的话
    SSE 续读会永远等一个不会再来的结尾（Task 5 的收尾判据就是这两列）。启动时没有
    活着的生成任务，所以全量封口是安全的。

    两句写、不用一个带 CASE 的大 UPDATE，是为了把两件事分开：

    1. 第一句只碰"没封口的半截行"（usage_json IS NULL AND truncated=0）：标
       truncated=1、usage_json 补 `'{}'`（与 S1 同口径，前端读到的是"没有用量数据"
       而不是 NULL）。返回的 rowcount 就是这一次真封了几条。
    2. 第二句给所有标了 truncated=1 的 agent 行补尾巴「（回复中断）」，已经带这个尾巴的
       跳过——所以它幂等，重复启动不会把标记补成两个，也顺带把 S1 那些"标了 1 但没补
       文案"的旧行补上（spec §8 要的是正文里看得出"回复中断"，不只是一个标志位）。

    只认 role='agent'：人话永远先落库再调模型（append_human），不存在"半截的人话"。
    提交归调用方（main.py 的 lifespan 里 commit）。
    """
    cur = conn.execute(
        "UPDATE messages SET truncated = 1, usage_json = '{}'"
        " WHERE role = 'agent' AND usage_json IS NULL AND truncated = 0"
    )
    sealed = cur.rowcount
    conn.execute(
        "UPDATE messages SET content = content || ?"
        " WHERE role = 'agent' AND truncated = 1"
        " AND content NOT LIKE '%' || ?",
        (SEAL_MARK, SEAL_MARK),
    )
    return sealed


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
