"""成员管理各条接口（/api/members*）与管理员后台设置（/api/admin/*）的路由层口径。

权限语义（member 403、admin 不能移除自己、改名/移除保留历史）在
tests/test_identity.py 里按 R1 逐条钉着，这里补的是路由本身：表单校验的 400、
查无此人的 404、GET /api/members 绝不外泄 password_hash，以及 2026-10-30 的两处改版：

- invites **合并注册与重置两种用途**（不再有 GET /api/members/reset-invites）；
- users 带 `can_build_wiki`，管理员的 PATCH /api/members/{id}/permissions 能改它；
- 站点设置 GET/PATCH /api/admin/settings（目前只有 chat_visibility）。

邀请的一次性语义在 tests/test_api_auth.py 的 register/reset 用例里钉；这里只验列表形状。
"""

from __future__ import annotations

from tests.conftest import fetch_one, new_invite


def test_members_requires_admin(client):
    resp = client.get("/api/members")
    assert resp.status_code == 401
    assert resp.json()["error"]


def test_members_list_hides_password_hash(client, admin, member):
    new_invite(client, "还没来的人")
    body = client.get("/api/members").json()

    assert {u["name"] for u in body["users"]} == {"管理员", "张三"}
    for user in body["users"]:
        assert "password_hash" not in user
        assert {"id", "name", "phone", "role", "deactivated_at", "can_build_wiki"} <= set(user)
    # 迁移后默认：所有存量成员都能建设 wiki（保持旧行为）。
    assert all(u["can_build_wiki"] == 1 for u in body["users"])

    invite = next(i for i in body["invites"] if i["name"] == "还没来的人")
    assert invite["purpose"] == "register"
    assert invite["used_count"] == 0
    assert {"token", "revoked_at", "first_opened_at", "purpose", "user_id"} <= set(invite)


def test_members_invites_merge_register_and_reset_newest_first(client, admin, member):
    """2026-10-30：成员页不再分两份邀请表，/api/members 一次交齐、按新到旧。"""
    register_token = new_invite(client, "还没来的人")
    older = client.post(f"/api/members/{member.user_id}/reset-login").json()["token"]
    newer = client.post(f"/api/members/{member.user_id}/reset-login").json()["token"]

    invites = client.get("/api/members").json()["invites"]
    assert [i["token"] for i in invites] == [newer, older, register_token]
    by_token = {i["token"]: i for i in invites}
    assert by_token[register_token]["purpose"] == "register"
    assert by_token[newer]["purpose"] == "reset"
    assert by_token[newer]["user_id"] == member.user_id
    assert by_token[newer]["name"] == "张三"


def test_reset_invites_standalone_route_is_gone(client, admin):
    """旧的重置邀请清单已合并进 /api/members；旧地址不再存在。"""
    assert client.get("/api/members/reset-invites").status_code == 404


def test_invite_needs_a_name(client, admin):
    resp = client.post("/api/members/invites", data={"name": "   "})
    assert resp.status_code == 400
    assert resp.json() == {"error": "填个姓名"}

    token = client.post("/api/members/invites", data={"name": "李婷"}).json()["token"]
    row = fetch_one("SELECT * FROM invites WHERE token = ?", (token,))
    assert row["purpose"] == "register" and row["used_count"] == 0


def test_revoke_is_idempotent(client, admin):
    token = new_invite(client, "李婷")
    invite_id = fetch_one("SELECT id FROM invites WHERE token = ?", (token,))["id"]
    assert client.post(f"/api/members/invites/{invite_id}/revoke").json() == {"ok": True}
    first = fetch_one("SELECT revoked_at FROM invites WHERE id = ?", (invite_id,))["revoked_at"]
    assert first
    assert client.post(f"/api/members/invites/{invite_id}/revoke").json() == {"ok": True}
    second = fetch_one("SELECT revoked_at FROM invites WHERE id = ?", (invite_id,))["revoked_at"]
    assert second == first  # 已作废的不再动时间戳

    assert client.get(f"/api/invites/{token}").status_code == 404


def test_rename_needs_a_name(client, admin, member):
    resp = client.post(f"/api/members/{member.user_id}/rename", data={"name": "  "})
    assert resp.status_code == 400
    assert resp.json() == {"error": "填个姓名"}
    assert fetch_one("SELECT name FROM users WHERE id = ?", (member.user_id,))["name"] == "张三"

    assert client.post(
        f"/api/members/{member.user_id}/rename", data={"name": "张三丰"}
    ).json() == {"ok": True}
    assert fetch_one("SELECT name FROM users WHERE id = ?", (member.user_id,))["name"] == "张三丰"


def test_remove_unknown_user_is_400(client, admin):
    resp = client.post("/api/members/9999/remove")
    assert resp.status_code == 400
    assert resp.json() == {"error": "不能停用管理员或不存在的人"}


def test_reset_login_unknown_user_is_404(client, admin):
    resp = client.post("/api/members/9999/reset-login")
    assert resp.status_code == 404
    assert resp.json() == {"error": "没有这个人"}


def test_reset_login_returns_a_reset_token_and_it_shows_in_the_merged_list(
    client, admin, member
):
    resp = client.post(f"/api/members/{member.user_id}/reset-login")
    assert resp.status_code == 200, resp.text
    token = resp.json()["token"]
    row = fetch_one("SELECT * FROM invites WHERE token = ?", (token,))
    assert row["purpose"] == "reset" and row["name"] == "张三"

    assert resp.json() == {"token": token}
    listed = next(
        i for i in client.get("/api/members").json()["invites"] if i["token"] == token
    )
    assert listed["purpose"] == "reset" and listed["used_count"] == 0


def test_reset_invite_can_be_revoked_by_id_from_the_merged_list(client, admin, member):
    """作废按 id 走、与用途无关：合并后的邀请行直接喂给 revoke 就该见效。"""
    token = client.post(f"/api/members/{member.user_id}/reset-login").json()["token"]
    row = next(i for i in client.get("/api/members").json()["invites"] if i["token"] == token)

    assert client.post(f"/api/members/invites/{row['id']}/revoke").json() == {"ok": True}

    after = next(
        i for i in client.get("/api/members").json()["invites"] if i["id"] == row["id"]
    )
    assert after["revoked_at"], "作废过的要在列表上看得出来"
    assert client.get(f"/api/invites/{token}").status_code == 404


def test_reset_login_for_deactivated_member_is_400(client, admin, member):
    """停用账号不能发重置链接：发出去也解不开，注册那一步还会撞 UNIQUE（S6b P1-4）。"""
    assert client.post(f"/api/members/{member.user_id}/remove").status_code == 200
    resp = client.post(f"/api/members/{member.user_id}/reset-login")
    assert resp.status_code == 400, resp.text
    assert "已停用" in resp.json()["error"]
    assert fetch_one(
        "SELECT COUNT(*) AS n FROM invites WHERE purpose = 'reset'"
    )["n"] == 0


# --- 2026-10-30：成员建设权限 ---------------------------------------------------


def test_permissions_patch_toggles_can_build_wiki(client, admin, member):
    assert client.patch(
        f"/api/members/{member.user_id}/permissions", json={"can_build_wiki": False}
    ).json() == {"ok": True}
    row = fetch_one("SELECT can_build_wiki FROM users WHERE id = ?", (member.user_id,))
    assert row["can_build_wiki"] == 0
    listed = next(
        u for u in client.get("/api/members").json()["users"] if u["id"] == member.user_id
    )
    assert listed["can_build_wiki"] == 0

    assert client.patch(
        f"/api/members/{member.user_id}/permissions", json={"can_build_wiki": True}
    ).json() == {"ok": True}
    assert fetch_one(
        "SELECT can_build_wiki FROM users WHERE id = ?", (member.user_id,)
    )["can_build_wiki"] == 1


def test_permissions_cannot_target_an_admin(client, admin):
    me = fetch_one("SELECT id FROM users WHERE role = 'admin'")
    resp = client.patch(
        f"/api/members/{me['id']}/permissions", json={"can_build_wiki": False}
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": "管理员始终拥有全部权限"}


def test_permissions_unknown_user_is_404(client, admin):
    resp = client.patch("/api/members/9999/permissions", json={"can_build_wiki": False})
    assert resp.status_code == 404
    assert resp.json() == {"error": "没有这个人"}


# --- 2026-10-30：站点设置（对话可见范围） ---------------------------------------


def test_admin_settings_default_to_all(client, admin):
    assert client.get("/api/admin/settings").json() == {"chat_visibility": "all"}


def test_admin_settings_can_switch_to_own_and_back(client, admin):
    resp = client.patch("/api/admin/settings", json={"chat_visibility": "own"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"chat_visibility": "own"}
    assert client.get("/api/admin/settings").json() == {"chat_visibility": "own"}

    assert client.patch(
        "/api/admin/settings", json={"chat_visibility": "all"}
    ).json() == {"chat_visibility": "all"}


def test_admin_settings_reject_unknown_value(client, admin):
    resp = client.patch("/api/admin/settings", json={"chat_visibility": "friends"})
    assert resp.status_code == 400
    assert resp.json() == {"error": "对话可见范围只能是 all 或 own"}
    # 非法值没写进库
    assert client.get("/api/admin/settings").json() == {"chat_visibility": "all"}


def test_admin_settings_are_admin_only(client, member_login):
    for call in (
        client.get("/api/admin/settings"),
        client.patch("/api/admin/settings", json={"chat_visibility": "own"}),
    ):
        assert call.status_code == 403
        assert "error" in call.json()
