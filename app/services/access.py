"""可见性与成员能力的唯一判定出口。

三件事：

1. **建设权限**（`users.can_build_wiki`）：能摄入材料、建/改/删 wiki 页。管理员恒 true。
   只有使用权限的成员仍能查询、搜索、阅读；查询时智能体新建 query/comparison/summary
   沉淀页不算修改（运行时限制在 app/agent/tools.py，这里只给判据）。
2. **对话可见范围**（`site_settings.chat_visibility`）：`all` 时沿用"全员可见可发言"，
   `own` 时成员只能看自己的会话；管理员始终看全部。
3. **身份形状**：登录/`/api/me`/注册三处共用一个 payload，字段不会各写各的。

为什么单开一层而不是散在路由里：同一套判据要在 chat / search / wiki / agent 四处用，
各处自己写一份，改一处漏一处就会出现"列表挡住了、搜索没挡""详情 404、SSE 还能看"
这种半拉子隔离。
"""

from __future__ import annotations

import sqlite3

from .site_settings import (
    CHAT_VISIBILITY_OWN,
    get_chat_visibility,
)


def _field(user, key: str, default):
    """sqlite3.Row / dict 混着用：取不到键就回 default（旧测试裸造的行没有新列）。"""
    try:
        value = user[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def is_admin(user) -> bool:
    return str(_field(user, "role", "")) == "admin"


def can_build_wiki(user) -> bool:
    """能不能建设 wiki（摄入 + 建/改/删页）。管理员恒 true。"""
    if is_admin(user):
        return True
    return bool(_field(user, "can_build_wiki", 1))


def chat_visibility(conn: sqlite3.Connection) -> str:
    return get_chat_visibility(conn)


def sees_all_chats(conn: sqlite3.Connection, user) -> bool:
    """管理员始终看全部；否则看站点设置。"""
    if is_admin(user):
        return True
    return get_chat_visibility(conn) != CHAT_VISIBILITY_OWN


def can_view_session(conn: sqlite3.Connection, user, session_row) -> bool:
    """这一场会话对该用户可见吗。None（不存在）一律不可见。"""
    if session_row is None:
        return False
    if sees_all_chats(conn, user):
        return True
    return int(_field(session_row, "user_id", 0)) == int(_field(user, "id", 0))


def session_is_visible(conn: sqlite3.Connection, user, session_id: int) -> bool:
    row = conn.execute(
        "SELECT user_id FROM chat_sessions WHERE id = ?", (session_id,)
    ).fetchone()
    return can_view_session(conn, user, row)


def message_owner_filter(conn: sqlite3.Connection, user) -> int | None:
    """搜索消息命中时该按谁过滤：None = 不过滤（all/管理员），否则只看这个 user 的会话。"""
    return None if sees_all_chats(conn, user) else int(_field(user, "id", 0))


def _flag(user, key: str) -> bool:
    """身份 payload 的能力字段：管理员恒 true；老行没有这一列时回退 true（保持旧行为）。"""
    if is_admin(user):
        return True
    return bool(_field(user, key, 1))


def identity_payload(user) -> dict:
    """登录 / `/api/me` / 注册三处共用的身份形状。"""
    return {
        "id": int(_field(user, "id", 0)),
        "name": str(_field(user, "name", "")),
        "role": str(_field(user, "role", "member")),
        "can_build_wiki": _flag(user, "can_build_wiki"),
    }


