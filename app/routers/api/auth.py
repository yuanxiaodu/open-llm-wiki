"""身份接口：登录、退出、邀请查询、注册、健康检查。

逻辑都在 app/services/auth.py 里，这一层只做三件事：收参数、写 session、把
(过不过, 人话) 翻成状态码。Task 9 之后全站只有这一套身份接口（同源的 HTML 面
app/routers/public.py 已随旧链路删除）。

契约（裁定 R11）：请求体一律 JSON，不再收表单字段；错误一律 {"error": 人话}，
键名由 app/main.py 的 handler 统一，路由里只管 raise HTTPException。
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ...config import settings
from ...deps import Conn, RequiredUser
from ...services import access, auth

router = APIRouter()


class LoginBody(BaseModel):
    """两个字段都给默认空串：漏填时走 auth.try_login 的人话 400，而不是 422。"""

    phone: str = ""
    password: str = ""


class RegisterBody(BaseModel):
    token: str
    phone: str
    password: str
    confirm: str


@router.post("/api/login")
def login(body: LoginBody, request: Request, conn: sqlite3.Connection = Conn):
    ok, payload = auth.try_login(conn, body.phone, body.password)
    if not ok:
        raise HTTPException(status_code=400, detail=payload)
    auth.login(request, payload["id"])
    return {
        "id": payload["id"],
        "name": payload["name"],
        "role": payload["role"],
        "can_build_wiki": payload["can_build_wiki"],
    }


@router.get("/api/me")
def me(user=RequiredUser):
    """用服务端 session cookie 恢复当前身份。

    前端把身份放在 sessionStorage（每个标签页一份）；新标签页打开 /wiki 时
    sessionStorage 是空的，但 cookie 还在——这里让新标签页把 cookie 换成
    {id,name,role,can_build_wiki}，而不是误判成未登录。前端每次整页加载也会来刷一次，
    管理员改了权限之后不用重新登录。
    """
    return access.identity_payload(user)


@router.post("/api/logout")
def logout(request: Request):
    auth.logout(request)
    return {"ok": True}


@router.get("/api/invites/{token}")
def invite_info(token: str, conn: sqlite3.Connection = Conn):
    invite = auth.resolve_invite(conn, token)
    if invite is None:
        raise HTTPException(status_code=404, detail="这条链接用不了，找管理员再要一个")
    auth.mark_invite_opened(conn, invite["id"])
    return {
        "name": invite["name"],
        "purpose": invite["purpose"],
        "used": invite["used_count"] >= 1,
    }


@router.post("/api/register")
def register(body: RegisterBody, request: Request, conn: sqlite3.Connection = Conn):
    ok, payload = auth.register_with_invite(
        conn, body.token, body.phone, body.password, body.confirm
    )
    if not ok:
        raise HTTPException(status_code=400, detail=payload)
    auth.login(request, payload["id"])
    return {
        "id": payload["id"],
        "name": payload["name"],
        "role": payload["role"],
        "can_build_wiki": payload["can_build_wiki"],
    }


@router.get("/healthz")
def healthz():
    return {"ok": True, "base_url": settings.public_base_url}
