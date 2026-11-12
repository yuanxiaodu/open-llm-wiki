"""wiki vault 的 lint：只读，不写任何文件（计划 §2）。

十项检查的 code 与 severity：

| code            | severity | 判据                                                     |
|-----------------|----------|----------------------------------------------------------|
| broken_link     | error    | 正文 `[[...]]` 按 slug/title 都解析不到页                 |
| orphan          | warn     | 非特殊页且全库没有任何入链                                |
| index_missing   | error    | 页面不在 index.md 里（理论上不会，防手改）                |
| frontmatter     | error    | title/type/created/updated 缺失或 type 非法、文件读不回来 |
| tag_unknown     | warn     | tags 不在 SCHEMA.md 的 `## tags` taxonomy 里              |
| source_missing  | warn     | sources 指向的文件不在 vault 里                           |
| stale           | info     | updated 超过 90 天且没有任何 sources                     |
| contested       | info     | frontmatter `contested: true` 或 contradictions 非空      |
| page_too_long   | warn     | 正文超过 200 行                                           |
| log_rotation    | info     | log.md 超过 500 条（`## [`)                               |

`raw_drift` 重算正文摘要，同时核对单独记录的原件摘要。旧归档缺正文基线时报告待核验。
语义候选仅提示人工/智能体读取复核，绝不声称结构检查已证明事实正确。
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, timedelta

from ..config import settings
from . import serialize
from .provenance import canonical_url, content_hash, source_file, source_info, split_raw
from .quality import independent_sources

PAGE_MAX_LINES = 200
STALE_DAYS = 90
LOG_MAX_ENTRIES = 500

_SEVERITY_ORDER = {"error": 0, "warn": 1, "info": 2}
# 这些是体检确认过的真实体卡（标题带冒号是"专名：身份说明"，不是命题页）
_ENTITY_SLUG_ALLOW = {"ai", "monica-ai", "monica", "mai-ke-ai-er-luo-ai"}
_SCHEMA_TAG_SECTION = re.compile(r"^##\s+(?:tags|tag taxonomy)\s*$", re.IGNORECASE | re.MULTILINE)
_NEXT_SECTION = re.compile(r"^##\s+", re.MULTILINE)


def _read(rel: str) -> str | None:
    try:
        return (settings.wiki_dir / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def schema_tags() -> set[str] | None:
    """SCHEMA.md 里 `## tags` 一节的 taxonomy；没有 SCHEMA 或没有这一节返 None（跳过校验）。"""
    text = _read("SCHEMA.md")
    if text is None:
        return None
    section = _SCHEMA_TAG_SECTION.search(text)
    if section is None:
        return None
    rest = text[section.end():]
    nxt = _NEXT_SECTION.search(rest)
    if nxt is not None:
        rest = rest[: nxt.start()]
    tags: set[str] = set()
    for line in rest.splitlines():
        item = line.strip()
        if item.startswith("- "):
            value = item[2:].strip()
            if ":" in value:
                value = value.split(":", 1)[1]
            for tag in re.split(r"[,，、;；]", value):
                tag = tag.strip().strip("`* ")
                if tag:
                    tags.add(tag)
    return tags


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def _finding(code: str, severity: str, path: str, message: str) -> dict:
    return {"code": code, "severity": severity, "path": path, "message": message}


def _raw_drift(conn, findings: list, add) -> None:
    base = settings.wiki_dir / "raw"
    if not base.is_dir():
        return
    for path in sorted(base.rglob("*.md")):
        rel = path.relative_to(settings.wiki_dir).as_posix()
        text = _read(rel)
        if text is None:
            add("raw_drift", "error", rel, "raw 文件读不回来")
            continue
        meta, body = split_raw(text)
        recorded = meta.get("sha256") or meta.get("content_sha256")
        if recorded:
            if content_hash(body) != recorded:
                add("raw_drift", "error", rel, "raw 正文摘要不匹配；保留现场，核对原始材料，不要自动重算掩盖修改")
        else:
            add("raw_integrity_unverified", "warn", rel, "旧归档缺正文摘要；需与解析原文核对后建立校验基线")


_LOG_PAGE_PATH_RE = re.compile(r"(?:entities|concepts|comparisons|queries)/[^\s`'\"<>，。；]+?\.md")


def _log_paths(text: str, pages: set[str] | None = None) -> set[str]:
    """log 里的页面 path：兼容原生 `- concepts/foo.md` 与服务端 `- path: ...`。"""
    out: set[str] = set()
    allowed = pages or set()
    for line in (text or "").splitlines():
        item = line.strip()
        if item.startswith("- path:"):
            path = item[len("- path:"):].strip().lstrip("./")
            if path:
                out.add(path)
            continue
        for match in _LOG_PAGE_PATH_RE.finditer(item):
            path = match.group(0).lstrip("./")
            if not allowed or path in allowed:
                out.add(path)
    return out


def _title_link_count(page, pages) -> int:
    """正文里"按标题解析"的链接数（Obsidian 不认，只在我们 app 里能点）。"""
    from . import store

    count = 0
    for link in store.wikilinks_of(page, pages):
        target = str(link.get("target") or "")
        if link.get("path") is not None and not store.valid_slug(target):
            count += 1
    return count


def _outlink_count(rel: str, page, pages) -> int:
    """正文里指向**其他页**的 wikilink 数（同一目标去重；cite 不算）。"""
    from . import store

    targets = set()
    for link in store.wikilinks_of(page, pages):
        path = link.get("path")
        if path is not None and path != rel:
            targets.add(path)
    return len(targets)


def lint(conn=None, *, paths: set[str] | None = None) -> dict:
    """跑完所有检查，返 {findings:[{code,severity,path,message}], summary:{total,error,warn,info}}。"""
    from . import store  # 懒导入：store.lint 反过来调本模块（避免 import 环）

    pages = store.all_pages()
    graph, incoming = store.link_graph(pages)
    findings: list[dict] = []

    def add(code: str, severity: str, path: str, message: str) -> None:
        findings.append(_finding(code, severity, path, message))

    # 1) frontmatter：parse 不过的文件（不在 all_pages 里）也要报，所以再扫一遍文件清单。
    for rel in store.page_files():
        page = store._parse_file(rel)
        if page is None:
            add("frontmatter", "error", rel, "front matter 读不回来（缺 title/type 或形状不对）")
            continue
        meta, _ = serialize._frontmatter((_read(rel) or "").splitlines())
        absent = sorted({"title", "type", "created", "updated", "tags", "sources"} - set(meta))
        if absent:
            add("frontmatter", "error", rel, "front matter 缺必需字段：" + "、".join(absent))
        missing = [
            name for name, value in (
                ("title", page.title), ("type", page.page_type),
                ("created", page.created), ("updated", page.updated),
            ) if not value
        ]
        if missing:
            add("frontmatter", "error", rel, f"front matter 缺 {'、'.join(missing)}")
        elif _parse_date(page.created) is None or _parse_date(page.updated) is None:
            add("frontmatter", "error", rel, "created/updated 不是 YYYY-MM-DD")

    index_text = _read("index.md") or ""

    taxonomy = schema_tags()
    raw_versions = {}
    raw_root = settings.wiki_dir / "raw"
    if raw_root.exists():
        for path in raw_root.rglob("*.md"):
            try:
                meta, body = split_raw(path.read_text(encoding="utf-8"))
                url = canonical_url(meta.get("source_url", ""))
                if url:
                    raw_versions.setdefault(url, set()).add(meta.get("content_sha256") or content_hash(body))
            except (OSError, UnicodeError):
                continue
    today = date.today()
    for rel, page in pages.items():
        # 2) broken_link
        for link in graph[rel]:
            if link["path"] is None:
                add(
                    "broken_link", "error", rel,
                    f"[[{link['target']}]] 按 slug 和标题都找不到对应的页",
                )
        # 3) orphan
        if not incoming[rel]:
            add("orphan", "warn", rel, "没有任何页面链到这一页（孤立页）")
        # 4) index_missing（index 里用 Obsidian 别名写法 [[slug|title]]，两种都认）
        if not re.search(r"\[\[" + re.escape(page.slug) + r"(?:\||\]\])", index_text):
            add("index_missing", "error", rel, "这一页不在 index.md 里（index 可能被手改过）")
        # 5) tag_unknown
        if taxonomy is not None:
            unknown = [tag for tag in page.tags if tag not in taxonomy]
            if unknown:
                add(
                    "tag_unknown", "warn", rel,
                    f"tags 不在 SCHEMA.md 的 taxonomy 里：{'、'.join(unknown)}",
                )
        # 6) source_missing
        for source in page.sources:
            if canonical_url(source):
                continue
            if source_file(settings.wiki_dir, source) is None:
                add("source_missing", "warn", rel, f"sources 指向的文件不在：{source}")
        # Compare against source dates; retaining sources does not exempt a page from review.
        updated = _parse_date(page.updated)
        dates = []
        for source in page.sources:
            target = source_file(settings.wiki_dir, source)
            if target is not None:
                try:
                    meta, _ = source_info(target)
                    url = canonical_url(meta.get("source_url", ""))
                    if url and len(raw_versions.get(url, set())) > 1:
                        add("source_update_available", "info", rel, "同一 URL 存在不同内容快照，需核对本页适用版本")
                    source_date = _parse_date(meta.get("published") or meta.get("updated") or meta.get("ingested", ""))
                    if source_date:
                        dates.append(source_date)
                except (OSError, UnicodeError):
                    pass
        if updated and ((dates and max(dates) - updated > timedelta(days=STALE_DAYS))
                        or (not page.sources and today - updated > timedelta(days=STALE_DAYS))):
            add("stale", "info", rel, "页面早于较新的来源或长期缺乏来源，需要复核")
        if len(independent_sources(page.sources)) >= 3 and not re.search(r"\^\[raw/[^\]]+\]", page.body_md):
            add("provenance_missing", "warn", rel, "多来源综合缺少段落证据映射，需标注各结论来源")
        # 8) contested
        if page.contested or page.contradictions:
            other = "、".join(page.contradictions) or "（未指名）"
            add("contested", "info", rel, f"这一页记了争议（与 {other} 矛盾/存疑）")
        # 9) page_too_long
        line_count = len(page.body_md.splitlines())
        if line_count > PAGE_MAX_LINES:
            add(
                "page_too_long", "warn", rel,
                f"正文 {line_count} 行，超过 {PAGE_MAX_LINES} 行的阈值，考虑拆页",
            )

        # 12) title_link：按标题解析的链接在我们 app 能点，但 Obsidian 按文件名解析，
        # 文件改成拼音后就是 16 条断链。标题链接只允许作为建页过程中的临时占位。
        title_links = sum(link["path"] is not None and not store.valid_slug(link["target"]) for link in graph[rel])
        if title_links:
            add(
                "title_link", "error", rel,
                f"有 {title_links} 个 [[标题]] 链接，必须改成 [[slug]]（Obsidian 不认标题解析）",
            )
        # 13) outlink_min：所有已发布页和草稿都显示真实出链情况
        if True:
            outlinks = len({link["path"] for link in graph[rel]} - {None, rel})
            if outlinks < 2:
                add(
                    "outlink_min", "warn", rel,
                    f"只链到 {outlinks} 个其他页（要求 ≥2）；孤立文档不等于知识网络",
                )
        # 14) numeric_slug：只剩数字/连字符的 slug 没有语义
        if re.fullmatch(r"[0-9-]+", page.slug):
            add("numeric_slug", "warn", rel, f"slug `{page.slug}` 只有数字，建议改成拼音语义 slug")
        # 15) confidence_missing：没有质量信号的页，读的人无法判断可信度
        if not (page.confidence or "").strip():
            add("confidence_missing", "info", rel, "没填 confidence（high/medium/low）")
        elif page.confidence == "low":
            add("confidence_low", "warn", rel, "证据强度低，需补充来源或保留不确定性")
        elif page.confidence == "high" and (page.contested or len(independent_sources(page.sources)) < 2):
            add("confidence_unsupported", "warn", rel, "high 缺少可识别的独立来源或存在争议，需复核")
        elif page.confidence not in ("high", "medium", "low"):
            add("confidence_invalid", "error", rel, "confidence 值不合法")
        if not page.sources:
            add("sources_empty", "warn", rel, "尚未记录原始来源，需核对证据后补充；空列表不代表证据已核验")
        # 17) type_heuristic：entity 应该是实体，不是“专名：论断”命题
        if (
            page.page_type == "entity"
            and page.slug not in _ENTITY_SLUG_ALLOW
            and ("：" in page.title or ":" in page.title)
        ):
            add(
                "type_heuristic", "info", rel,
                f"entity 标题像命题（{page.title}），考虑改成 concept",
            )

    # 18) source_coverage：每个 raw 源至少被 1 页引用
    raw_base = settings.wiki_dir / "raw"
    if raw_base.is_dir():
        referenced = {source for page in pages.values() for source in page.sources}
        for path in sorted(raw_base.rglob("*.md")):
            rel = path.relative_to(settings.wiki_dir).as_posix()
            if rel not in referenced:
                add("source_coverage", "warn", rel, "这个 raw 源尚待编译；不属于本轮结构错误")

    # 19) log_coverage：每页至少有一条日志记录（create/edit）
    log_text = _read("log.md") or ""
    archive_logs = "\n".join(p.read_text(encoding="utf-8") for p in settings.wiki_dir.glob("log-*.md") if p.is_file())
    logged = _log_paths(log_text + "\n" + archive_logs, set(pages))
    for rel in pages:
        if rel not in logged:
            add("log_coverage", "warn", rel, "这一页没有任何 log 记录（create/edit 缺失）")

    # 21) tag_skew：单一标签覆盖过半，标签失去筛选价值
    if len(pages) >= 20:
        tag_counts = Counter(tag for page in pages.values() for tag in page.tags)
        if tag_counts:
            tag, count = tag_counts.most_common(1)[0]
            if count / len(pages) > 0.5:
                add(
                    "tag_skew", "info", "SCHEMA.md",
                    f"标签「{tag}」覆盖 {count}/{len(pages)} 页（>50%），考虑拆分 taxonomy",
                )

    # 22) index 链接必须闭合：摘要安全截断后不应再出现未闭合的 [[
    index_text = _read("index.md") or ""
    if index_text.count("[[") != index_text.count("]]"):
        add("index_unbalanced", "error", "index.md", "index.md 里有未闭合的 wikilink（摘要截断导致）")

    # 10) log_rotation
    entries = sum(1 for line in log_text.splitlines() if line.startswith("## ["))
    if entries > LOG_MAX_ENTRIES:
        add("log_rotation", "info", "log.md", f"log.md 已有 {entries} 条，考虑轮转")

    # 11) raw_drift（计划 §3）
    _raw_drift(conn, findings, add)

    if paths is not None:
        affected = set(paths)
        for rel in paths:
            affected.update(link["path"] for link in graph.get(rel, []) if link["path"])
            affected.update(item["path"] for item in incoming.get(rel, []))
        findings = [item for item in findings if item["path"] in affected]
    findings.sort(key=lambda item: (item["path"], _SEVERITY_ORDER[item["severity"]], item["code"]))
    summary = {
        "total": len(findings),
        "error": sum(1 for item in findings if item["severity"] == "error"),
        "warn": sum(1 for item in findings if item["severity"] == "warn"),
        "info": sum(1 for item in findings if item["severity"] == "info"),
    }
    candidates = []
    for rel, page in pages.items():
        related = [other for other, p in pages.items() if other != rel
                   and set(page.tags) & set(p.tags)]
        if related:
            candidates.append({"path": rel, "related": related[:3],
                               "reason": "同主题证据需复核；候选不等于已确认的矛盾"})
    return {"findings": findings, "summary": summary,
            "review_candidates": candidates[:20],
            "checks": {"structure": "checked", "source_integrity": "checked",
                       "semantic": "review_required" if candidates else "not_assessed",
                       "semantic_pages_checked": 0, "pages_total": len(pages)}}
