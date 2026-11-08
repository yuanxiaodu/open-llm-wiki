"""网页摄入：DeepSeek 内置 Fetch API（`POST /v1/tools/fetch`）→ Markdown → 一份「材料」。

S6b 起把"服务端自己 httpx 抓 HTML + HTMLParser 抽正文"整段换掉：抓取与正文抽取交给
DeepSeek 的 Fetch 工具（官方 API，返回 `{title, content}`，content 就是 Markdown 正文）。
好处是正文质量由上游负责，坏处是本地不再做 DNS/内网校验——所以这一层只保留 **URL 形状
校验**（http(s)、带 host、无 userinfo、长度 ≤8192），什么地址能抓由 DeepSeek 侧裁定。

错误映射成人话（`WebIngestError`，继承 ValueError：agent 工具层的 `_guard` 与路由层的
400 都按 ValueError 处理）：400 链接不合法、401 key 不对、402/429 额度或限流、403 对方
站点安全策略拒抓、404 抓不到、408 超时、413 太大、5xx 上游故障。

落库口径不变（契约与 S5b 相同）：Markdown 原文存 `data/uploads/{user_id}/{uuid}.md`
作为"原件"（只作来源归档，不提供下载）、正文写 `data/extracted_text/`、`upload_files` 记
`source_kind='web'` 与 `source_url`、进 FTS、交给 wiki raw 层归档（失败只记日志）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from urllib.parse import urlsplit

import httpx

from ..config import settings
from ..db import utcnow
from ..wiki.provenance import canonical_url

logger = logging.getLogger(__name__)

FETCH_TIMEOUT = 60.0
MAX_URL_LENGTH = 8192


class WebIngestError(ValueError):
    """网页摄入的用户可读错误（URL 不合法、上游拒绝、正文为空）。消息是中文。

    继承 ValueError：agent 工具层 `_guard` 统一翻成中文失败串，路由层翻 400。
    """


# --- DeepSeek 工具 API：URL 拼装 + 统一请求 ----------------------------------


def tools_endpoint(name: str) -> str:
    """`{llm_base_url}` → `https://api.deepseek.com/v1/tools/{name}`。

    base 可能配成 `https://api.deepseek.com`，也可能为了 OpenAI 兼容写了
    `https://api.deepseek.com/v1`（客户端那边会自己接 `/chat/completions`）——两种都要
    拼对，所以先把结尾的 `/v1` 削掉，再统一拼 `/v1/tools/…`。
    """
    base = (settings.llm_base_url or "https://api.deepseek.com").rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")].rstrip("/")
    return f"{base}/v1/tools/{name}"


def _status_text(status: int) -> str:
    """DeepSeek 工具 API 的状态码 → 中文人话（官方错误码：400/401/402/403/404/408/413/429/5xx）。"""
    if status == 400:
        return "这个链接抓不了（请求不合法）"
    if status == 401:
        return "网页工具鉴权失败：检查 LLM_API_KEY（旧名 DEEPSEEK_API_KEY） 配没配对"
    if status == 402:
        return "网页工具额度不够了（账户余额不足），充完再试"
    if status == 403:
        return "对方站点按安全策略拒绝了抓取（403），这个链接抓不了"
    if status == 404:
        return "抓不到这个链接（404），页面可能已经不存在"
    if status == 408:
        return "抓这个链接超时了，稍后再试"
    if status == 413:
        return "这个网页太大了（413），抓不了"
    if status == 429:
        return "网页工具太忙（限流 429），隔一会儿再试"
    if 500 <= status < 600:
        return "DeepSeek 的网页工具出错了（5xx），稍后再试"
    return f"抓取失败（HTTP {status}）"


def deepseek_tool_post(path: str, payload: dict, *, timeout: float = FETCH_TIMEOUT) -> dict:
    """POST 一次 DeepSeek 工具 API，返回解析后的 JSON dict；所有失败翻成中文错误。"""
    if not settings.llm_api_key:
        raise WebIngestError("LLM_API_KEY（旧名 DEEPSEEK_API_KEY） 没配，网页抓不了。复制 .env.example 填上")
    url = tools_endpoint(path)
    try:
        response = httpx.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {settings.llm_api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        logger.warning("DeepSeek 网页工具连不上（%s）：%r", url, exc)
        raise WebIngestError("连不上 DeepSeek 的网页工具，稍后再试") from exc
    if response.status_code >= 400:
        raise WebIngestError(_status_text(response.status_code))
    try:
        data = response.json()
    except ValueError as exc:
        raise WebIngestError("网页工具返回的不是合法 JSON，稍后再试") from exc
    if not isinstance(data, dict):
        raise WebIngestError("网页工具返回的形状不对，稍后再试")
    return data


# --- Fetch API ---------------------------------------------------------------


def _check_url(url: str) -> str:
    """URL 形状校验（不解析 DNS）：http/https、带 host、无 userinfo、长度 ≤ 8192。"""
    raw = (url or "").strip()
    if not raw:
        raise WebIngestError("链接不能为空")
    if len(raw) > MAX_URL_LENGTH:
        raise WebIngestError(f"链接太长了（最长 {MAX_URL_LENGTH} 字符）")
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise WebIngestError("链接格式不对") from exc
    if parsed.scheme not in ("http", "https"):
        raise WebIngestError("只支持 http/https 链接")
    if parsed.username is not None or parsed.password is not None:
        raise WebIngestError("链接里不能带用户名或密码")
    if not (parsed.hostname or "").strip():
        raise WebIngestError("链接里没有域名")
    return raw


def fetch_page(url: str) -> dict:
    """一个链接 → `{title, text, url}`：text 是 DeepSeek Fetch 返回的 Markdown 正文。

    正文为空（空页、纯图片页、或上游没抽出来）按错误处理——没有正文的材料摄进来也引不到。
    """
    clean = _check_url(url)
    data = deepseek_tool_post("fetch", {"url": clean})
    text = str(data.get("content") or "").strip()
    if not text:
        raise WebIngestError("这个网页没有可读的正文（可能是空页、纯图片页或抓取被拦）")
    title = " ".join(str(data.get("title") or "").split())
    title = (title or urlsplit(clean).hostname or "网页")[:120]
    return {"title": title, "text": text, "url": clean}


# --- 落库：网页 → 一份「材料」 ------------------------------------------------


def ingest_url(conn, url: str, user_id: int, actor: str) -> dict:
    """抓网页并落成一份材料（upload_files + extracted_text + FTS + raw），返回摘要 dict。

    `source_kind='web'`、`source_url` 是这次请求的 URL；`message_id` 为 NULL（不是会话里
    的一次上传，是独立摄入）。Markdown 原文存成"原件"（`.md`，只作来源归档、不提供
    下载），解析文本另写一份给检索/引注校验用。raw 归档是旁路：失败只记日志，raw_path 给 None。
    """
    page = fetch_page(url)
    from . import search, uploads  # 懒导入：uploads 反向依赖本模块之外的东西，避免环

    canonical = canonical_url(page["url"])
    prior = conn.execute("SELECT * FROM upload_files WHERE source_kind='web' ORDER BY id DESC").fetchall()
    matching = next((r for r in prior if canonical_url(r["source_url"] or "") == canonical), None)
    digest = hashlib.sha256(page["text"].encode("utf-8")).hexdigest()
    if matching and matching["sha256"] == digest:
        raw_path = uploads.ingest_raw(conn, matching["id"], matching["orig_filename"], actor)
        return {"upload_id": matching["id"], "raw_path": raw_path, "title": page["title"],
                "url": page["url"], "chars": len(page["text"]), "parse_status": "done", "unchanged": True}
    directory = settings.uploads_dir / str(user_id)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{uuid.uuid4().hex}.md"
    markdown = page["text"]
    target.write_text(markdown, encoding="utf-8")

    text = markdown
    parsed_text_path = uploads._write_parsed_text(target, text)
    report = uploads._report([text])
    payload = markdown.encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    cur = conn.execute(
        "INSERT INTO upload_files (message_id, user_id, orig_filename, stored_path, size,"
        " mime, sha256, parse_status, parsed_text_path, parse_report, created_at,"
        " source_kind, source_url)"
        " VALUES (NULL, ?, ?, ?, ?, 'text/markdown', ?, 'done', ?, ?, ?, 'web', ?)",
        (
            user_id,
            page["title"],
            str(target.relative_to(settings.data_dir)),
            len(payload),
            digest,
            parsed_text_path,
            json.dumps(report, ensure_ascii=False),
            utcnow(),
            page["url"],
        ),
    )
    upload_id = int(cur.lastrowid)
    search.index_upload(conn, upload_id)
    raw_path = uploads.ingest_raw(conn, upload_id, page["title"], actor)
    return {
        "upload_id": upload_id,
        "raw_path": raw_path,
        "title": page["title"],
        "url": page["url"],
        "chars": len(text),
        "parse_status": "done",
        "source_changed": matching is not None,
        "previous_upload_id": matching["id"] if matching else None,
    }
