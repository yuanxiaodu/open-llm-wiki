"""R1：邀请只填姓名、手机号+密码注册登录、成员管理（Task 9 改写到 /api 口径，备注 N4）。

原来四条 xfail（裁定 R8）钉的是旧渲染面的死法，这一版逐条改成对 /api 的断言：
- 登录落点 → 登录返 200 + 身份（含 role），会话随即建立；
- 成员页仅 admin → member 打 /api/members 得 403 {"error"}；
- 改名+移除保留历史 → 被移除者的名字仍以 user_name 出现在旧消息上；
- 成员页列表 → /api/members 返 users+invites，字段齐、不外泄 password_hash。
"""

from __future__ import annotations

from app.services import auth
from tests.conftest import (
    ADMIN_PASSWORD,
    ADMIN_PHONE,
    fetch_all,
    fetch_one,
    login,
    make_admin,
    make_member,
    new_invite,
    register,
)

PHONE_A = "13800000001"
PHONE_B = "13800000002"


def test_invite_only_needs_a_name(client, admin):
    token = new_invite(client, "张三")
    invite = fetch_one("SELECT * FROM invites WHERE token = ?", (token,))
    assert invite["name"] == "张三"
    assert invite["purpose"] == "register"
    assert invite["used_count"] == 0


def test_invite_info_returns_the_fixed_name(client, admin):
    """旧"注册页姓名只读"的 S1 口径：接口返 name，前端照显示、来的人改不了。"""
    token = new_invite(client, "张三")
    resp = client.get(f"/api/invites/{token}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "张三"
    assert resp.json()["used"] is False


def test_register_creates_member_with_the_invite_name(client, admin, browser):
    token = new_invite(client, "张三")
    who = browser()
    resp = register(who, token, PHONE_A)
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "张三" and resp.json()["role"] == "member"
    user = fetch_one("SELECT * FROM users WHERE phone = ?", (PHONE_A,))
    assert user["name"] == "张三"
    assert user["role"] == "member"


def test_name_cannot_be_changed_by_the_comer(client, admin, browser):
    """注册体里偷塞 name 也没用：RegisterBody 不收这个字段，名字以邀请为准。"""
    token = new_invite(client, "张三")
    resp = browser().post(
        "/api/register",
        json={"token": token, "name": "李四", "phone": PHONE_A,
              "password": "member-pass-1", "confirm": "member-pass-1"},
    )
    assert resp.status_code == 200, resp.text
    assert fetch_one("SELECT * FROM users WHERE phone = ?", (PHONE_A,))["name"] == "张三"


def test_password_is_scrypt_hashed_not_plaintext(client, admin, browser):
    token = new_invite(client, "张三")
    register(browser(), token, PHONE_A, "member-pass-1")
    stored = fetch_one("SELECT password_hash FROM users WHERE phone = ?", (PHONE_A,))["password_hash"]
    assert stored.startswith("scrypt$")
    assert "member-pass-1" not in stored
    assert auth.verify_password("member-pass-1", stored)


def test_login_returns_identity_with_role(client, admin, browser):
    """旧"登录落在自己的对话页"的 S1 口径：接口返 200 + role，会话 cookie 随即种上。"""
    token = new_invite(client, "张三")
    register(browser(), token, PHONE_A, "member-pass-1")
    who = browser()
    resp = who.post("/api/login", json={"phone": PHONE_A, "password": "member-pass-1"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["role"] == "member" and body["name"] == "张三"
    assert who.get("/api/chat/sessions").status_code == 200


def test_used_invite_cannot_register_a_second_person(client, admin, browser):
    token = new_invite(client, "张三")
    assert register(browser(), token, PHONE_A).status_code == 200
    second = browser()
    info = second.get(f"/api/invites/{token}")
    assert info.status_code == 200
    assert info.json()["used"] is True
    resp = register(second, token, PHONE_B)
    assert resp.status_code == 400
    assert "已经用过了" in resp.json()["error"]
    assert fetch_one("SELECT * FROM users WHERE phone = ?", (PHONE_B,)) is None


def test_revoked_invite_is_dead(client, admin, browser):
    token = new_invite(client, "张三")
    invite_id = fetch_one("SELECT id FROM invites WHERE token = ?", (token,))["id"]
    assert client.post(f"/api/members/invites/{invite_id}/revoke").status_code == 200
    assert browser().get(f"/api/invites/{token}").status_code == 404
    resp = register(browser(), token, PHONE_A)
    assert resp.status_code == 400
    assert "作废" in resp.json()["error"]
    assert fetch_one("SELECT * FROM users WHERE phone = ?", (PHONE_A,)) is None


def test_duplicate_phone_is_refused_in_plain_words(client, admin, browser):
    register(browser(), new_invite(client, "张三"), PHONE_A)
    resp = register(browser(), new_invite(client, "李四"), PHONE_A)
    assert resp.status_code == 400
    assert "已经被" in resp.json()["error"]
    assert len(fetch_all("SELECT * FROM users WHERE phone = ?", (PHONE_A,))) == 1


def test_password_mismatch_and_short_password(client, admin, browser):
    token = new_invite(client, "张三")
    who = browser()
    resp = who.post(
        "/api/register",
        json={"token": token, "phone": PHONE_A, "password": "member-pass-1",
              "confirm": "other-pass-1"},
    )
    assert resp.status_code == 400
    assert "不一样" in resp.json()["error"]
    resp = who.post(
        "/api/register",
        json={"token": token, "phone": PHONE_A, "password": "123", "confirm": "123"},
    )
    assert resp.status_code == 400
    assert "至少 6 位" in resp.json()["error"]
    assert fetch_one("SELECT * FROM users WHERE phone = ?", (PHONE_A,)) is None


def test_login_throttle_after_five_failures(client, admin, browser):
    register(browser(), new_invite(client, "张三"), PHONE_A, "member-pass-1")
    who = browser()
    for _ in range(auth.MAX_FAILS):
        resp = who.post("/api/login", json={"phone": PHONE_A, "password": "wrong-pass-1"})
        assert resp.status_code == 400
        assert "不对" in resp.json()["error"]
    resp = who.post("/api/login", json={"phone": PHONE_A, "password": "member-pass-1"})
    assert resp.status_code == 400
    assert "密码错太多次" in resp.json()["error"]
    # 限速期间会话没建立：要登录的接口一律 401
    assert who.get("/api/chat/sessions", follow_redirects=False).status_code == 401


def test_reset_link_changes_password_only_for_the_same_person(client, admin, browser):
    register(browser(), new_invite(client, "张三"), PHONE_A, "member-pass-1")
    person = fetch_one("SELECT * FROM users WHERE phone = ?", (PHONE_A,))
    token = client.post(f"/api/members/{person['id']}/reset-login").json()["token"]

    who = browser()
    assert who.get(f"/api/invites/{token}").status_code == 200
    resp = who.post(
        "/api/register",
        json={"token": token, "phone": PHONE_A, "password": "brand-new-pass",
              "confirm": "brand-new-pass"},
    )
    assert resp.status_code == 200, resp.text
    assert login(browser(), PHONE_A, "brand-new-pass").status_code == 200
    assert len(fetch_all("SELECT * FROM users WHERE phone = ?", (PHONE_A,))) == 1


def test_members_api_is_admin_only(client, admin, browser):
    """旧"成员页仅 admin 可见"的 S1 口径：member 打 /api/members 得 403 JSON。"""
    who, _ = make_member(browser, client, "张三", PHONE_A)
    resp = who.get("/api/members", follow_redirects=False)
    assert resp.status_code == 403
    assert resp.json()["error"]
    assert client.get("/api/members").status_code == 200


def test_member_cannot_touch_member_management(client, admin, browser):
    who, _ = make_member(browser, client, "张三", PHONE_A)
    for path in ("/api/members/invites", "/api/members/1/rename", "/api/members/1/remove",
                 "/api/members/1/reset-login", "/api/members/invites/1/revoke"):
        response = who.post(path, data={"name": "改名"})
        assert response.status_code == 403, path
        assert response.json()["error"], path
    assert who.get("/api/members").status_code == 403
    # 建设权限也是管理员的事：成员改不了自己或别人的权限（2026-10-30）。
    assert who.patch(
        "/api/members/1/permissions", json={"can_build_wiki": False}
    ).status_code == 403
    assert fetch_one("SELECT * FROM users WHERE id = 1")["role"] == "admin"


def test_rename_and_remove_keep_history(client, admin, browser):
    """旧公共房间版的 S1 口径：改名+移除后，旧消息上的 user_name 还在（引注来源姓名不丢）。"""
    who, _ = make_member(browser, client, "张三", PHONE_A)
    sid = who.post("/api/chat/sessions").json()["id"]
    assert who.post(f"/api/chat/{sid}/messages", data={"content": "我说过一句话。"}).status_code == 200
    person = fetch_one("SELECT * FROM users WHERE phone = ?", (PHONE_A,))

    assert client.post(
        f"/api/members/{person['id']}/rename", data={"name": "张三丰"}
    ).status_code == 200
    assert fetch_one("SELECT * FROM users WHERE id = ?", (person["id"],))["name"] == "张三丰"

    assert client.post(f"/api/members/{person['id']}/remove").status_code == 200
    row = fetch_one("SELECT * FROM users WHERE id = ?", (person["id"],))
    assert row["deactivated_at"] is not None

    # 消息还在，且详情里的 user_name 是改过之后的名字——历史不随账号停用消失
    detail = client.get(f"/api/chat/{sid}").json()
    mine = [m for m in detail["messages"] if m["content"] == "我说过一句话。"]
    assert len(mine) == 1
    assert mine[0]["user_name"] == "张三丰"

    # 被移除的人登录不进来
    resp = browser().post("/api/login", json={"phone": PHONE_A, "password": "member-pass-1"})
    assert resp.status_code == 400
    assert "不对" in resp.json()["error"]


def test_admin_cannot_remove_self(client, admin):
    me = fetch_one("SELECT * FROM users WHERE phone = ?", (ADMIN_PHONE,))
    resp = client.post(f"/api/members/{me['id']}/remove")
    assert resp.status_code == 400
    assert fetch_one("SELECT * FROM users WHERE id = ?", (me["id"],))["deactivated_at"] is None


def test_admin_cannot_remove_another_admin(client, admin):
    make_admin(phone="13900000002", password="admin-pass-2")
    other = fetch_one("SELECT * FROM users WHERE phone = ?", ("13900000002",))
    resp = client.post(f"/api/members/{other['id']}/remove")
    assert resp.status_code == 400
    assert fetch_one("SELECT * FROM users WHERE id = ?", (other["id"],))["deactivated_at"] is None


def test_members_api_lists_people_and_invites(client, admin, browser):
    """旧成员页渲染断言的 S1 口径：/api/members 返 users+invites，字段齐、不泄哈希。"""
    make_member(browser, client, "张三", PHONE_A)
    new_invite(client, "还没来的人")
    body = client.get("/api/members").json()

    assert {"管理员", "张三"} <= {u["name"] for u in body["users"]}
    for user in body["users"]:
        assert "password_hash" not in user
    assert ADMIN_PASSWORD not in str(body)

    by_name = {i["name"]: i for i in body["invites"]}
    assert by_name["还没来的人"]["used_count"] == 0
    assert by_name["还没来的人"]["first_opened_at"] is None
    assert by_name["还没来的人"]["revoked_at"] is None
    assert by_name["张三"]["used_count"] == 1


def test_anonymous_api_is_401_json(client, browser):
    stranger = browser()
    for path in ("/api/chat/sessions", "/api/wiki", "/api/log", "/api/members",
                 "/api/search?q=x"):
        response = stranger.get(path, follow_redirects=False)
        assert response.status_code == 401, path
        assert response.json()["error"], path
