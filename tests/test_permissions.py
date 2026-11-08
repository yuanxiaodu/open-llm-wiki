"""成员「可建设 wiki」权限与站点可见范围的 HTTP 层口径（2026-10-30）。

权限只有一档：`can_build_wiki=0` 的成员只能用 wiki——能查询/搜索/阅读，但不能摄入
材料、不能建/改/删页。管理员恒有权限。

这里钉的是 HTTP 闸与身份下发；智能体工具层的限制在 test_agent_tools.py，可见范围在
test_sessions.py / test_search.py / test_api_wiki.py。
"""

from __future__ import annotations

from app.config import settings
from tests.conftest import fetch_one


def _set_builder(conn, user_id: int, allowed: bool) -> None:
    conn.execute(
        "UPDATE users SET can_build_wiki = ? WHERE id = ?",
        (1 if allowed else 0, user_id),
    )


def test_restricted_member_cannot_stage_uploads(client, conn, member_login):
    _set_builder(conn, member_login.user_id, False)
    resp = client.post(
        "/api/uploads", files={"file": ("a.txt", b"hello", "text/plain")}
    )
    assert resp.status_code == 403, resp.text
    assert resp.json() == {"error": "当前账号只能使用 wiki，不能摄入材料或修改知识库"}
    # 材料列表仍然能看（wiki 语料共享）
    assert client.get("/api/uploads").status_code == 200


def test_restricted_member_cannot_ingest_web_or_document(client, conn, member_login):
    _set_builder(conn, member_login.user_id, False)
    for method, path, body in (
        ("post", "/api/ingest/url", {"url": "https://example.com/x"}),
        ("post", "/api/ingest/search", {"query": "测试"}),
        ("post", "/api/ingest/document", {"upload_id": 1}),
    ):
        resp = getattr(client, method)(path, json=body)
        assert resp.status_code == 403, (path, resp.status_code, resp.text)
        assert "不能摄入" in resp.json()["error"], path


def test_restricted_member_cannot_send_staged_uploads(client, conn, member_login):
    """权限被撤回后，之前暂存的 upload_id 也不能再随消息发出去。"""
    _set_builder(conn, member_login.user_id, False)
    resp = client.post(
        f"/api/chat/{member_login.session_id}/messages",
        data={"content": "看这个", "upload_ids": [9999]},
    )
    assert resp.status_code == 403, resp.text
    assert "不能摄入" in resp.json()["error"]
    # 旧上传路由同样被挡
    legacy = client.post(
        f"/api/chat/{member_login.session_id}/upload",
        files={"file": ("a.txt", b"hello", "text/plain")},
    )
    assert legacy.status_code == 403, legacy.text


def test_restricted_member_cannot_write_wiki_through_http(client, conn, member_login):
    """WIKI_READONLY=0 时（测试环境），人改写路由也要看成员建设权限。"""
    assert settings.wiki_readonly is False
    _set_builder(conn, member_login.user_id, False)
    resp = client.post("/api/wiki", json={
        "title": "权限测试页", "page_type": "concept", "body_md": "正文",
        "citations": [], "tags": [], "sources": [],
    })
    assert resp.status_code == 403, resp.text
    assert resp.json() == {"error": "当前账号只能使用 wiki，不能修改知识库"}
    assert fetch_one("SELECT COUNT(*) AS n FROM users WHERE can_build_wiki = 0")["n"] == 1


def test_admin_always_has_build_permission(client, admin, conn):
    admin_id = fetch_one("SELECT id FROM users WHERE role = 'admin'")["id"]
    _set_builder(conn, admin_id, False)
    resp = client.post(
        "/api/uploads", files={"file": ("a.txt", b"hello", "text/plain")}
    )
    assert resp.status_code == 200, resp.text


def test_identity_reports_can_build_wiki_after_change(client, admin, conn, member,
                                                      browser):
    from tests.conftest import api_login

    _set_builder(conn, member.user_id, False)
    who = browser()
    api_login(who, "13800000001", "member-local-1")
    me = who.get("/api/me").json()
    assert me["id"] == member.user_id
    assert me["can_build_wiki"] is False

    login = who.post(
        "/api/login", json={"phone": "13800000001", "password": "member-local-1"}
    ).json()
    assert login["can_build_wiki"] is False


def test_admin_can_restore_build_permission(client, admin, member):
    assert client.patch(
        f"/api/members/{member.user_id}/permissions", json={"can_build_wiki": False}
    ).status_code == 200
    assert client.patch(
        f"/api/members/{member.user_id}/permissions", json={"can_build_wiki": True}
    ).status_code == 200
    assert fetch_one(
        "SELECT can_build_wiki FROM users WHERE id = ?", (member.user_id,)
    )["can_build_wiki"] == 1
