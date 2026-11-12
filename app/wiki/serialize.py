"""wiki 页的 markdown 形状：新 frontmatter + 正文 + 尾部引注块。

这是文件形状的唯一读写者。模型与人都不手写 front matter 与引注块，
由 store 调 dump_md 序列化，读侧调 parse_md 反序列化。

S5 起 wiki 是 Hermes 标准的物理 vault（`data/wiki/`，Obsidian 可直接打开）：

    SCHEMA.md / index.md / log.md + raw/ + {entities,concepts,comparisons,queries}/

一页的形状是 `{dir}/{slug}.md`，目录由 `type` 决定（`page_path`），`type` 白名单见 TYPES。
frontmatter 的唯一写者是本模块的 dump_md，因此 `created`/`updated`/tags/sources 的排序
与格式都在这里钉死（store 只负责算值）。
"""

from __future__ import annotations

import re
import json
from dataclasses import dataclass, field
from datetime import date

# 旧布局（S5 之前）的 type 白名单：迁移时还要认，新写只认 TYPES。
LEGACY_TYPES = ("topic", "outline", "summary")

TYPES = ("entity", "concept", "comparison", "query")

# frontmatter 的已知键：其余键原样保留到 Page.extra（Obsidian 插件字段不丢）。
KNOWN_FRONTMATTER = (
    "title", "created", "updated", "type", "tags", "sources",
    "confidence", "contested", "contradictions",
)

# type → 物理目录。顺序也是索引分节的顺序（固定下来，index.md 的输出才稳定）。
DIRS = {
    "entity": "entities",
    "concept": "concepts",
    "comparison": "comparisons",
    "query": "queries",
}

# vault 根上的特殊文件：不是页面，不进 pages 列表，不可编辑。
SPECIAL_FILES = ("SCHEMA.md", "index.md", "log.md")

# raw 层：原生 2.1.0 子目录；上传材料只读、不可变。
RAW_DIR = "raw"
RAW_DIRS = ("raw/articles", "raw/papers", "raw/transcripts", "raw/assets")

_SLUG_SAFE = re.compile(r"[^A-Za-z0-9_-]+")
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
SLUG_MAX = 60


class PageFormatError(ValueError):
    """文件形状不对：缺 front matter、type 不在白名单、引注行缺字段。"""


@dataclass
class Citation:
    """历史兼容：句子级引注已下线（2026-10-30），parse 不再产出、dump 不再写。"""

    key: str
    source_kind: str
    source_id: int
    quote: str


@dataclass
class Page:
    slug: str
    title: str
    page_type: str
    body_md: str
    # 历史兼容字段：不再参与读写（引注功能已删）
    citations: list[Citation] = field(default_factory=list)
    # 日期一律 YYYY-MM-DD（计划 §1）。created 首写定，updated 每次写更新（store 算）。
    created: str = ""
    updated: str = ""
    tags: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    confidence: str = ""          # high|medium|low；空=没写
    contested: bool = False
    contradictions: list[str] = field(default_factory=list)
    # frontmatter 里我们不认识的键（Obsidian/Dataview 字段一类）：原样保留、原样写回。
    extra: dict[str, str] = field(default_factory=dict)


def semantic_slug(title: str) -> str:
    """标题 → 可读 slug：汉字转拼音，英文/数字原样，标点当分隔。

    2026-10-30 起取代"非 ASCII 一律 page-日期"的旧兜底：旧兜底让中文标题全变成
    `page-2026-10-23-57`，链接不可读，还得靠一次性迁移改名。现在的规则：
    - 取标题第一小节（冒号/逗号/顿号/破折号前，≥4 字才用）——书里切片标题的前半句
      通常就是主题；
    - 汉字逐字转拼音；连续 ASCII/数字自成一个 token（避免 2021 被拆成 2-0-2-1）；
    - 最长 SLUG_MAX=60，超了在最后一个连字符处截断；清洗后为空返回空串（调用方兜底）。

    依赖 pypinyin（requirements 里已固定）；没装/转换失败退回纯 ASCII 清洗，
    再空就由 slugify 兜底成 page-日期（极端标题才走到）。
    """
    text = (title or "").strip()
    if not text:
        return ""
    # 标题第一小节作主 slug（"技术狂热者：高科技营销..." → "技术狂热者"）
    parts = re.split(r"[：:，,。；;、（）()—]", text, maxsplit=1)
    lead = parts[0].strip() if parts else ""
    if len(lead) >= 4:
        text = lead

    tokens: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            tokens.append("".join(buffer))
            buffer.clear()

    try:
        from pypinyin import Style, lazy_pinyin
    except Exception:  # noqa: BLE001 —— 没装 pypinyin 就退回纯 ASCII 清洗
        lazy_pinyin = None
        Style = None

    for char in text:
        if _CJK_RE.match(char) and lazy_pinyin is not None:
            flush()
            pieces = lazy_pinyin(char, style=Style.NORMAL)
            if pieces and pieces[0]:
                tokens.append(pieces[0])
        elif char.isascii() and (char.isalnum() or char in "_-"):
            buffer.append(char)
        else:
            flush()
    flush()
    raw = "-".join(token for token in tokens if token)
    cleaned = _SLUG_SAFE.sub("-", raw).strip("-").lower().lstrip("-_").strip("-")
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    if not cleaned:
        return ""
    if len(cleaned) > SLUG_MAX:
        cut = cleaned.rfind("-", 0, SLUG_MAX)
        cleaned = cleaned[: cut if cut > 20 else SLUG_MAX].strip("-")
    return cleaned


def slugify(title: str) -> str:
    """标题 → 全库唯一 slug 的**基准形态**（撞车由 store.new_slug 补 -2/-3）。

    前导 `_`/`-` 必须剥掉：store.valid_slug 不收前导 dash 与点，
    标题以 `__init__` / `_私有大纲` 开头时会产生"写得进盘、GET 404、目录里看不见"的页。
    2026-10-30 起中文标题走拼音（semantic_slug），只有清洗后为空的极端标题才退回
    `page-{日期}`。
    """
    slug = semantic_slug(title)
    if slug:
        return slug
    return f"page-{date.today().isoformat()}"


def page_dir(page_type: str) -> str:
    """type → 目录名；白名单外抛 PageFormatError（与「type 不在白名单」同一个信封）。"""
    try:
        return DIRS[page_type]
    except KeyError:
        raise PageFormatError(f"type 不在白名单：{page_type}") from None


def page_path(page: Page) -> str:
    """一页在 vault 里的相对 path：`{dir}/{slug}.md`。"""
    return f"{page_dir(page.page_type)}/{page.slug}.md"


def path_for(slug: str, page_type: str) -> str:
    """按 slug + type 拼出相对 path（不检查磁盘上在不在）。"""
    return f"{page_dir(page_type)}/{slug}.md"


def scalar(value: str, *, flow: bool = False) -> str:
    text = str(value)
    unsafe = (not text or ": " in text or " #" in text or text[0] in "[{}]#&*!|>\"'%@`"
              or text.lower() in ("true", "false", "null", "yes", "no", "on", "off", "~")
              or text.startswith(("- ", "? ", ": ")) or "\n" in text or "\r" in text
              or bool(re.fullmatch(r"[-+]?[0-9]+(?:\.[0-9]+)?", text))
              or (flow and any(ch in text for ch in ",[]{}\"'")))
    return json.dumps(text, ensure_ascii=False) if unsafe else text


def _flow_list(values) -> str:
    """inline YAML flow 清单：`[a, b]`。空清单写 `[]`（frontmatter 里这个键永远在）。

    稳定排序在 dump 时做：tags/sources/contradictions 的集合语义不依赖提交顺序，
    排一下输出才逐字稳定（no-op 判定靠的就是逐字相同）。
    """
    items = sorted(str(v).strip() for v in (values or []) if str(v).strip())
    return json.dumps(items, ensure_ascii=False) if any(scalar(item, flow=True).startswith('"') for item in items) else "[" + ", ".join(items) + "]"


def _parse_flow_list(raw: str) -> list[str]:
    """解析 frontmatter 里的 inline 清单；不认的写法（块清单）给空清单。"""
    text = (raw or "").strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    except ValueError:
        pass
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if not text.strip():
        return []
    import csv
    out = []
    for part in next(csv.reader([text], skipinitialspace=True)):
        item = part.strip().strip("'\"")
        if item:
            out.append(item)
    return out


def _parse_bool(raw: str) -> bool:
    return (raw or "").strip().lower() in ("true", "yes", "1", "on")


def dump_md(p: Page) -> str:
    """Page → 文件字节（frontmatter + 正文 + 页尾脚注定义）。

    键序固定（已知键在前、extra 按 key 排序在后），可选键只在有值时写。引注写成
    Obsidian 原生脚注：正文 `[^c1]`，页尾 `[^c1]: 消息 12：“逐字引文”`。
    frontmatter 的值都是一行，标题/清单里的换行会被读成别的页——那由 store 的恒等闸
    兜住（写不回来就一个字都不落盘）。
    """
    lines = [
        "---",
        f"title: {scalar(p.title)}",
        f"created: {p.created}",
        f"updated: {p.updated}",
        f"type: {p.page_type}",
        f"tags: {_flow_list(p.tags)}",
    ]
    lines.append(f"sources: {_flow_list(p.sources)}")
    if p.confidence:
        lines.append(f"confidence: {p.confidence}")
    if p.contested:
        lines.append("contested: true")
    if p.contradictions:
        lines.append(f"contradictions: {_flow_list(p.contradictions)}")
    # 2.1.0 原生 frontmatter 之外不再写扩展键；extra 只用于迁移期读取。
    lines += ["---", "", p.body_md.rstrip()]
    return "\n".join(lines) + "\n"


def _frontmatter(lines: list[str]) -> tuple[dict[str, str], int]:
    """拆 frontmatter：返回 (键值, 闭合行下标)。缺头/没闭合抛 PageFormatError。"""
    if not lines or lines[0].strip() != "---":
        raise PageFormatError("缺 front matter 开头")
    meta: dict[str, str] = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return meta, i
        if ":" in line:
            key, _, value = line.partition(":")
            value = value.strip()
            if value.startswith('"'):
                try: value = json.loads(value)
                except ValueError: pass
            meta[key.strip()] = str(value)
    raise PageFormatError("front matter 没闭合")


def parse_md(slug: str, text: str, *, allow_legacy: bool = False) -> Page:
    """文件字节 → Page（双读：新脚注格式 + 旧 `[[cite:]]`/隐藏块）。

    allow_legacy=True 只给迁移用：认旧 type（topic/outline），topic→entity；
    旧页只有 title/type 时，created/updated/tags/sources 给空值，由迁移补。
    新写一律严格：type 不在 TYPES 就抛（store 的恒等闸会把它翻成一句人话）。
    """
    lines = text.splitlines()
    meta, end = _frontmatter(lines)
    title = meta.get("title", "")
    page_type = meta.get("type", "")
    if allow_legacy:
        if page_type == "topic":
            page_type = "entity"
        elif page_type == "summary":
            # 迁移工具会在移动文件时改 type；这里只让旧文件读得出来。
            page_type = "concept"
    if not title:
        raise PageFormatError("缺 title")
    if page_type not in TYPES and not (allow_legacy and page_type == "outline"):
        raise PageFormatError(f"type 不在白名单：{page_type}")

    # 2026-10-30：不再有句子级引注；正文就是 frontmatter 之后的所有行
    # （普通 Obsidian 脚注作为正文的一部分原样保留）。
    body = "\n".join(lines[end + 1:]).strip("\n")
    extra: dict[str, str] = {}
    for key, value in meta.items():
        if key not in KNOWN_FRONTMATTER and key:
            extra[key] = value
    return Page(
        slug=slug,
        title=title,
        page_type=page_type,
        body_md=body,
        created=meta.get("created", ""),
        updated=meta.get("updated", ""),
        tags=_parse_flow_list(meta.get("tags", "")),
        sources=_parse_flow_list(meta.get("sources", "")),
        confidence=meta.get("confidence", ""),
        contested=_parse_bool(meta.get("contested", "")),
        contradictions=_parse_flow_list(meta.get("contradictions", "")),
        extra=extra,
    )
