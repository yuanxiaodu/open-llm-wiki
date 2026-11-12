"""站点名可配置（SITE_NAME）的钉子：一条公开接口 + 入口页标题注入。

为什么这两件事承重：

- **站名是部署方唯一要改的品牌字**。它写死在源码里的话，换一个部署就得改三四个
  React 文件；所以后端 `/api/site` 与入口页 `<title>` 都从同一个 `settings.site_name`
  出，前端（web/src/site.ts）只消费接口。这里钉住"后端只有一个来源"。
- **`/api/site` 必须公开**。登录页在人拿到身份之前就要显示站名，它要是像 `/api/me`
  一样要登录，未登录的登录页就只能退回写死的兜底文案——那正是本任务要去掉的东西。
- **标题在响应时注入**，不是构建时写死：`web/dist` 是构建产物、每台部署共享，
  `SITE_NAME` 却是每台机器自己的 `.env`。同时注入必须 `html.escape`：`SITE_NAME`
  是运维可填的自由文本，不转义就能被拼成 `</title><script>…` 把页面结构打开。
- **不跨请求缓存**：`tests/test_api_wiring.py` 靠 monkeypatch `app.main.WEB_DIST`
  测"dist 在/不在"两种状态；任何模块级缓存都会让第二台机器/第二次请求拿到旧页面。

环境差异提醒（别写成字面量断言）：`app/config.py` 在 import 时会把仓库里真实存在的
`.env` 读进 `os.environ`（`os.environ.setdefault`），所以本机与 CI 的 `settings.site_name`
可能不同——这里一律断言"与 settings 的关系"，不钉某个中文串。
"""

from __future__ import annotations

from html import escape as html_escape
from types import SimpleNamespace

import pytest

from app.config import DEFAULT_SITE_NAME, _site_name, settings

# 构建产物里的静态标题：服务端必须把它替换掉，留作 dev/无 JS 兜底。
STATIC_TITLE = "构建产物里的静态标题"
INDEX_HTML = (
    '<!doctype html><html lang="zh-CN"><head>'
    f"<title>{STATIC_TITLE}</title>"
    "</head><body><div id=\"root\"></div>"
    '<script type="module" src="/assets/index-TESTHASH.js"></script>'
    "</body></html>"
)


@pytest.fixture
def tmp_dist(tmp_path, monkeypatch):
    """把 `app.main.WEB_DIST` 指到一个假 dist（与 tests/test_api_wiring.py 同一手法）。

    不跟那边共用 fixture：fixture 定义在测试模块里不会被别的模块看见，而提到
    conftest 又不是本任务的写范围。这里只造入口页，够标题注入用。
    """
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    monkeypatch.setattr("app.main.WEB_DIST", dist)
    return dist


def _expected_title() -> str:
    """按当前 settings 算期望标题：中文串从 settings 取，不写死（见文件头环境提醒）。"""
    return f"<title>{html_escape(settings.site_name)}</title>"


def test_settings_site_name_is_clean_and_never_empty():
    """配置层的不变量：非空、已 strip。前端拿它去当标题，空串会让标签页显示成地址。"""
    assert settings.site_name
    assert settings.site_name == settings.site_name.strip()
    # 空串/纯空白落回默认值（工具函数与 Settings 用的是同一个 _site_name）
    assert _site_name("") == DEFAULT_SITE_NAME
    assert _site_name(" \t\n ") == DEFAULT_SITE_NAME
    assert _site_name("  某个站名  ") == "某个站名"


def test_site_api_is_public_and_returns_config(client):
    """未登录也能打：站名、描述、类型标签、欢迎语、已启用插件都在登录前可见。"""
    resp = client.get("/api/site")
    assert resp.status_code == 200, resp.text[:400]
    body = resp.json()
    assert set(body) == {"name", "description", "type_labels", "welcome", "plugins"}
    assert body["name"] == settings.site_name
    assert body["name"].strip() == body["name"]
    assert body["plugins"] == []
    assert set(body["type_labels"]) == {"entity", "concept", "comparison", "query"}
    assert set(body["welcome"]) == {"build", "readonly"}


def test_site_api_reads_settings_at_request_time(client, monkeypatch):
    """接线钉在"每请求现读 settings"上，不是 import 时的快照。

    先把模块里的 settings 换成占位对象，再请求两次：第二次换掉占位对象后响应必须
    跟着变——这同时排除了"路由函数把站名缓存在闭包/模块常量里"的写法。
    """
    monkeypatch.setattr(
        "app.routers.api.site.settings", SimpleNamespace(site_name="钉住的站名")
    )
    assert client.get("/api/site").json()["name"] == "钉住的站名"
    monkeypatch.setattr(
        "app.routers.api.site.settings", SimpleNamespace(site_name="换了个站名")
    )
    assert client.get("/api/site").json()["name"] == "换了个站名"


def test_docs_title_does_not_double_the_default_site_name():
    """默认站名时 /docs 的标题就是「知识库」，不是「知识库 · 知识库」。

    FastAPI(title=…) 的拼接规则（lead 裁定）：默认站名不再加品类后缀；配置了真实站名
    才补「· 知识库」。标题在 import 时就固定了，测试改不动，所以这条按"当前 settings
    与默认值的关系"断言，不钉中文字面量。
    """
    from app.main import app

    expected = (
        settings.site_name
        if settings.site_name == DEFAULT_SITE_NAME
        else f"{settings.site_name} · {DEFAULT_SITE_NAME}"
    )
    assert app.title == expected
    assert app.title != f"{DEFAULT_SITE_NAME} · {DEFAULT_SITE_NAME}"


def test_root_injects_site_name_into_title(client, tmp_dist):
    """`/` 回的入口页里，构建产物那个静态 <title> 被换成 SITE_NAME。"""
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert '<div id="root"></div>' in resp.text
    assert _expected_title() in resp.text
    assert f"<title>{STATIC_TITLE}</title>" not in resp.text


def test_spa_fallback_injects_site_name_into_title(client, tmp_dist):
    """SPA 回退（深链接刷新）回的也是同一份注入后的入口页，不是两套逻辑。"""
    resp = client.get("/some/deep/link")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert _expected_title() in resp.text
    assert f"<title>{STATIC_TITLE}</title>" not in resp.text


def test_title_injection_escapes_a_hostile_site_name(client, tmp_dist, monkeypatch):
    """SITE_NAME 是运维可填文本，注入前必须转义。

    只 monkeypatch 站名、不换真正的 settings 单例：这条测的是 main.py 的注入方式。
    如果不转义，`</title>` 会提前关掉标题、后面的 `<script>` 就成了页面上的一行脚本。
    """
    hostile = "</title><script>alert(1)</script>"
    monkeypatch.setattr("app.main.settings", SimpleNamespace(site_name=hostile))
    text = client.get("/").text
    assert "<script>alert(1)</script>" not in text
    assert f"<title>{html_escape(hostile)}</title>" in text


def test_title_is_recomputed_per_request_not_cached(client, tmp_dist, monkeypatch):
    """既不缓存 settings 的快照，也不缓存整页 HTML。

    第一段换 settings：下一次请求的标题必须跟着变（排除"第一次渲染后缓存起来"）。
    第二段直接改 dist 里的文件：下一次请求必须读到新字节（排除"整页 HTML 缓存在
    模块级"——那会让测试 monkeypatch WEB_DIST 的第二条用例看到上一台机器的页面）。
    """
    assert _expected_title() in client.get("/").text

    monkeypatch.setattr("app.main.settings", SimpleNamespace(site_name="第二个站名"))
    assert "<title>第二个站名</title>" in client.get("/").text

    (tmp_dist / "index.html").write_text(
        "<!doctype html><html><head><title>旧</title></head>"
        '<body><div id="root"></div>MARKER-换过文件</body></html>',
        encoding="utf-8",
    )
    assert "MARKER-换过文件" in client.get("/").text
