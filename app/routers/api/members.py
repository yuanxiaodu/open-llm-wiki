"""成员管理：管理员唯一多出来的几条接口（Task 9 从旧 routers/admin.py 搬来，改 JSON）。

逻辑都在 app/services/auth.py，这一层只做收参数与翻状态码。全员 403 的口径由
RequiredAdmin 依赖把守；错误信封 {"error": 人话} 由 app/main.py 的 handler 统一出。

GET /api/members 的 users 一律剔掉 password_hash：接口是 JSON，字段给了就是发出去了，
scrypt 哈希再硬也不该离开库。users 还带 `can_build_wiki`（成员能否摄入材料、建/改/删页）。

invites **合并两种用途**（2026-10-30 委托人反馈：成员页原来成员/注册邀请/重置邀请三张表
太冗余）：返回 register + reset 全部邀请，按 id 倒序，带 purpose/user_id。注册与重置
链接都一次性；作废仍按 id 走，与用途无关。

人员权限只有一档：`can_build_wiki`。管理员恒有权限，也不能被改这一列。
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Form, HTTPException
from pydantic import BaseModel

from ...deps import Conn, RequiredAdmin
from ...services import auth

router = APIRouter()


class PermissionBody(BaseModel):
    """成员建设权限（2026-10-30）：一个开关，摄入与改 wiki 共用。"""

    can_build_wiki: bool


@router.get("/api/members")
def members(conn: sqlite3.Connection = Conn, user=RequiredAdmin):
    users = [
        {k: v for k, v in dict(r).items() if k != "password_hash"}
        for r in conn.execute("SELECT * FROM users ORDER BY id")
    ]
    invites = [
        dict(r) for r in conn.execute("SELECT * FROM invites ORDER BY id DESC")
    ]
    return {"users": users, "invites": invites}


@router.patch("/api/members/{user_id}/permissions")
def set_permissions(
    user_id: int,
    body: PermissionBody,
    conn: sqlite3.Connection = Conn,
    user=RequiredAdmin,
):
    """设置成员的「可建设 wiki」权限：开=能摄入材料 + 建/改/删页；关=只能查询阅读。

    管理员恒有权限，不允许被改成受限；这样不会出现"唯一的管理员把自己锁成只读"。
    """
    person = conn.execute(
        "SELECT id, role FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if person is None:
        raise HTTPException(404, "没有这个人")
    if person["role"] == "admin":
        raise HTTPException(400, "管理员始终拥有全部权限")
    conn.execute(
        "UPDATE users SET can_build_wiki = ? WHERE id = ?",
        (1 if body.can_build_wiki else 0, user_id),
    )
    return {"ok": True}


@router.post("/api/members/invites")
def make_invite(conn: sqlite3.Connection = Conn, user=RequiredAdmin, name: str = Form("")):
    name = name.strip()
    if not name:
        raise HTTPException(400, "填个姓名")
    return {"token": auth.create_invite(conn, name, user["id"])}


@router.post("/api/members/invites/{invite_id}/revoke")
def revoke(invite_id: int, conn: sqlite3.Connection = Conn, user=RequiredAdmin):
    auth.revoke_invite(conn, invite_id)
    return {"ok": True}


@router.post("/api/members/{user_id}/remove")
def remove(user_id: int, conn: sqlite3.Connection = Conn, user=RequiredAdmin):
    if not auth.deactivate(conn, user_id, user["id"]):
        raise HTTPException(400, "不能停用管理员或不存在的人")
    return {"ok": True}


@router.post("/api/members/{user_id}/rename")
def rename(user_id: int, conn: sqlite3.Connection = Conn, user=RequiredAdmin,
           name: str = Form("")):
    if not auth.rename_member(conn, user_id, name.strip()):
        raise HTTPException(400, "填个姓名")
    return {"ok": True}


@router.post("/api/members/{user_id}/reset-login")
def reset_login(user_id: int, conn: sqlite3.Connection = Conn, user=RequiredAdmin):
    person = conn.execute(
        "SELECT id, deactivated_at FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if person is None:
        raise HTTPException(404, "没有这个人")
    if person["deactivated_at"]:
        # 停用账号不能重置：给它发链接既解不开（登录被 deactivated 挡住），
        # 注册那一步也会撞"账号已停用"。直接在这里说清楚。
        raise HTTPException(400, "这个成员已停用，不能重置口令")
    token = auth.create_reset_invite(conn, user_id, user["id"])
    if token is None:
        raise HTTPException(404, "没有这个人")
    return {"token": token}
