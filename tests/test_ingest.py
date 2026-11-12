"""网页来源（DeepSeek Fetch/Search API）：URL 形状、状态码翻译、规范化、落库。

S6b 起抓取不再由服务端 httpx 直连目标站点，而是调 DeepSeek 的 `POST /v1/tools/fetch`
（返回 {title, content: markdown}）；本地只保留 URL 形状校验。所以这里不再测 DNS/重定向/
HTML 抽取（那些是上游的事），改测：工具 API 的请求形状与鉴权、错误码→中文映射、搜索结果
规范化，以及"抓回来的 Markdown 落成一份材料"的整条链（upload_files/extracted_text/FTS/
raw/citation.source）保持不变。**所有 HTTP 都用假的 httpx.post，不出网、不花额度。**
"""

from __future__ import annotations

import json

import pytest

from app.config import settings
from app.services import web_ingest, web_search


# --- 假的 httpx.post：按剧本回一个响应，并记下请求 ---------------------------


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _mock_post(monkeypatch, *, status: int = 200, payload=None, capture: list | None = None,
               boom: Exception | None = None):
    """把 httpx.post 换掉（web_ingest 与 web_search 共用同一个 httpx 模块）。"""
    def fake_post(url, json=None, headers=None, timeout=None):
        if capture is not None:
            capture.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if boom is not None:
            raise boom
        return _FakeResponse(status, payload)

    monkeypatch.setattr(web_ingest.httpx, "post", fake_post)


class _setting:
    """临时改 frozen settings 的一个字段（退出还原）。"""

    def __init__(self, name: str, value):
        self.name, self.value, self.prev = name, value, None

    def __enter__(self):
        self.prev = getattr(settings, self.name)
        object.__setattr__(settings, self.name, self.value)
        return self

    def __exit__(self, *exc):
        object.__setattr__(settings, self.name, self.prev)
        return False


# --- DeepSeek 工具 API 的 URL 拼装 -------------------------------------------


def test_tools_endpoint_handles_base_with_and_without_v1():
    for base in ("https://api.deepseek.com", "https://api.deepseek.com/",
                 "https://api.deepseek.com/v1", "https://api.deepseek.com/v1/"):
        with _setting("llm_base_url", base):
            assert web_ingest.tools_endpoint("fetch") == (
                "https://api.deepseek.com/v1/tools/fetch"
            ), base


# --- Fetch：请求形状 / 成功 / 错误映射 / URL 形状 ---------------------------


def test_fetch_page_success_and_request_shape(monkeypatch):
    capture: list = []
    _mock_post(monkeypatch, payload={
        "title": " 招聘要求解读 ", "content": "# 岗位职责\n\n要求三年以上产品经验。",
    }, capture=capture)
    page = web_ingest.fetch_page("https://example.com/job")
    assert page == {
        "title": "招聘要求解读",
        "text": "# 岗位职责\n\n要求三年以上产品经验。",
        "url": "https://example.com/job",
    }
    assert len(capture) == 1
    request = capture[0]
    assert request["url"] == "https://api.deepseek.com/v1/tools/fetch"
    assert request["json"] == {"url": "https://example.com/job"}
    assert request["timeout"] == 60
    assert request["headers"]["Authorization"].startswith("Bearer ")


def test_fetch_page_title_falls_back_to_hostname(monkeypatch):
    _mock_post(monkeypatch, payload={"title": "  ", "content": "有正文的一段话。"})
    page = web_ingest.fetch_page("https://example.com/a/b")
    assert page["title"] == "example.com"


@pytest.mark.parametrize("status, expected", [
    (400, "请求不合法"),
    (401, "鉴权失败"),
    (402, "额度不够"),
    (403, "安全策略"),
    (404, "抓不到这个链接"),
    (408, "超时"),
    (413, "太大"),
    (429, "限流"),
    (500, "5xx"),
    (503, "5xx"),
])
def test_fetch_page_status_codes_are_chinese(monkeypatch, status, expected):
    _mock_post(monkeypatch, status=status)
    with pytest.raises(web_ingest.WebIngestError) as caught:
        web_ingest.fetch_page("https://example.com/x")
    message = str(caught.value)
    assert expected in message and not message.isascii(), (status, message)


def test_fetch_page_rejects_bad_url_shape_without_calling_upstream(monkeypatch):
    capture: list = []
    _mock_post(monkeypatch, payload={"title": "t", "content": "正文"}, capture=capture)
    bad_urls = [
        "",
        "file:///etc/passwd",
        "ftp://example.com/x",
        "http://user:pw@example.com/x",
        "http://",
        "https://example.com/" + "a" * 9000,
    ]
    for url in bad_urls:
        with pytest.raises(web_ingest.WebIngestError) as caught:
            web_ingest.fetch_page(url)
        message = str(caught.value)
        assert message and not message.isascii(), (url, message)
    assert capture == [], capture


def test_fetch_page_empty_content_and_non_json_are_errors(monkeypatch):
    _mock_post(monkeypatch, payload={"title": "t", "content": "   \n "})
    with pytest.raises(web_ingest.WebIngestError) as caught:
        web_ingest.fetch_page("https://example.com/x")
    assert "没有可读的正文" in str(caught.value)

    _mock_post(monkeypatch, payload=None)          # json() 直接抛 ValueError
    with pytest.raises(web_ingest.WebIngestError) as caught:
        web_ingest.fetch_page("https://example.com/x")
    assert "不是合法 JSON" in str(caught.value)


def test_fetch_page_connection_error_is_chinese(monkeypatch):
    _mock_post(monkeypatch, boom=web_ingest.httpx.ConnectError("boom"))
    with pytest.raises(web_ingest.WebIngestError) as caught:
        web_ingest.fetch_page("https://example.com/x")
    assert "连不上" in str(caught.value)


def test_fetch_page_requires_api_key(monkeypatch):
    capture: list = []
    _mock_post(monkeypatch, payload={"title": "t", "content": "正文"}, capture=capture)
    with _setting("llm_api_key", ""):
        with pytest.raises(web_ingest.WebIngestError) as caught:
            web_ingest.fetch_page("https://example.com/x")
    assert "DEEPSEEK_API_KEY" in str(caught.value)
    assert capture == []


# --- Search：请求形状 / 规范化 / 参数校验 ------------------------------------


_SEARCH_PAYLOAD = {
    "results": [
        {"title": " DeepSeek 官网 ", "url": "https://www.deepseek.com/",
         "snippet": " 官网\n首页 ", "summary": "DeepSeek 官方站点。",
         "date": "2026-10-01", "site_name": "DeepSeek", "authority": 0.98,
         "content": "整页正文（不往上层带）", "rerank_score": 9.1},
        {"title": "", "url": "", "snippet": "", "summary": ""},      # 空行应跳过
        {"title": "新闻", "url": "https://news.example/x"},
    ],
}


def test_search_web_normalizes_results(monkeypatch):
    capture: list = []
    _mock_post(monkeypatch, payload=_SEARCH_PAYLOAD, capture=capture)
    results = web_search.search_web("DeepSeek", count=2, need_content=False)
    assert len(results) == 2
    assert set(results[0]) == {
        "title", "url", "snippet", "summary", "date", "site_name", "authority",
    }
    assert results[0] == {
        "title": "DeepSeek 官网",
        "url": "https://www.deepseek.com/",
        "snippet": "官网 首页",
        "summary": "DeepSeek 官方站点。",
        "date": "2026-10-01",
        "site_name": "DeepSeek",
        "authority": 0.98,
    }
    # 缺字段的行给空串，不把 None 交给上层
    assert results[1]["title"] == "新闻"
    assert results[1]["snippet"] == "" and results[1]["date"] == ""
    # content 不往上层带
    assert "content" not in results[0] and "rerank_score" not in results[0]

    request = capture[0]
    assert request["url"] == "https://api.deepseek.com/v1/tools/search"
    assert request["json"] == {"query": "DeepSeek", "count": 2, "need_content": False}


def test_search_web_empty_results_and_none_results(monkeypatch):
    _mock_post(monkeypatch, payload={"results": []})
    assert web_search.search_web("DeepSeek") == []
    _mock_post(monkeypatch, payload={})
    assert web_search.search_web("DeepSeek") == []


def test_search_web_validates_query_and_count(monkeypatch):
    capture: list = []
    _mock_post(monkeypatch, payload={"results": []}, capture=capture)
    for kwargs in (
        {"query": ""},
        {"query": "   "},
        {"query": "字" * (web_search.MAX_QUERY_CHARS + 1)},
        {"query": "DeepSeek", "count": 0},
        {"query": "DeepSeek", "count": 51},
    ):
        with pytest.raises(web_search.WebSearchError) as caught:
            web_search.search_web(**kwargs)
        assert str(caught.value) and not str(caught.value).isascii(), kwargs
    assert capture == []


def test_search_web_status_error_is_chinese(monkeypatch):
    _mock_post(monkeypatch, status=429)
    with pytest.raises(web_search.WebSearchError) as caught:
        web_search.search_web("DeepSeek")
    assert "限流" in str(caught.value)


# --- 落库：DeepSeek Fetch 的 Markdown → 一份材料 ------------------------------

_FAKE_PAGE = {
    "title": "招聘要求解读",
    "text": "这是网页正文：要求三年以上产品经验，另外还要会写需求文档。",
    "url": "https://example.com/job",
}


def _patch_fetch(monkeypatch, page=None):
    def fake_fetch(url):
        return dict(page or _FAKE_PAGE)

    monkeypatch.setattr(web_ingest, "fetch_page", fake_fetch)


def test_ingest_url_endpoint_stores_material(client, conn, member_login, tmp_wiki,
                                             monkeypatch):
    """POST /api/ingest/url：Markdown 存成原件归档 + extracted_text + FTS + raw 全齐。"""
    _patch_fetch(monkeypatch)
    resp = client.post("/api/ingest/url", json={"url": "https://example.com/job"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"upload_id", "raw_path", "title", "url", "chars", "parse_status", "source_changed", "previous_upload_id"}
    assert body["parse_status"] == "done"
    assert body["title"] == "招聘要求解读"
    assert body["url"] == "https://example.com/job"
    assert body["chars"] == len(_FAKE_PAGE["text"])
    assert body["raw_path"].startswith(f"raw/articles/{body['upload_id']}-")

    row = conn.execute(
        "SELECT message_id, user_id, orig_filename, stored_path, mime, parse_status,"
        " parsed_text_path, source_kind, source_url FROM upload_files WHERE id = ?",
        (body["upload_id"],),
    ).fetchone()
    assert row["message_id"] is None            # 不是会话里的上传
    assert row["user_id"] == member_login.user_id
    assert row["orig_filename"] == "招聘要求解读"
    assert row["mime"] == "text/markdown"
    assert row["parse_status"] == "done"
    assert row["source_kind"] == "web"
    assert row["source_url"] == "https://example.com/job"
    assert row["stored_path"].endswith(".md")

    # 原件 = DeepSeek Fetch 回来的 Markdown，逐字一致；但 2026-10-30 起不提供下载
    original = (settings.data_dir / row["stored_path"]).read_text(encoding="utf-8")
    assert original == _FAKE_PAGE["text"]
    assert client.get(f"/api/uploads/{body['upload_id']}/download").status_code == 404

    text = (settings.data_dir / row["parsed_text_path"]).read_text(encoding="utf-8")
    assert "要求三年以上产品经验" in text

    # 检索：正文进 FTS（ref_id 是 upload id）
    hits = conn.execute(
        "SELECT kind, ref_id FROM fts_shared WHERE kind = 'upload' AND ref_id = ?",
        (str(body["upload_id"]),),
    ).fetchall()
    assert hits

    # raw 层归档文件在 vault 里
    raw_file = tmp_wiki / body["raw_path"]
    assert raw_file.is_file() and "要求三年以上产品经验" in raw_file.read_text(encoding="utf-8")


def test_ingest_url_route_needs_login(client):
    resp = client.post("/api/ingest/url", json={"url": "https://example.com/x"})
    assert resp.status_code == 401
    resp = client.post("/api/ingest/search", json={"query": "DeepSeek"})
    assert resp.status_code == 401


def test_ingest_url_route_400_for_bad_url_and_upstream_failure(client, member_login,
                                                               monkeypatch):
    # URL 形状在本地就拦（不调上游）
    for url in ("file:///etc/passwd", "http://user:pw@example.com/x", ""):
        resp = client.post("/api/ingest/url", json={"url": url})
        assert resp.status_code == 400, (url, resp.text)
        message = resp.json()["error"]
        assert message and not message.isascii(), (url, message)

    # 上游拒绝/正文为空：服务层抛 WebIngestError，路由翻 400
    def boom(url):
        raise web_ingest.WebIngestError("抓不到这个链接（404），页面可能已经不存在")

    monkeypatch.setattr(web_ingest, "fetch_page", boom)
    resp = client.post("/api/ingest/url", json={"url": "https://example.com/x"})
    assert resp.status_code == 400
    assert "404" in resp.json()["error"]


def test_web_ingest_survives_raw_failure(client, conn, member_login, tmp_wiki, monkeypatch):
    """raw 归档是旁路：它失败（这里让 store 抛 StoreError）也照样 200，raw_path=None。"""
    from app.wiki import store

    _patch_fetch(monkeypatch)

    def boom(*args, **kwargs):
        raise store.StoreError("版本库没跑通（假装 git 挂了）")

    monkeypatch.setattr(store, "ingest_raw_upload", boom)
    resp = client.post("/api/ingest/url", json={"url": "https://example.com/job"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["raw_path"] is None
    row = conn.execute(
        "SELECT source_kind, parse_status FROM upload_files WHERE id = ?",
        (body["upload_id"],),
    ).fetchone()
    assert (row["source_kind"], row["parse_status"]) == ("web", "done")


# --- 可选的搜索接口 POST /api/ingest/search ---------------------------------


def test_ingest_search_route_returns_normalized_results(client, member_login, monkeypatch):
    fake = [{
        "title": "DeepSeek 官网", "url": "https://www.deepseek.com/",
        "snippet": "官网", "summary": "DeepSeek 官方站点。",
        "date": "2026-10-01", "site_name": "DeepSeek", "authority": 0.98,
    }]
    captured: list = []

    def fake_search(query, count=5, need_content=False):
        captured.append({"query": query, "count": count, "need_content": need_content})
        return fake

    monkeypatch.setattr(web_search, "search_web", fake_search)
    resp = client.post("/api/ingest/search", json={
        "query": "DeepSeek", "count": 2, "need_content": False,
    })
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"results": fake}
    assert captured == [{"query": "DeepSeek", "count": 2, "need_content": False}]


def test_ingest_search_route_400_on_upstream_error(client, member_login, monkeypatch):
    def boom(query, count=5, need_content=False):
        raise web_ingest.WebIngestError("网页工具太忙（限流 429），隔一会儿再试")

    monkeypatch.setattr(web_search, "search_web", boom)
    resp = client.post("/api/ingest/search", json={"query": "DeepSeek"})
    assert resp.status_code == 400
    assert "429" in resp.json()["error"] or "限流" in resp.json()["error"]
