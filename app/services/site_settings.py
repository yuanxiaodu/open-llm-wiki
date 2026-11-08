"""站点级设置：管理员能在前台改、运行时立刻生效的那些开关。

为什么存库而不是扩 `app/config.py`：`Settings` 是 import 时读 env 的 frozen dataclass，
改它要改 `.env` + 重启进程，而"对话可见范围"是管理员在页面上随时要切的运营开关。
数据放 `site_settings` 表，读一次库就能拿到，改动下一请求即生效。

目前只有一项：

- `chat_visibility`：`all`（所有人可见所有会话，默认、与迁移前一致）或
  `own`（成员只看自己的会话；管理员始终看全部）。

读侧宁可回退默认值也不抛：缺表（旧库还没迁移完）、行被手工删掉、值被写坏，都按
`all` 处理——把站点误锁成"每个人都只能看自己"比暂时维持旧行为更糟。
"""

from __future__ import annotations

import sqlite3

from ..db import utcnow

CHAT_VISIBILITY_ALL = "all"
CHAT_VISIBILITY_OWN = "own"
CHAT_VISIBILITIES = (CHAT_VISIBILITY_ALL, CHAT_VISIBILITY_OWN)

_KEY_CHAT_VISIBILITY = "chat_visibility"


def _read(conn: sqlite3.Connection, key: str) -> str | None:
    try:
        row = conn.execute(
            "SELECT value FROM site_settings WHERE key = ?", (key,)
        ).fetchone()
    except sqlite3.OperationalError:
        # 迁移还没跑（旧库/裸连接）时不要让所有会话接口 500；按默认值继续。
        return None
    return None if row is None else str(row["value"] or "")


def get_chat_visibility(conn: sqlite3.Connection) -> str:
    """当前对话可见范围；认不出的值回退 `all`。"""
    value = (_read(conn, _KEY_CHAT_VISIBILITY) or "").strip().lower()
    return value if value in CHAT_VISIBILITIES else CHAT_VISIBILITY_ALL


def set_chat_visibility(
    conn: sqlite3.Connection, value: str, actor_id: int | None
) -> str:
    """写对话可见范围，返回归一化后的值。调用方负责 commit。非法值抛 ValueError。"""
    clean = (value or "").strip().lower()
    if clean not in CHAT_VISIBILITIES:
        raise ValueError("对话可见范围只能是 all 或 own")
    conn.execute(
        "INSERT INTO site_settings(key, value, updated_at, updated_by)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET"
        "   value = excluded.value,"
        "   updated_at = excluded.updated_at,"
        "   updated_by = excluded.updated_by",
        (_KEY_CHAT_VISIBILITY, clean, utcnow(), actor_id),
    )
    return clean
