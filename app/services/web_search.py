"""DeepSeek 内置 Search API（`POST /v1/tools/search`）：关键词 → 候选网页清单。

这是给智能体用的"找来源"入口：先搜到候选页（标题/URL/摘要），再挑最相关的 URL 交给
`ingest_url` 抓进 raw 层——**搜索结果本身不能当引注来源**（引注必须逐字来自已摄入的
raw 正文，见 app/services/verify.py）。

上游返回 `{results:[{title,url,snippet,summary,content,site_name,date,authority,rerank_score}]}`；
这一层只做规范化（固定 7 个键：title/url/snippet/summary/date/site_name/authority）与
参数校验，鉴权、超时、状态码翻译复用 `web_ingest.deepseek_tool_post`（同一个 key/base）。
"""

from __future__ import annotations

from .web_ingest import WebIngestError, deepseek_tool_post

MIN_COUNT = 1
MAX_COUNT = 50
MAX_QUERY_CHARS = 128


class WebSearchError(WebIngestError):
    """搜索的用户可读错误。继承 WebIngestError：路由翻 400、agent 工具翻中文失败串，
    与网页抓取共用同一套错误口径。"""


def _check_query(query: str) -> str:
    text = " ".join(str(query or "").split())
    if not text:
        raise WebSearchError("搜索词不能为空")
    if len(text) > MAX_QUERY_CHARS:
        raise WebSearchError(f"搜索词太长了（最长 {MAX_QUERY_CHARS} 字）")
    return text


def search_web(query: str, count: int = 5, need_content: bool = False) -> list[dict]:
    """搜网页，返回规范化的结果清单；没有结果返回 []。

    每条固定 7 个键：title/url/snippet/summary/date/site_name/authority。上游多给的
    content 不往上层带（要正文就挑 URL 去 ingest_url，正文必须进 raw 才能引用）。
    """
    text = _check_query(query)
    try:
        count_int = int(count)
    except (TypeError, ValueError) as exc:
        raise WebSearchError("count 得是 1–50 的整数") from exc
    if not MIN_COUNT <= count_int <= MAX_COUNT:
        raise WebSearchError(f"count 只许 {MIN_COUNT}–{MAX_COUNT}")
    try:
        data = deepseek_tool_post(
            "search",
            {"query": text, "count": count_int, "need_content": bool(need_content)},
        )
    except WebIngestError as exc:
        # 传输层的错误是 WebIngestError；换成搜索自己的类型，调用方（路由/工具）按
        # WebSearchError 接也不会漏。
        raise WebSearchError(str(exc)) from exc
    results = data.get("results")
    if not isinstance(results, list):
        return []
    out: list[dict] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        title = " ".join(str(item.get("title") or "").split())
        if not url and not title:
            continue
        out.append({
            "title": title,
            "url": url,
            "snippet": " ".join(str(item.get("snippet") or "").split()),
            "summary": " ".join(str(item.get("summary") or "").split()),
            "date": str(item.get("date") or "").strip(),
            "site_name": str(item.get("site_name") or "").strip(),
            "authority": item.get("authority"),
        })
    return out
