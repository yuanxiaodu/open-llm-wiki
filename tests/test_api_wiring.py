"""Task 9 装配：全站只剩 /api 一套路由，旧渲染面全部消失（简报 Step 1 逐字 + 装配补充）。

S3 Task 2 起还多了一条：非 /api 路径回退到 web/dist/index.html（SPA），
dist 不在时保留原来的 JSON 提示与 404。
"""

from __future__ import annotations

import pytest

INDEX_HTML = (
    '<!doctype html><html lang="zh-CN"><head><title>众筹共写</title></head>'
    '<body><div id="root"></div>'
    '<script type="module" src="/assets/index-TESTHASH.js"></script></body></html>'
)


@pytest.fixture
def tmp_dist(tmp_path, monkeypatch):
    """把 app.main.WEB_DIST 指到一个临时的构建产物目录（index.html + assets/）。

    为什么用 monkeypatch 而不是真去 `pnpm build`：web/dist 不进 git（.gitignore），
    服务器与别人的检出里可能压根没有；托管这条链路在两种状态下都得能测，
    所以两种状态各自钉一条 fixture，不让"本机碰巧构建过"决定用例结果。
    """
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "assets" / "index-TESTHASH.js").write_text("console.log(1)", encoding="utf-8")
    (dist / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    monkeypatch.setattr("app.main.WEB_DIST", dist)
    return dist


@pytest.fixture
def no_dist(tmp_path, monkeypatch):
    """dist 不存在的状态：服务器上还没构建，或本机 web/ 连依赖都没装。"""
    monkeypatch.setattr("app.main.WEB_DIST", tmp_path / "dist-missing")


def test_api_unauthenticated_is_401_json(client):
    resp = client.get("/api/chat/sessions", follow_redirects=False)
    assert resp.status_code == 401
    assert resp.json()["error"]


def test_api_forbidden_is_403_json(client, member_login):
    resp = client.get("/api/members", follow_redirects=False)
    assert resp.status_code == 403
    assert resp.json()["error"]


def test_invite_register_flow_still_works(client, conn, admin):
    token = client.post("/api/members/invites", data={"name": "李婷"}).json()["token"]
    info = client.get(f"/api/invites/{token}").json()
    assert info["name"] == "李婷"
    resp = client.post(
        "/api/register",
        json={"token": token, "phone": "13700000001", "password": "secret1", "confirm": "secret1"},
    )
    assert resp.status_code == 200, resp.text
    assert client.get("/api/chat/sessions").status_code == 200


def test_old_render_routes_gone(client, no_dist):
    """旧的服务端渲染路由一条都不剩（dist 不在时它们就是 404/405）。

    带 no_dist 是因为 dist 在的时候这些路径会被 SPA 回退接住返 index.html——
    那正是回退该有的样子，由下面 test_deep_link_falls_back_to_index_html 钉。
    """
    for path in ("/wiki", "/log", "/pending", "/chat/mine", "/members", "/login", "/outline"):
        assert client.get(path, follow_redirects=False).status_code in (404, 405), path


def test_static_is_no_longer_mounted_and_falls_back_to_the_spa(client, tmp_dist):
    """S3 T6a：/static 挂载与 app/static 一起退了，那些路径现在归 SPA 回退接（裁定 R-S3-1）。

    旧面的 style.css/app.js 与 mock/ 三张样张都是 S1 遗留，新前端一律从 web/dist 走
    /assets。资产既然不存在了，回一个空壳页（前端路由自己会判）比回一个 404 更不突兀——
    这是裁定认下来的形状，钉在这儿免得哪天有人手滑把挂载加回来（加回来的话这三条
    会变成 404，因为目录已经没了，而 404 与"挂载还在但目录空了"是两种不同的坏）。
    """
    for path in ("/static/style.css", "/static/app.js", "/static/mock/mock-chat.html"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert resp.headers["content-type"].startswith("text/html"), path
        assert '<div id="root"></div>' in resp.text, path


def test_static_is_404_without_dist(client, no_dist):
    """dist 不在时 /static/* 是 404：回退无页可回（与 test_unknown_path_is_404 同口径）。"""
    for path in ("/static/style.css", "/static/mock/mock-chat.html"):
        assert client.get(path).status_code == 404, path


def test_root_returns_the_s3_note_without_dist(client, no_dist):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json() == {"note": "前端在 S3 接入，当前只有 /api/*"}


def test_root_serves_index_html_when_dist_present(client, tmp_dist):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert '<div id="root"></div>' in resp.text


def test_deep_link_falls_back_to_index_html(client, tmp_dist):
    """SPA 回退：前端自己的路由（对话页、邀请注册页）刷新时也得拿到 index.html，
    不然浏览器一刷新就是 404，路由只能靠点链接走。"""
    for path in ("/c/12", "/i/some-token"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert resp.headers["content-type"].startswith("text/html"), path
        assert '<div id="root"></div>' in resp.text, path


def test_unknown_path_is_404_without_dist(client, no_dist):
    assert client.get("/c/12").status_code == 404


def test_missing_api_path_still_json_404_with_dist(client, tmp_dist):
    """catch-all 不许把 /api 吞成 index.html：前端 fetch 拿到一坨 HTML 只会报
    "JSON 解析失败"，而契约是单一信封 {"error": 人话}（裁定 R11）。"""
    resp = client.get("/api/definitely-missing")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["error"]
    assert "<div id=" not in resp.text


def test_api_405_says_it_in_chinese(client, tmp_dist):
    """裁定 R-S3-17：405 也翻人话，信封里不留英文。

    405 是 starlette 自己抛的（路径对上了、方法对不上 → "Method Not Allowed"），
    而前端的契约是"后端给的就是人话，不翻译"（web/src/api.ts 第 1 条），所以不翻的话
    横幅上就摆出一句英文。三种撞法都验：

    - 方法对不上的真路由（/api/search 只有 GET、/api/login 只有 POST、/api/chat/sessions
      只有 GET+POST）。方法不匹配在**依赖之前**就判掉了，所以未登录也是 405 而不是 401。
      两条挑路径的坑记在这儿：GET 打一条 POST-only 的 /api 路径不会 405，而是被最后那条
      GET catch-all **完全**接住（完全匹配赢过半匹配），由它的 api 守卫 raise 404；
      PATCH /api/chat/sessions 也不是 405，它被 PATCH /api/chat/{session_id} 完全接住
      （session_id 收下 "sessions" 这个串），于是先撞 401 而不是方法错；
    - 打错的路径（POST /api/<不存在> 被最后那条 GET catch-all 半接住 → 也是 405，
      不是 404：路径匹配上了，方法没有）。
    """
    for method, path in (
        ("post", "/api/search"),
        ("delete", "/api/login"),
        ("delete", "/api/chat/sessions"),
        ("post", "/api/definitely-missing"),
    ):
        resp = getattr(client, method)(path)
        assert resp.status_code == 405, (method, path)
        assert resp.headers["content-type"].startswith("application/json"), (method, path)
        assert resp.json() == {"error": "这个方法不行"}, (method, path)


def test_api_405_translation_does_not_leak_outside_api(client, tmp_dist):
    """非 /api 的 405 保持 FastAPI 默认形状（{"detail": 英文}）：这张表只管 /api 的信封。

    全站只有 /api 一个面（Task 9 起），这一支只可能被静态挂载一类基础设施走到；
    跟着一起翻的话就等于在 /api 之外也承诺了一套人话，而那套话没人消费。
    """
    resp = client.post("/")  # root() 只挂 GET
    assert resp.status_code == 405
    assert resp.json() == {"detail": "Method Not Allowed"}
    assert "这个方法不行" not in resp.text


def test_api_auth_envelope_not_swallowed_with_dist(client, tmp_dist):
    resp = client.get("/api/chat/sessions", follow_redirects=False)
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["error"]


def test_healthz_not_swallowed_with_dist(client, tmp_dist):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_healthz_trailing_slash_is_still_healthz_json(client, tmp_dist):
    """裁定 R-S3-2：/healthz/（尾斜杠变体）也得回探活 JSON，不回 SPA 的 index.html。

    精确 /healthz 由它自己的路由先接住；能落到 catch-all 的只有这条变体，
    归一化守卫（rstrip）把它也交回 healthz handler 的 JSON。
    """
    resp = client.get("/healthz/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["ok"] is True
    assert "<div id=" not in resp.text


def test_bare_api_paths_are_json_404(client, tmp_dist):
    """裁定 R-S3-2：裸 /api 与 /api/ 也是 JSON 面——老守卫（startswith("api/")）
    对 "api"、"api/" 两个 full_path 都不命中，会把它们吞成 index.html。"""
    for path in ("/api", "/api/"):
        resp = client.get(path)
        assert resp.status_code == 404, path
        assert resp.headers["content-type"].startswith("application/json"), path
        assert resp.json() == {"error": "Not Found"}, path
        assert "<div id=" not in resp.text, path


def test_missing_api_path_with_trailing_slash_is_json_404(client, tmp_dist):
    """/api/definitely-missing/ 同样 404 JSON：rstrip 归一化后仍在 api/ 前缀下。"""
    resp = client.get("/api/definitely-missing/")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["error"]


def test_healthz_still_there(client):
    assert client.get("/healthz").json()["ok"] is True


def _flatten(routes):
    """FastAPI 0.150 的 include_router 是懒展开（_IncludedRouter 包着原 router），
    装配自查要自己把包着的那层拆开才看得见真路由。"""
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _flatten(inner.routes)
        else:
            yield route


def test_no_route_registered_twice():
    """装配自查：同一条 (path, method) 只许注册一次——聚合 router 只 include 一遍。"""
    from fastapi.routing import APIRoute

    from app.main import app

    seen: set[tuple[str, str]] = set()
    for route in _flatten(app.routes):
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods:
            key = (route.path, method)
            assert key not in seen, key
            seen.add(key)
    # 拆完必须真的看得见 /api 路由，不然上面查的是一张空表
    assert any(path.startswith("/api/") for path, _ in seen), seen


def test_every_route_is_api_or_infra():
    """装配自查：除了 /api/*、/healthz、/、SPA 回退、FastAPI 自带的文档面与静态挂载，
    不许有别的路由留下（旧 HTML 面删干净的证明）。"""
    from starlette.routing import Mount

    from app.main import app

    allowed_exact = {
        "/healthz",
        "/",
        "/openapi.json",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
        # S3 Task 2 的 SPA 回退：注册在所有路由之后，只接得住没人认领的路径
        "/{full_path:path}",
    }
    for route in _flatten(app.routes):
        path = getattr(route, "path", "")
        if isinstance(route, Mount):
            # S3 T6a 起只剩 /assets 这一个挂载（/static 与 app/static 一起退了）。
            # 注意这条 Mount 只在 import 时 web/dist/assets 真的在才存在，所以
            # 没有 dist 的检出里这个分支压根不会走到——两种状态都算过。
            assert path == "/assets", path
            continue
        assert path.startswith("/api/") or path in allowed_exact, path


def test_openapi_paths_are_api_or_infra():
    """装配自查（裁定 R-S3-3）：openapi() 广告出去的每条 path 要么在 /api/* 下，
    要么是基础设施集（/、/healthz）。

    catch-all 已加 include_in_schema=False，不许再以 "/{full_path:path}" 出现在
    /docs 里；FastAPI 自带的 /openapi.json、/docs、/redoc 也都 include_in_schema=False，
    不会进 paths；/assets 是 Mount，压根不进 paths。所以白名单就只有
    /api/* 加两条基础设施，多出来的任何一条都说明有人往 schema 里塞了私货。
    """
    from app.main import app

    infra = {"/", "/healthz"}
    paths = app.openapi()["paths"]
    assert paths, "openapi paths 是空的，这条自查等于没查"
    for path in paths:
        assert path.startswith("/api/") or path in infra, path
    assert not any("{full_path" in path for path in paths), paths
