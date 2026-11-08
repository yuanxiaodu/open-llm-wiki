"""管理员后台设置：目前只有「对话可见范围」。

为什么单开一个小路由：成员/邀请是人员管理，站点设置是运营开关，两者共用
`RequiredAdmin` 但生命周期不同；混进 members.py 会让“改设置”看起来像“改成员”。
错误信封仍由 app/main.py 统一出。

前端在「管理」页里改这两项：
- `GET  /api/admin/settings` → `{"chat_visibility": "all" | "own"}`
- `PATCH /api/admin/settings {"chat_visibility": ...}` → 同上

`all`：所有人可见所有会话（迁移前口径，默认）。
`own`：成员只能看到自己的会话；管理员始终看全部。
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...deps import Conn, RequiredAdmin
from ...services import site_settings

router = APIRouter()


class SettingsBody(BaseModel):
    chat_visibility: str = ""


@router.get("/api/admin/settings")
def get_settings(conn: sqlite3.Connection = Conn, user=RequiredAdmin):
    return {"chat_visibility": site_settings.get_chat_visibility(conn)}


@router.patch("/api/admin/settings")
def update_settings(
    body: SettingsBody,
    conn: sqlite3.Connection = Conn,
    user=RequiredAdmin,
):
    try:
        value = site_settings.set_chat_visibility(conn, body.chat_visibility, user["id"])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"chat_visibility": value}
