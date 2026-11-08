from __future__ import annotations

import sqlite3

from fastapi import Depends, HTTPException, Request

from .db import get_conn
from .services import access, auth

Conn = Depends(get_conn)


class NeedsLogin(HTTPException):
    """没登录。Task 9 之后全站只剩 /api 一个面：走 main.py 的 HTTPException
    handler 翻成 401 {"error": "请先登录"}，不再有 302 那条分支。"""

    def __init__(self):
        super().__init__(401, "请先登录")


class NeedsAdmin(HTTPException):
    """登录了但不是管理员。翻成 403 {"error": 人话}，口径同 NeedsLogin。"""

    def __init__(self):
        super().__init__(403, "只有管理员能看这一块")


class NeedsWikiBuilder(HTTPException):
    """登录了但没有「建设 wiki」权限（2026-10-30 裁定）。

    摄入材料与建/改/删 wiki 页共用这一道闸；管理员恒有权限。
    """

    def __init__(self):
        super().__init__(403, "当前账号只能使用 wiki，不能摄入材料或修改知识库")


def current_user(request: Request, conn: sqlite3.Connection = Conn):
    return auth.current_user(request, conn)


CurrentUser = Depends(current_user)


def require_user(user=CurrentUser):
    if user is None:
        raise NeedsLogin()
    return user


def require_admin(user=Depends(require_user)):
    if user["role"] != "admin":
        raise NeedsAdmin()
    return user


def require_wiki_builder(user=Depends(require_user)):
    """能摄入材料、能建/改/删 wiki 页的成员。管理员恒通过。"""
    if not access.can_build_wiki(user):
        raise NeedsWikiBuilder()
    return user


RequiredUser = Depends(require_user)
RequiredAdmin = Depends(require_admin)
RequiredWikiBuilder = Depends(require_wiki_builder)
