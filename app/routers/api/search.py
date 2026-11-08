"""全站搜索：一组接口把三样语料（会话人话 / 上传材料 / wiki 生效页）一起搜。

返回三组分开的命中，前端各渲染各的；每条只有 ref_id、heading、snippet，
点开去哪由前端按组决定（sessions 组多带一个 session_id，见下）。登录即可搜——
知识库语料本来就共享（spec §5）；对话可见范围是 `own` 时，会话那一组只出现自己的
消息（2026-10-30），见 access.message_owner_filter。
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter

from ...deps import Conn, RequiredUser
from ...services import access, search

router = APIRouter()


@router.get("/api/search")
def search_all(q: str, conn: sqlite3.Connection = Conn, user=RequiredUser):
    # 消息命中按对话可见范围过滤（2026-10-30）：own 模式下只搜得到自己的会话；
    # all 模式与管理员是 None（不过滤）。上传材料与 wiki 页不受影响。
    hits = search.search(
        conn, q, limit=30, message_owner_id=access.message_owner_filter(conn, user)
    )
    groups = {"sessions": [], "uploads": [], "pages": []}
    target = {"message": "sessions", "upload": "uploads", "page": "pages"}
    for hit in hits:
        # 裁定 R64：不认识的 kind 跳过，不 KeyError——库里将来多出一类语料时，
        # 搜索接口该是"少一组结果"，不是整条 500。
        group = target.get(hit["kind"])
        if group is None:
            continue
        groups[group].append(
            {"ref_id": hit["ref_id"], "heading": hit["heading"], "snippet": hit["body"]}
        )
    _attach_session_ids(conn, groups["sessions"])
    return groups


def _attach_session_ids(conn: sqlite3.Connection, items: list[dict]) -> None:
    """给 sessions 组每条补上"这条消息待在哪场会话里"（S4 T1，搜索页要跳 /chat/{id}）。

    ref_id 是**消息** id（S1 终审 I8：索引用消息建行），拿它当会话 id 去跳就是 404，
    所以另给一列 session_id；ref_id 原样留着（引注、定位到那一条还要用）。

    一次 IN 批量查完这一组，不是每条命中查一次——30 条命中就是 30 趟 SQLite。
    空组直接返回，一条 SQL 都不发。

    消息在库里已经不在了（恢复出来的库、索引还没重建）就给 None：前端据此不渲染
    链接，比给一个必然 404 的会话 id 诚实。
    """
    ids = []
    for item in items:
        try:
            ids.append(int(item["ref_id"]))
        except (TypeError, ValueError):
            # 索引里混进一个不像 id 的 ref_id：这条查不出归属，别把整组搜索拖成 500。
            continue
    if not ids:
        return
    rows = conn.execute(
        f"SELECT id, session_id FROM messages WHERE id IN ({','.join('?' * len(ids))})",
        ids,
    ).fetchall()
    owner = {str(row["id"]): row["session_id"] for row in rows}
    for item in items:
        item["session_id"] = owner.get(str(item["ref_id"]))
