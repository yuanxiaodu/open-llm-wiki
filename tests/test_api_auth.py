"""R3：身份接口挂到 /api 返 JSON；未登录/无权限打 /api/* 返 401/403 JSON。

R11：/api/* 的错误信封统一成 {"error": 人话}——依赖链上的 401/403 与路由里
raise 出来的 HTTPException（400/404）共用一个键名；/api/login 只收 JSON body。

R13：422（RequestValidationError）也归到同一个信封，第一条校验错误拼成人话。

Task 9 之后 HTML 面已删除，全站只剩这一套口径；probe 路由改挂在真路由不会用的
探针路径上（备注 N2：原来的 /api/chat/sessions、/api/members 已被真路由占了，
add_api_route 追加在真路由后面，永远轮不到 probe 的 endpoint）。
"""

from __future__ import annotations

import pytest

from tests.conftest import fetch_one, make_admin, new_invite

MEMBER_PHONE = "13800000001"
MEMBER_PASSWORD = "member-local-1"

# app/main.py 里那条 SPA 回退的路径（S3 Task 2 起它是路由表的最后一条）
SPA_FALLBACK_PATH = "/{full_path:path}"


def _add_probe_route(app, path: str, endpoint) -> None:
    """挂一条探针路由，并把它挪到 SPA 回退之前。

    add_api_route 只会追加到路由表末尾，而末尾那条现在是 catch-all `/{full_path:path}`：
    它对 `api/` 前缀一律 raise 404（不许把 /api 吞成 index.html），对别的路径回
    index.html——两种都轮不到追加在它后面的探针。这与本文件顶上备注 N2 是同一个坑
    （真路由在前、探针在后就探不着），只是这回挡路的是回退。挪到回退前面，
    探针探到的才是 NeedsLogin/NeedsAdmin → JSON 信封这条链本身。
    """
    routes = app.router.routes
    tail = len(routes)
    app.router.add_api_route(path, endpoint, methods=["GET"])
    added = routes[tail:]
    del routes[tail:]
    fallback_at = next(
        (i for i, route in enumerate(routes) if getattr(route, "path", "") == SPA_FALLBACK_PATH),
        len(routes),
    )
    routes[fallback_at:fallback_at] = added


@pytest.fixture
def probe_routes(client):
    """临时挂两条要登录/要管理员的 /api 探针路由，验完拆掉并断言路由表回到基线。

    路径故意选真路由永远不会用的 /api/_probe/*：与真路由（/api/chat/sessions、
    /api/members）零重叠，探的才是 NeedsLogin/NeedsAdmin → JSON 信封这条链本身。
    真路由上的同样行为由 tests/test_api_wiring.py 钉着。
    """
    from app.deps import RequiredAdmin, RequiredUser
    from app.main import app

    paths = ("/api/_probe/user", "/api/_probe/admin")

    def need_login(user=RequiredUser):
        return {"name": user["name"]}

    def need_admin(user=RequiredAdmin):
        return {"name": user["name"]}

    before = len(app.router.routes)
    _add_probe_route(app, paths[0], need_login)
    _add_probe_route(app, paths[1], need_admin)
    try:
        yield paths
    finally:
        app.router.routes = [r for r in app.router.routes if getattr(r, "path", "") not in paths]
        assert len(app.router.routes) == before
        assert not [r for r in app.router.routes if getattr(r, "path", "") in paths]


def login(client, phone: str = MEMBER_PHONE, password: str = MEMBER_PASSWORD):
    """裁定 R11：/api/login 收 JSON body。"""
    return client.post("/api/login", json={"phone": phone, "password": password})


# --- /api 一套错误口径（Task 9 之后全站只有这一个面） --------------------------


def test_anonymous_api_gets_401_json(client, probe_routes):
    sessions, _ = probe_routes
    resp = client.get(sessions, follow_redirects=False)
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert set(body) == {"error"}, body
    assert isinstance(body["error"], str) and body["error"]


def test_member_hitting_admin_api_gets_403_json(client, probe_routes, member_login):
    _, members = probe_routes
    resp = client.get(members, follow_redirects=False)
    assert resp.status_code == 403
    body = resp.json()
    assert set(body) == {"error"}, body


def test_raised_404_on_api_speaks_error_not_detail(client, admin):
    """裁定 R11：路由里 raise 的 HTTPException 与 401/403 共用一套信封。"""
    resp = client.get("/api/invites/nope")
    assert resp.status_code == 404
    body = resp.json()
    assert set(body) == {"error"}, body
    assert body["error"] == "这条链接用不了，找管理员再要一个"


def test_raised_400_on_api_speaks_error_not_detail(client, member):
    resp = login(client, password="wrong-pass-1")
    assert resp.status_code == 400
    body = resp.json()
    assert set(body) == {"error"}, body
    assert body["error"] == "手机号或密码不对"


def test_admin_hitting_admin_api_gets_200(client, probe_routes, admin):
    _, members = probe_routes
    assert client.get(members).status_code == 200


def test_unhandled_exception_on_api_is_500_chinese_envelope(client):
    """终审 I2：/api/* 上没被接住的异常 → 500 {"error": "服务器出错了，请稍后再试"}。

    裸 traceback 只进服务端日志（logger.error），不进响应体。探针路由挂上再拆掉，
    与 probe_routes 同口径（/api/_probe/*，真路由永远不会用这个前缀）。
    非 /api 的探针钉住另一半口径：保持 starlette 默认的 500 纯文本，不套 JSON 信封。
    两条都要挂在 SPA 回退之前（_add_probe_route）：非 /api 那条落在回退后面就会被
    回退接住返 index.html（200 html），500 那一半口径等于没探。
    """
    from fastapi.testclient import TestClient

    from app.main import app

    api_path, other_path = "/api/_probe/boom", "/_probe/boom"

    def boom():
        raise RuntimeError("探针：故意不处理")

    before = len(app.router.routes)
    _add_probe_route(app, api_path, boom)
    _add_probe_route(app, other_path, boom)
    try:
        # raise_server_exceptions=False：TestClient 才把 500 响应交回来断言，而不是重抛
        with TestClient(app, raise_server_exceptions=False) as raw:
            resp = raw.get(api_path)
            assert resp.status_code == 500
            body = resp.json()
            assert set(body) == {"error"}, body
            assert body["error"] == "服务器出错了，请稍后再试"

            other = raw.get(other_path)
            assert other.status_code == 500
            assert other.headers["content-type"].startswith("text/plain")
            assert other.text == "Internal Server Error"
    finally:
        app.router.routes = [
            r for r in app.router.routes
            if getattr(r, "path", "") not in (api_path, other_path)
        ]
        assert len(app.router.routes) == before


# --- 登录 / 退出 -------------------------------------------------------------


def test_api_login_returns_identity_and_session(client, probe_routes, member):
    sessions, _ = probe_routes
    assert client.get(sessions, follow_redirects=False).status_code == 401

    resp = login(client)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "id": member.user_id, "name": "张三", "role": "member", "can_build_wiki": True,
    }
    # 会话 cookie 已经种上：同一条 client 再打要登录的接口就通了
    assert client.get(sessions).status_code == 200


def test_api_login_takes_a_json_body(client, member):
    """裁定 R11：请求体是 {"phone": …, "password": …}，不是表单字段。"""
    resp = client.post(
        "/api/login", json={"phone": MEMBER_PHONE, "password": MEMBER_PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "id": member.user_id, "name": "张三", "role": "member", "can_build_wiki": True,
    }


def test_api_login_form_encoded_body_is_422(client, member):
    """契约的另一半：老的表单编码不再被接受。

    422 而不是 400——body 根本解不成 JSON，进不到 auth.try_login 的人话分支。
    """
    resp = client.post(
        "/api/login", data={"phone": MEMBER_PHONE, "password": MEMBER_PASSWORD}
    )
    assert resp.status_code == 422, resp.text
    assert resp.headers["content-type"].startswith("application/json")


def test_422_on_api_speaks_error_not_detail(client, member):
    """裁定 R13：/api/* 的 422 也是 {"error": 人话}，与 401/403/400/404 同信封。

    简报点名的形状（JSON 里漏写 password）今天拿不到 422：LoginBody 两个字段带
    默认空串（裁定 R14 签认保留，S2 若要字段级 422 再去掉默认值），漏填会一路走到
    auth.try_login 的人话 400。先把这条真行为钉在这儿——S2 去掉默认值时它会红，
    提醒连同下面一半一起改口径，不会悄悄漂。

    真会撞 RequestValidationError 的形状用同一条路由上的 password: null（漏填的显式
    写法）：422，信封里只有 error 一个键。S2 Task 5 起这句话是中文的
    （「<field> 的类型不对」，计划 Step 4b），不再是 pydantic 的英文原文。
    """
    missing = client.post("/api/login", json={"phone": MEMBER_PHONE})
    assert missing.status_code == 400, missing.text
    assert set(missing.json()) == {"error"}, missing.json()

    resp = client.post("/api/login", json={"phone": MEMBER_PHONE, "password": None})
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert set(body) == {"error"}, body  # 默认的 {"detail": [一堆原文]} 没了
    assert body["error"] == "password 的类型不对", body["error"]


# --- S2 Task 5 Step 4b：422 的三种常见形状翻成人话 --------------------------------


def test_422_missing_field_says_what_is_missing(client):
    """缺字段 →「缺了 <field>」。/api/register 四个字段都是必填，且不要登录。"""
    resp = client.post(
        "/api/register",
        json={"phone": MEMBER_PHONE, "password": "secret1", "confirm": "secret1"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json() == {"error": "缺了 token"}


def test_422_wrong_type_says_the_field_and_that_its_type_is_wrong(client):
    """类型不对 →「<field> 的类型不对」（字段名还在，人知道是哪个框）。"""
    resp = client.post(
        "/api/register",
        json={"token": 12345, "phone": MEMBER_PHONE, "password": "s", "confirm": "s"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json() == {"error": "token 的类型不对"}


def test_422_unparseable_body_says_it_is_not_json(client):
    """请求体解不出来 →「请求体不是合法的 JSON」，别再回一句 pydantic 的英文。

    两种形状都要盖住：字节不是合法 JSON（json_invalid），以及老客户端拿表单编码打
    JSON 接口（model_attributes_type，S1 裁定 R11 之后 /api 只收 JSON）。
    """
    resp = client.post(
        "/api/register",
        content="{这不是 json".encode(),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json() == {"error": "请求体不是合法的 JSON"}

    form = client.post("/api/login", data={"phone": MEMBER_PHONE, "password": "x"})
    assert form.status_code == 422, form.text
    assert form.json() == {"error": "请求体不是合法的 JSON"}


def test_api_login_wrong_password_is_400_in_plain_words(client, member):
    resp = client.post(
        "/api/login", json={"phone": MEMBER_PHONE, "password": "wrong-pass-1"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "手机号或密码不对"


def test_api_login_bad_phone_shape(client, member):
    resp = client.post("/api/login", json={"phone": "12", "password": MEMBER_PASSWORD})
    assert resp.status_code == 400
    assert resp.json()["error"] == "手机号看起来不对，检查一下"


def test_api_login_throttles_after_five_failures(client, member):
    from app.services import auth

    for _ in range(auth.MAX_FAILS):
        assert client.post(
            "/api/login", json={"phone": MEMBER_PHONE, "password": "wrong-pass-1"}
        ).status_code == 400
    resp = login(client)
    assert resp.status_code == 400
    assert "密码错太多次" in resp.json()["error"]


def test_me_restores_identity_from_cookie(client, member_login):
    body = client.get("/api/me").json()
    assert body["id"] == member_login.user_id
    assert body["name"] == member_login.name
    assert body["role"] == "member"


def test_me_requires_login(client):
    client.cookies.clear()
    resp = client.get("/api/me")
    assert resp.status_code == 401


def test_api_logout_drops_the_session(client, probe_routes, member_login):
    sessions, _ = probe_routes
    assert client.get(sessions).status_code == 200
    assert client.post("/api/logout").json() == {"ok": True}
    assert client.get(sessions, follow_redirects=False).status_code == 401


def test_healthz(client):
    body = client.get("/healthz").json()
    assert body["ok"] is True


# --- 邀请与注册 --------------------------------------------------------------


def test_invite_info_returns_name_and_purpose(client, admin):
    token = new_invite(client, "李婷")
    resp = client.get(f"/api/invites/{token}")
    assert resp.status_code == 200
    assert resp.json() == {"name": "李婷", "purpose": "register", "used": False}
    assert fetch_one("SELECT first_opened_at FROM invites WHERE token = ?", (token,))[
        "first_opened_at"
    ]


def test_invite_info_unknown_or_revoked_is_404(client, admin):
    assert client.get("/api/invites/nope").status_code == 404

    token = new_invite(client, "李婷")
    invite_id = fetch_one("SELECT id FROM invites WHERE token = ?", (token,))["id"]
    client.post(f"/api/members/invites/{invite_id}/revoke")
    assert client.get(f"/api/invites/{token}").status_code == 404


def test_first_opened_at_is_written_once(client, conn, admin):
    """裁定 R12：first_opened_at 只有 auth.mark_invite_opened 一个家，只写一次。

    先把这一列钉成一个哨兵值，再从 /api 面打开两次：不许把它改成"现在"。
    哨兵还在，说明"只写一次"的语义从调用方的 if 挪进 SQL 的 WHERE 之后没有变。
    """
    token = new_invite(client, "李婷")
    sentinel = "2000-01-01T00:00:00+00:00"
    conn.execute(
        "UPDATE invites SET first_opened_at = ? WHERE token = ?", (sentinel, token)
    )

    def opened_at():
        return fetch_one("SELECT first_opened_at FROM invites WHERE token = ?", (token,))[
            "first_opened_at"
        ]

    assert client.get(f"/api/invites/{token}").status_code == 200
    assert opened_at() == sentinel
    assert client.get(f"/api/invites/{token}").status_code == 200
    assert opened_at() == sentinel


def test_register_creates_member_and_logs_in(client, admin):
    token = new_invite(client, "李婷")
    resp = client.post(
        "/api/register",
        json={"token": token, "phone": "13700000002", "password": "secret-1",
              "confirm": "secret-1"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "李婷" and body["role"] == "member"

    row = fetch_one("SELECT * FROM users WHERE phone = ?", ("13700000002",))
    assert row["name"] == "李婷" and row["role"] == "member"
    assert row["password_hash"].startswith("scrypt$")
    assert fetch_one("SELECT used_count FROM invites WHERE token = ?", (token,))[
        "used_count"
    ] == 1


def test_invite_is_one_shot(client, admin):
    token = new_invite(client, "李婷")
    assert client.get(f"/api/invites/{token}").json()["used"] is False
    first = client.post(
        "/api/register",
        json={"token": token, "phone": "13700000002", "password": "secret-1",
              "confirm": "secret-1"},
    )
    assert first.status_code == 200, first.text
    assert client.get(f"/api/invites/{token}").json()["used"] is True

    second = client.post(
        "/api/register",
        json={"token": token, "phone": "13700000003", "password": "secret-2",
              "confirm": "secret-2"},
    )
    assert second.status_code == 400
    assert "已经用过了" in second.json()["error"]
    assert fetch_one("SELECT * FROM users WHERE phone = ?", ("13700000003",)) is None


def test_reset_link_is_one_shot_too(client, admin, member):
    """重置链接也一次性：换过一次密码之后，同一条 token 再来一次必须 400。"""
    token = client.post(f"/api/members/{member.user_id}/reset-login").json()["token"]

    first = client.post("/api/register", json={
        "token": token, "phone": MEMBER_PHONE,
        "password": "brand-new-pass", "confirm": "brand-new-pass",
    })
    assert first.status_code == 200, first.text
    assert first.json()["id"] == member.user_id
    assert fetch_one("SELECT used_count FROM invites WHERE token = ?", (token,))[
        "used_count"
    ] == 1

    second = client.post("/api/register", json={
        "token": token, "phone": MEMBER_PHONE,
        "password": "another-pass", "confirm": "another-pass",
    })
    assert second.status_code == 400, second.text
    assert "已经用过了" in second.json()["error"]
    # 第二次没改密码：第一次的新密码还能登录
    assert login(client, password="brand-new-pass").status_code == 200
    assert fetch_one("SELECT used_count FROM invites WHERE token = ?", (token,))[
        "used_count"
    ] == 1



def test_register_rejects_bad_input_in_plain_words(client, admin):
    token = new_invite(client, "李婷")

    def post(**over):
        body = {"token": token, "phone": "13700000002", "password": "secret-1",
                "confirm": "secret-1"}
        body.update(over)
        return client.post("/api/register", json=body)

    assert post(phone="12").json()["error"] == "手机号看起来不对，检查一下"
    assert post(confirm="别的").json()["error"] == "两次输入的密码不一样"
    assert post(password="123", confirm="123").json()["error"] == "密码至少 6 位"
    assert post(token="nope").status_code == 400
    assert fetch_one("SELECT * FROM users WHERE phone = ?", ("13700000002",)) is None


def test_register_refuses_a_phone_someone_else_owns(client, admin, member):
    token = new_invite(client, "李婷")
    resp = client.post(
        "/api/register",
        json={"token": token, "phone": MEMBER_PHONE, "password": "secret-1",
              "confirm": "secret-1"},
    )
    assert resp.status_code == 400
    assert "已经被" in resp.json()["error"]
    assert fetch_one("SELECT used_count FROM invites WHERE token = ?", (token,))[
        "used_count"
    ] == 0


def test_reset_link_only_changes_the_same_persons_password(client, admin, member):
    token = client.post(f"/api/members/{member.user_id}/reset-login").json()["token"]

    resp = client.post(
        "/api/register",
        json={"token": token, "phone": MEMBER_PHONE, "password": "brand-new-pass",
              "confirm": "brand-new-pass"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == member.user_id

    assert login(client, password="brand-new-pass").status_code == 200
    old = client.post(
        "/api/login", json={"phone": MEMBER_PHONE, "password": MEMBER_PASSWORD}
    )
    assert old.status_code == 400
    assert old.json()["error"] == "手机号或密码不对"


def test_register_keeps_admin_identity_on_reset(client, admin):
    make_admin(phone="13900000002", password="admin-pass-2")
    person = fetch_one("SELECT * FROM users WHERE phone = ?", ("13900000002",))
    token = client.post(f"/api/members/{person['id']}/reset-login").json()["token"]
    resp = client.post(
        "/api/register",
        json={"token": token, "phone": "13900000002", "password": "brand-new-pass",
              "confirm": "brand-new-pass"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "admin"


# --- S6b 修复：停用账号 / 改名 / 同名 / 邀请原子占用 ---------------------------


def test_reset_link_for_deactivated_account_is_400_not_500(client, admin, member):
    """停用账号仍占着 users.phone 的 UNIQUE：拿它的 reset 链接去注册必须 400 人话。"""
    token = client.post(f"/api/members/{member.user_id}/reset-login").json()["token"]
    assert client.post(f"/api/members/{member.user_id}/remove").status_code == 200
    row = fetch_one("SELECT deactivated_at FROM users WHERE id = ?", (member.user_id,))
    assert row["deactivated_at"]

    resp = client.post("/api/register", json={
        "token": token, "phone": MEMBER_PHONE,
        "password": "brand-new-pass", "confirm": "brand-new-pass",
    })
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "这个手机号对应的账号已停用，找管理员恢复"
    # 请求事务回滚：链接没被消耗，密码也没被改
    assert fetch_one("SELECT used_count FROM invites WHERE token = ?", (token,))[
        "used_count"
    ] == 0


def test_reset_link_survives_a_rename(client, admin, member):
    """reset 链接按 user_id 绑人（迁移 0008）：改名之后旧链接照样能用。"""
    token = client.post(f"/api/members/{member.user_id}/reset-login").json()["token"]
    client.post(f"/api/members/{member.user_id}/rename", data={"name": "张三丰"})

    resp = client.post("/api/register", json={
        "token": token, "phone": MEMBER_PHONE,
        "password": "brand-new-pass", "confirm": "brand-new-pass",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == member.user_id and body["name"] == "张三丰"
    assert login(client, password="brand-new-pass").status_code == 200


def test_reset_link_does_not_cross_namesakes(client, admin, conn, member):
    """同名不同人不能串：reset 链接绑的是 user_id，不是姓名。"""
    from app.services import auth as auth_service

    other_id = auth_service.create_member_direct(conn, "张三", "13700000009", "other-pass-1")
    token = client.post(f"/api/members/{member.user_id}/reset-login").json()["token"]

    resp = client.post("/api/register", json={
        "token": token, "phone": "13700000009",
        "password": "hijacked-pass", "confirm": "hijacked-pass",
    })
    assert resp.status_code == 400, resp.text
    assert "已经被" in resp.json()["error"]
    # 同名的那个人密码没被动
    assert client.post(
        "/api/login", json={"phone": "13700000009", "password": "other-pass-1"}
    ).status_code == 200
    assert other_id != member.user_id


def test_reset_link_with_an_unknown_phone_does_not_create_a_member(client, admin, member):
    """重设链接不是注册链接：手机号在库里查无此人时 400，绝不 INSERT 新账号。

    2026-10-23 线上长出的第二本账号就是这么来的：管理员发的是 reset 链接（账号是预建的、
    手机号是占位号），本人在"这个账号的手机号"一栏填了自己真实的号。旧代码
    `clash = SELECT ... WHERE phone = ?` 查不到这条号，就一路滑到下面的 INSERT，
    把 invite.name 当新成员名建了出来——陈旭豪、王欣阳、张语纯各多一份。

    这里同时钉三件事：400 人话、库里没添人、链接没被消耗（本人还能拿对的号重来）。
    """
    token = client.post(f"/api/members/{member.user_id}/reset-login").json()["token"]

    resp = client.post("/api/register", json={
        "token": token, "phone": "13700000008",
        "password": "brand-new-pass", "confirm": "brand-new-pass",
    })
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == (
        "手机号对不上：这条链接是给「张三」的账号换密码的，要填这个账号原来登录用的手机号。"
        "要是这个号已经不用了，找管理员重新发一条邀请"
    )
    assert fetch_one("SELECT * FROM users WHERE phone = ?", ("13700000008",)) is None
    assert fetch_one("SELECT used_count FROM invites WHERE token = ?", (token,))[
        "used_count"
    ] == 0
    # 旧密码没被动：本人拿对的手机号再试一次仍然走得通
    assert login(client, password=MEMBER_PASSWORD).status_code == 200
    retry = client.post("/api/register", json={
        "token": token, "phone": MEMBER_PHONE,
        "password": "brand-new-pass", "confirm": "brand-new-pass",
    })
    assert retry.status_code == 200, retry.text
    assert retry.json()["id"] == member.user_id


def test_legacy_reset_link_with_unknown_phone_also_refuses_to_create(client, admin, conn, member):
    """老 reset 行（user_id 为 NULL）走姓名回落时同样不许建号：查无此号就 400。"""
    token = client.post(f"/api/members/{member.user_id}/reset-login").json()["token"]
    conn.execute("UPDATE invites SET user_id = NULL WHERE token = ?", (token,))

    resp = client.post("/api/register", json={
        "token": token, "phone": "13700000008",
        "password": "brand-new-pass", "confirm": "brand-new-pass",
    })
    assert resp.status_code == 400, resp.text
    assert fetch_one("SELECT * FROM users WHERE phone = ?", ("13700000008",)) is None
    assert fetch_one("SELECT used_count FROM invites WHERE token = ?", (token,))[
        "used_count"
    ] == 0


def test_legacy_reset_link_is_ambiguous_if_two_people_share_the_name(client, admin, conn, member):
    """老 reset 行只剩姓名可依：同名两个人时无法判定是谁，宁可拒绝也不猜（改错人更糟）。"""
    from app.services import auth as auth_service

    auth_service.create_member_direct(conn, "张三", "13700000009", "other-pass-1")
    token = client.post(f"/api/members/{member.user_id}/reset-login").json()["token"]
    conn.execute("UPDATE invites SET user_id = NULL WHERE token = ?", (token,))

    resp = client.post("/api/register", json={
        "token": token, "phone": MEMBER_PHONE,
        "password": "brand-new-pass", "confirm": "brand-new-pass",
    })
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "这条链接对应的账号已经不在了，找管理员重新发一条"
    # 两个人的密码都没被改
    assert login(client, password=MEMBER_PASSWORD).status_code == 200
    assert client.post(
        "/api/login", json={"phone": "13700000009", "password": "other-pass-1"}
    ).status_code == 200


def test_legacy_reset_invite_without_user_id_falls_back_to_name(client, admin, conn, member):
    """迁移前的老 reset 行 user_id 为 NULL：退回姓名匹配，旧链接仍然可用。"""
    token = client.post(f"/api/members/{member.user_id}/reset-login").json()["token"]
    conn.execute("UPDATE invites SET user_id = NULL WHERE token = ?", (token,))

    resp = client.post("/api/register", json={
        "token": token, "phone": MEMBER_PHONE,
        "password": "brand-new-pass", "confirm": "brand-new-pass",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == member.user_id


def test_invite_claim_is_single_use_with_atomic_update(client, conn, admin):
    """服务层：第一次抢到，第二次拒绝；原子 UPDATE 对已用链接 rowcount==0。"""
    from app.services import auth as auth_service

    token = new_invite(client, "李婷")
    ok, payload = auth_service.register_with_invite(
        conn, token, "13700000002", "secret-1", "secret-1"
    )
    assert ok, payload

    again_ok, message = auth_service.register_with_invite(
        conn, token, "13700000003", "secret-2", "secret-2"
    )
    assert again_ok is False and "已经用过了" in message
    assert fetch_one("SELECT used_count FROM invites WHERE token = ?", (token,))[
        "used_count"
    ] == 1
    assert fetch_one("SELECT * FROM users WHERE phone = ?", ("13700000003",)) is None

    cur = conn.execute(
        "UPDATE invites SET used_count = used_count + 1, first_used_at = ?"
        " WHERE token = ? AND used_count = 0",
        ("2026-01-01T00:00:00+00:00", token),
    )
    assert cur.rowcount == 0


def test_concurrent_duplicate_phone_insert_is_400_not_500(client, admin, monkeypatch):
    """同手机号并发注册撞 users.phone UNIQUE：翻 400 人话，邀请名额随事务回滚。"""
    import sqlite3

    from app.services import auth as auth_service

    token = new_invite(client, "李婷")

    def boom(password: str) -> str:
        raise sqlite3.IntegrityError("UNIQUE constraint failed: users.phone")

    monkeypatch.setattr(auth_service, "hash_password", boom)
    resp = client.post("/api/register", json={
        "token": token, "phone": "13700000002",
        "password": "secret-1", "confirm": "secret-1",
    })
    assert resp.status_code == 400, resp.text
    assert "已经被" in resp.json()["error"]
    assert fetch_one("SELECT used_count FROM invites WHERE token = ?", (token,))[
        "used_count"
    ] == 0
