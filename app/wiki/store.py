"""wiki 的共享写路径与读侧。人和智能体都走 write_page，没有第二条路。

S5 起 wiki 是 Hermes 标准的物理 vault（`data/wiki/`，Obsidian 可直接打开）：
`SCHEMA.md` / `index.md` / `log.md` + `raw/` + `{entities,concepts,comparisons,queries,summaries}/`。
一页的 path 是 `{dir}/{slug}.md`，`type` 决定目录、创建后不可改（计划 §1）。

写路径是"先证后写"：

1. path/action/type 形状、create 撞车（裁定 R17）、乐观锁先过；
2. 恒等闸（裁定 R15）：这一页 dump 出去必须能原样 parse 回来再 dump 出同样的字节，
   否则一个字都不落盘；
3. 2026-10-30 起不再有句子级引注；页面契约只有 frontmatter + 正文 + wikilink + sources。
   它后面（裁定 R43）：磁盘上不存在"还没通过校验的正文"这样一个中间态；
4. 全对上了才落盘：页面 + 自动重写的 `index.md` + 追加的 `log.md`，三份**一次 commit**
   （计划 §2）；没有改动时 git 自己说没有（裁定 R16），记为 no-op —— 页面字节未变时
   不重写 index/log、不提交，但 FTS 照样按 head 重写一遍（裁定 R46）；
5. commit 段炸了（GitError / 子进程超时 / OSError）→ 三份文件都恢复成写之前的样子 +
   从暂存区撤下来（裁定 R45），不留"暂存了没提交"的残局。

repo.GitError 一律翻成中文，不出裸 traceback（裁定 R23）：写侧进 failed，
读侧抛 StoreError。ConflictError 是例外，它要往上冒到路由去换 409。
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import functools
import logging
import re
import sqlite3
import subprocess
import threading
import time
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path

from ..config import settings
from ..services import search
from ..wiki_config import get_config
from . import repo, serialize
from .provenance import content_hash, split_raw
from .quality import confidence_for

logger = logging.getLogger("openllmwiki.wiki")

# slug 的合法形状（2026-10-30 起允许中文）：本地 Hermes 的 vault 用中文文件名
# （`entities/中国新闻史.md`），线上要原样接收，不再强制转拼音。判据只保留"文件系统 +
# URL + Obsidian 都安全"这一条：
# - 不许路径分隔符 `/\`、Windows 保留字符 `:*?"<>|`、Obsidian 链接语法字符 `[]#^`；
# - 不许任何空白/控制字符；
# - 不许前导 `-`（裁定 R83，git 命令行会当选项）、前导/末尾 `.`（隐藏文件、`..`）；
# - 长度 80 字：中文文件名 3 字节/字，80 字离 255 字节的文件名上限还有余量。
SLUG_MAX_CHARS = 80
SLUG_FORBIDDEN = frozenset('/\\:*?"<>|[]#^')

# 页面 path 的唯一合法形状（相对 vault）：{dir}/{slug}.md。目录名只认 type 映射出来的这五个。
DIR_TO_TYPE = {directory: page_type for page_type, directory in serialize.DIRS.items()}
_PAGE_DIRS = frozenset(serialize.DIRS.values())

SCHEMA_FILE = "SCHEMA.md"
INDEX_FILE = "index.md"
LOG_FILE = "log.md"
LOG_HEADER = "# 行动日志\n\n"

# 2026-11-13：index/SCHEMA 是生成物，留在磁盘给 UI/模型读，但不进 git。
# 两个 writer 每次 pull 都在生成物上做文本合并，必然漂移；git 只该管页面和 log。
GENERATED_FILES = (SCHEMA_FILE, INDEX_FILE)
GENERATED_COMMIT_SUBJECT = "wiki|edit|_vault_|system|index/SCHEMA 改为生成物"
GITIGNORE_LINES = (
    ".DS_Store",
    "index.md",
    "SCHEMA.md",
)
GITATTRIBUTES_LINES = (
    "log.md merge=union",
    "log-*.md merge=union",
)

# 裁定 R47：提交主题里的 action 字段只认这几个值（S9 加 delete：删页也是一次提交）。
ACTIONS = ("create", "edit", "revert", "delete")

# 裁定 R17：create 撞上已存在的页时给的那句人话（cli seed / 路由 / 工具都认这个常量）。
ALREADY_EXISTS = "这一页已存在"

# 按 path 一把锁。用 threading.Lock 而不是 asyncio.Lock，两个理由：
# 1) 锁在 write_page_sync 里面，同步入口（cli、上传摄入、测试）也一起串起来；
# 2) asyncio.Lock 会绑死创建它的那个事件循环（TestClient 每个请求一个新循环会炸）。
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

# path=None（create-intent，裁定 R91）的分配互斥锁，与 per-path 锁是两把。
# 两个并发的 "判冲突 → 发 path → 落盘" 若不串起来，会交错发出同一个 slug：
# new_slug 的判重只看磁盘，后落盘的按 action=None → path.exists() 判成一次 edit，
# 把赢家的页连标题一起改掉（两次都 ok=True，一页无声消失）。锁域盖到落盘为止。
# 锁序 _CREATE_LOCK → per-path 锁 → repo._INDEX_LOCK，全项目只在这个方向上取，无环。
_CREATE_LOCK = threading.Lock()


class ConflictError(Exception):
    """你交上来的 base_commit 不是这一页的现行版本：有人先改了。"""

    def __init__(self, diff_text: str, reason: str = "有人先改了这一页"):
        super().__init__(reason)
        self.diff_text = diff_text
        self.reason = reason


class StoreError(RuntimeError):
    """wiki 库自己出了问题（git 跑不通、data/wiki 被删）。消息是给人看的中文。"""


def _serialized_write(fn):
    """把一次"改 data/wiki"的操作放进跨进程写锁里，并拒绝在 rebase/merge 半途写。

    2026-10-30：同步器是独立进程，store 原有的 per-path / _CREATE_LOCK / _INDEX_LOCK
    都只是进程内的锁。这个装饰器补上跨进程那一层（repo.wiki_write_lock），顺带守住
    "版本库卡在 rebase 时不许写页"——那种状态下 commit 会落到 detached HEAD，人工
    abort 之后页就没了（旧双向同步的真实故障模式）。

    可重入：revert_sync 会调 write_page_sync，ensure_vault 会调 rebuild_index，
    同一线程再进一层不会重复抢 flock（repo.wiki_write_lock 内部按线程深度实现）。
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with repo.wiki_write_lock():
            if repo.rebase_in_progress():
                raise StoreError(
                    "版本库正卡在 rebase/merge 中途，先处理完再写："
                    "在 data/wiki 里执行 git status 看冲突文件，"
                    "git rebase --abort 或 --continue 之后重试"
                )
            return fn(*args, **kwargs)
    return wrapper


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return date.today().isoformat()


_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _date_or(value: str, fallback: str) -> str:
    text = (value or "").strip()
    return text if _DATE_RE.fullmatch(text) else fallback


# --- path 形状 ---------------------------------------------------------------


def valid_slug(slug) -> bool:
    """slug 只能是我们认的那种形状。**这是全项目唯一的那个判据。**

    写侧靠它拦住路径拼接与 git pathspec 注入；读侧也要拦：HTTP 路径参数 starlette
    匹配之后才 unquote，`..%2F..%2F` 一类能变成 `../../x`。search.reindex_all 也用
    同一个判据（裁定 R48）：进不了目录的页绝不能进 FTS。
    2026-10-30 起允许中文/Unicode 文件名（本地 Hermes vault 的页面就用中文命名），
    只拦路径分隔符、控制字符、空白与文件系统/Obsidian 的保留字符。
    """
    if not isinstance(slug, str) or not slug:
        return False
    if len(slug) > SLUG_MAX_CHARS:
        return False
    if slug in (".", "..") or "." in slug:
        return False
    if slug[0] in "-." or slug[-1] == ".":
        return False
    for ch in slug:
        if ch in SLUG_FORBIDDEN or ch.isspace():
            return False
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            return False
    return True


def _split_page_path(path) -> tuple[str, str] | None:
    """`{dir}/{slug}.md` → (目录, slug)；形状不对返 None（不做磁盘检查）。"""
    if not isinstance(path, str):
        return None
    parts = path.split("/")
    if len(parts) != 2:
        return None
    directory, filename = parts
    if directory not in _PAGE_DIRS or not filename.endswith(".md"):
        return None
    return directory, filename[:-3]


def valid_page_path(path) -> bool:
    """是不是一页的合法相对 path（`{dir}/{slug}.md`）。特殊文件、raw、坏名字都 False。"""
    parts = _split_page_path(path)
    return parts is not None and valid_slug(parts[1])


def page_type_of(path: str) -> str | None:
    parts = _split_page_path(path)
    if parts is None or not valid_slug(parts[1]):
        return None
    return DIR_TO_TYPE[parts[0]]


def slug_of(path: str) -> str | None:
    parts = _split_page_path(path)
    if parts is None or not valid_slug(parts[1]):
        return None
    return parts[1]


def _canonical_write_path(path: str) -> str | None:
    """写侧 path 归一：`entities/x`、`entities/x.md` 都认；形状不对返 None。

    与读侧 canonical_path 的差别：这里**不查磁盘**（create 的 path 本来就不存在）。
    """
    if not isinstance(path, str):
        return None
    text = path.strip().lstrip("/")
    if not text:
        return None
    if not text.endswith(".md"):
        text += ".md"
    return text if valid_page_path(text) else None


def canonical_path(path: str) -> str | None:
    """读侧 path 归一：带/不带 .md 都认，**也兼容旧裸 slug**（`baoming` → 扫五个目录）。

    slug 全库唯一（计划 §1），所以裸 slug 最多命中一页；一个都不命中返 None
    （调用方按"没有这一页"处置）。形状不合法一律 None，绝不让 `..` 拼出去。
    """
    if not isinstance(path, str):
        return None
    text = path.strip().lstrip("/")
    if not text:
        return None
    if "/" in text:
        if not text.endswith(".md"):
            text += ".md"
        return text if valid_page_path(text) else None
    slug = text[:-3] if text.endswith(".md") else text
    if not valid_slug(slug):
        return None
    for page_type in serialize.TYPES:
        rel = serialize.path_for(slug, page_type)
        if (settings.wiki_dir / rel).exists():
            return rel
    return None


def _path(rel: str) -> Path:
    return settings.wiki_dir / rel


def _ensure_dirs() -> None:
    """vault 目录结构不在就先建出来（页面写入的第一道准备；ensure_vault 也调它）。

    目录不在时 repo 的 subprocess(cwd=...) 会 FileNotFoundError（裸 traceback）；
    建库（git init + 写身份）仍然是 repo.ensure_repo() 的活。
    """
    settings.wiki_dir.mkdir(parents=True, exist_ok=True)
    for directory in serialize.DIRS.values():
        (settings.wiki_dir / directory).mkdir(parents=True, exist_ok=True)
    (settings.wiki_dir / serialize.RAW_UPLOADS_DIR).mkdir(parents=True, exist_ok=True)


def page_files() -> list[str]:
    """五个页面目录里的所有 `*.md`（相对 vault，按 path 排序）。坏文件也算进来。

    lint 需要看见"读不回来的文件"（frontmatter 报错），而 all_pages 会把它们跳过，
    所以这里只列文件、不 parse。
    """
    out: list[str] = []
    for directory in serialize.DIRS.values():
        base = settings.wiki_dir / directory
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.md")):
            out.append(f"{directory}/{path.name}")
    return out


def _parse_file(rel: str) -> serialize.Page | None:
    """读一个页面文件成 Page；读不出来或形状不对都返 None（裁定 R21）。

    ValueError 那一支：PageFormatError 是它的子类，坏字节的 UnicodeDecodeError 也是。
    OSError 那一支：`evil.md/` 目录、chmod 000、glob 与 read 之间被删的竞态。
    读侧与写侧都要它不出裸异常（_title_owner 在 _CREATE_LOCK 内扫全目录）。
    """
    slug = slug_of(rel)
    if slug is None:
        return None
    try:
        return serialize.parse_md(slug, _path(rel).read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


_PAGE_CACHE_LOCK = threading.Lock()
_PAGE_CACHE: dict = {}


def all_pages() -> dict[str, serialize.Page]:
    """Cache parsed files by filesystem identity; external sync invalidates without a TTL."""
    with _PAGE_CACHE_LOCK:
        root = str(settings.wiki_dir.resolve())
        if _PAGE_CACHE.get("root") != root:
            _PAGE_CACHE.clear()
            _PAGE_CACHE.update(root=root, files={})
        cached = _PAGE_CACHE["files"]
        result = {}
        current = set(page_files())
        for removed in set(cached) - current:
            cached.pop(removed, None)
        for rel in sorted(current):
            try:
                stat = _path(rel).stat()
                signature = (stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
            except OSError:
                continue
            old = cached.get(rel)
            if old is None or old[0] != signature:
                page = _parse_file(rel)
                cached[rel] = (signature, page)
            page = cached[rel][1]
            if page is not None:
                result[rel] = copy.deepcopy(page)
        return result


def _lock_for(rel: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(rel, threading.Lock())


def _git_read(what: str, fn, *args, **kwargs):
    """读侧的 git 调用出口：GitError 翻成 StoreError 的中文（裁定 R23）。"""
    try:
        return fn(*args, **kwargs)
    except repo.GitError as exc:
        raise StoreError(f"{what}没读出来：{exc}") from exc


# --- wikilink ---------------------------------------------------------------

# 正文里的页面互链（计划 §1）：[[slug]] 或 [[slug|显示文字]]。
WIKILINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")


def iter_wikilinks(body_md: str):
    """正文里的页面链接：逐条 yield (target, label)。引注角标跳过。"""
    for hit in WIKILINK_RE.finditer(body_md or ""):
        raw = hit.group(1)
        target, _, label = raw.partition("|")
        target = target.strip()
        if not target or target.startswith("cite:"):
            continue
        yield target, label.strip() or target


# 标题链接归一化的噪声字符：空白、书名号/引号/括号、常见中英标点与分隔符。
# 目的只有一个——容忍"标题里少打一个冒号/空格"造成的假断链，不改变精确优先的判据。
_TITLE_NOISE_RE = re.compile(r"""[\s《》〈〉「」『』（）()【】\[\]{}<>:：;；,，.。!！?？、·\-—_/\\|"'－～~]+""")


def _title_key(title: str) -> str:
    """标题的宽松键：去噪声字符 + casefold。空串表示这个标题不参与宽松解析。"""
    return _TITLE_NOISE_RE.sub("", title or "").casefold()


def link_index(pages: dict[str, serialize.Page]) -> tuple[dict[str, str], dict[str, str]]:
    """(slug→path, title→path)。slug 精确优先，title 其次（计划 §2 的解析顺序）。"""
    by_slug: dict[str, str] = {}
    by_title: dict[str, str] = {}
    for rel, page in pages.items():
        by_slug.setdefault(page.slug, rel)
        by_title.setdefault(page.title, rel)
    return by_slug, by_title


def _title_key_index(pages: dict[str, serialize.Page]) -> dict[str, str]:
    """标题宽松索引（title 归一化 → path）。两个标题归一化后撞车就整组丢弃：
    宁可让 lint 报断链，也不要把链接悄悄指到错的那一页。"""
    out: dict[str, str] = {}
    ambiguous: set[str] = set()
    for rel, page in pages.items():
        key = _title_key(page.title)
        if not key:
            continue
        if key in out:
            ambiguous.add(key)
        else:
            out[key] = rel
    for key in ambiguous:
        out.pop(key, None)
    return out


def link_resolver(pages: dict[str, serialize.Page]):
    by_slug, by_title = link_index(pages)
    loose = _title_key_index(pages)
    return lambda target: by_slug.get(target) or by_title.get(target) or loose.get(_title_key(target))


def link_graph(pages: dict[str, serialize.Page]) -> tuple[dict, dict]:
    resolve = link_resolver(pages)
    links = {}
    incoming = {rel: [] for rel in pages}
    for rel, page in pages.items():
        links[rel] = [{"target": target, "label": label, "path": resolve(target)}
                      for target, label in iter_wikilinks(page.body_md)]
        for target in {link["path"] for link in links[rel]} - {None, rel}:
            incoming[target].append({"path": rel, "title": page.title})
    return links, {rel: sorted(items, key=lambda item: item["path"])
                   for rel, items in incoming.items()}


def resolve_link(target: str, pages: dict[str, serialize.Page]) -> str | None:
    """解析顺序：slug 精确 → 标题精确 → 标题宽松（去空格/标点、大小写不敏感）。

    第三层是给"目标页本轮才建、写正文时还不知道服务端发什么 slug"的工序兜底的：
    模型可以写 `[[用户画像]]`，标题允许差一个冒号/空格；归一化后撞车的一律不解析
    （宁可报断链也不指错页）。
    """
    by_slug, by_title = link_index(pages)
    return (
        by_slug.get(target)
        or by_title.get(target)
        or _title_key_index(pages).get(_title_key(target))
    )



def wikilinks_of(page: serialize.Page,
                 pages: dict[str, serialize.Page] | None = None) -> list[dict]:
    """一页正文里的链接摊成 [{target,label,path|null}]；解析不到 path 给 null（lint 报断链）。"""
    pages = pages if pages is not None else all_pages()
    return [
        {"target": target, "label": label, "path": resolve_link(target, pages)}
        for target, label in iter_wikilinks(page.body_md)
    ]


def backlinks_of(rel: str, pages: dict[str, serialize.Page] | None = None) -> list[dict]:
    """扫全库入链，返 [{path,title}]（按 path 排序）。页数少，直接扫；后续可优化。"""
    pages = pages if pages is not None else all_pages()
    return link_graph(pages)[1].get(rel, [])


# --- 读侧 ---

# 目录摘要（llm-wiki 索引风格）的上限：一眼能扫完一行，不是让人在目录里读正文。
SUMMARY_MAX = 120
# 脚注角标从摘要里摘掉：它是正文里的锚点，混进目录只是噪音。
_FOOTNOTE_MARK_RE = re.compile(r"\[\^[A-Za-z0-9_-]+\]")
# 行首的 markdown 标题井号（# ~ ######）。只去行首那一段，正文中间的 # 原样留着。
_HEADING_RE = re.compile(r"^#{1,6}[ \t]*")


_WIKILINK_RE = re.compile(r"\[\[([^\[\]|]+)(?:\|([^\[\]]*))?\]\]")


def summary_of(body_md: str) -> str:
    """一页正文的目录摘要：第一段非空行 → 去行首 # → 去角标 → wikilink 转纯文本 →
    压空白 → 安全截断（超过 SUMMARY_MAX 加省略号）。

    2026-10-30 修：旧实现直接 `[:80]`，会在 `[[2016-...` 中间切断，未闭合的链接
    会把 index 下一行吞掉（Hermes 报的"index 缺 1 页/鬼 1 条"就是它）。摘要不承载
    链接功能，所以先把 `[[slug|标题]]` 转成"标题"、`[[slug]]` 转成 slug，再截断。
    """
    for line in (body_md or "").splitlines():
        text = line.strip()
        if not text:
            continue
        text = _HEADING_RE.sub("", text)
        text = _FOOTNOTE_MARK_RE.sub("", text)
        text = _WIKILINK_RE.sub(lambda m: (m.group(2) or m.group(1)).strip(), text)
        text = " ".join(text.split())
        if len(text) > SUMMARY_MAX:
            text = text[: SUMMARY_MAX - 1].rstrip() + "…"
        return text
    return ""


def list_pages(conn: sqlite3.Connection) -> list[dict]:
    """目录：每页的 path/slug/标题/类型/tags/摘要/创建与更新时间/改的人，按新到旧。

    摘要从已经 parse 好的 Page.body_md 上顺算（summary_of），不额外读盘。
    updated_at 取 frontmatter 的 `updated`（日期，计划 §1），actor 与并列时的次序取
    这一页最近一条提交：一次 git log 拿回整张 path→最近提交 的映射，而不是每页跑
    一趟子进程（repo.latest_touches 的说明里写了为什么这样等价）。空仓库返 None。
    """
    pages = all_pages()
    latest = _git_read("目录", repo.latest_touches) if pages else {}
    out: list[dict] = []
    for rel, page in pages.items():
        entry = latest.get(rel)
        out.append({
            "path": rel,
            "slug": page.slug,
            "title": page.title,
            "page_type": page.page_type,
            "tags": list(page.tags),
            "summary": summary_of(page.body_md),
            "created_at": page.created or None,
            "updated_at": page.updated or None,
            "actor": entry.actor if entry else None,
            **({"status": "draft"} if page.extra.get("status") == "draft" else {}),
            "_at": entry.at if entry else "",
        })
    out.sort(key=lambda item: (item["updated_at"] or "", item["_at"]), reverse=True)
    for item in out:
        item.pop("_at", None)
    return out


def get_page(conn, path: str) -> dict | None:
    """一页的全文 + wikilinks/backlinks + frontmatter 可选键。

    2026-10-30 起不再有句子级引注；来源是 frontmatter 的 `sources`（页级）。
    path 接受带/不带 .md 与旧裸 slug；形状不合法、读不回来、文件不在都返 None（404）。
    """
    rel = canonical_path(path)
    if rel is None or not _path(rel).exists():
        return None
    page = _parse_file(rel)
    if page is None:
        return None
    head = _git_read("这一页", repo.head_sha, rel)
    pages = all_pages()
    links, incoming = link_graph(pages)
    return {
        "path": rel,
        "slug": page.slug,
        "title": page.title,
        "page_type": page.page_type,
        "body_md": page.body_md,
        "head": head,
        "tags": list(page.tags),
        "sources": list(page.sources),
        "created_at": page.created or None,
        "updated_at": page.updated or None,
        "confidence": page.confidence or None,
        "contested": page.contested,
        "contradictions": list(page.contradictions),
        "metadata": dict(page.extra),
        # 历史兼容：引注功能已删，恒为空数组（前端/旧脚本读不到字段会炸）
        "citations": [],
        "wikilinks": links.get(rel, []),
        "backlinks": incoming.get(rel, []),
    }


def versions(path: str) -> list[dict]:
    """一页的提交记录，新到旧。path 可以是旧裸 slug；形状不合法就是没有记录。"""
    rel = canonical_path(path)
    if rel is None:
        return []
    return [asdict(entry) for entry in _git_read("版本记录", repo.log, rel)]


def page_diff(path: str, a: str, b: str) -> str:
    """两个版本之间这一页的 unified diff。a/b 必须是 log()/head_sha() 给出的 sha。"""
    rel = canonical_path(path)
    if rel is None:
        raise ValueError(f"path 不合法：{path!r}")
    return _git_read("改前改后的对比", repo.diff, a, b, rel)


# --- 特殊文件（SCHEMA / index / log / raw） ---------------------------------

_SPECIAL_ALIASES = {
    "schema": SCHEMA_FILE, "schema.md": SCHEMA_FILE,
    "index": INDEX_FILE, "index.md": INDEX_FILE,
    "log": LOG_FILE, "log.md": LOG_FILE,
}

def _schema_text() -> str:
    """SCHEMA.md 由 wiki.toml 生成（domain / conventions / tags）。"""
    return get_config().render_schema()



def read_special(name: str) -> dict | None:
    """读 SCHEMA.md / index.md / log.md：{path, body_md, updated_at}；没有就 None。

    name 认 `index`/`index.md`/`SCHEMA.md` 一类写法（大小写不敏感）。updated_at 取
    文件 mtime（特殊文件没有 frontmatter）。
    """
    key = (name or "").strip().lower()
    rel = _SPECIAL_ALIASES.get(key)
    if rel is None:
        return None
    path = _path(rel)
    if not path.is_file():
        return None
    try:
        body = path.read_text(encoding="utf-8")
        updated = datetime.fromtimestamp(
            path.stat().st_mtime, timezone.utc
        ).isoformat(timespec="seconds")
    except (OSError, UnicodeDecodeError):
        return None
    return {"path": rel, "body_md": body, "updated_at": updated}


def read_raw(rel: str) -> str | None:
    """读 raw 层的原文（`raw/uploads/{id}-{safe}.md`）；越界/不存在/不是 raw 一律 None。"""
    text = (rel or "").strip().lstrip("/")
    if not text.startswith(serialize.RAW_DIR + "/") or ".." in Path(text).parts:
        return None
    base = settings.wiki_dir.resolve()
    target = (settings.wiki_dir / text).resolve()
    if not target.is_relative_to(base):
        return None
    try:
        return target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _append_missing_lines(path: Path, lines: tuple[str, ...]) -> bool:
    """把缺失的行追加到文件末尾；文件原有的内容一行不动。返回是否写过。"""
    current = _read_text(path) or ""
    present = set(current.splitlines())
    missing = [line for line in lines if line not in present]
    if not missing:
        return False
    if current and not current.endswith("\n"):
        current += "\n"
    path.write_text(current + "\n".join(missing) + "\n", encoding="utf-8")
    return True


def _index_text() -> str:
    """自动生成的 index.md：标题、最后更新时间、总页数，然后按 type 分节。"""
    config = get_config()
    pages = all_pages()
    updated = max((page.updated for page in pages.values() if page.updated), default="")
    lines = [
        f"# {config.site_name}索引",
        "",
        f"最后更新：{updated or '—'}",
        f"总页数：{sum(p.extra.get('status') != 'draft' for p in pages.values())}",
        *(["[按主题浏览](/wiki/source?path=_meta/topic-map.md)"] if len(pages) > 200 else []),
    ]
    labels = config.type_labels
    for page_type in serialize.TYPES:
        group = sorted(
            ((rel, page) for rel, page in pages.items() if page.page_type == page_type and page.extra.get("status") != "draft"),
            key=lambda item: item[1].slug,
        )
        if not group:
            continue
        lines += ["", f"## {labels.get(page_type, page_type)}（{len(group)}）"]
        for _rel, page in group:
            # Obsidian 别名写法：文件名仍是 slug，看到的是标题（中文标题的 slug 是
            # page-日期，直接摆出来对人不友好）。
            entry = f"- [[{page.slug}|{page.title}]]"
            summary = summary_of(page.body_md)
            if summary:
                entry += f" — {summary}"
            if page.tags:
                entry += f"（{'、'.join(page.tags)}）"
            lines.append(entry)
    return "\n".join(lines) + "\n"


def write_schema() -> str:
    """把 SCHEMA.md 按当前 wiki.toml 重写到磁盘（生成物，不 commit）。

    本地同步脚本用它刷新本地 SCHEMA：这个文件不跟踪 Git，但原装 Hermes 技能会读它
    来判断 index/log 的维护约定，所以它必须跟服务端生成的一致。
    """
    _ensure_dirs()
    text = _schema_text()
    path = _path(SCHEMA_FILE)
    if _read_text(path) != text:
        path.write_text(text, encoding="utf-8")
    return text


@_serialized_write
def rebuild_index(conn=None) -> str:
    """重写 index.md 并返回正文（不提交；调用方负责 commit）。

    ensure_vault 与写路径都走它：index 是派生物，任何时候都能从页面重建。
    """
    _ensure_dirs()
    text = _index_text()
    _path(INDEX_FILE).write_text(text, encoding="utf-8")
    return text


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def log_block(action: str, subject: str, path: str, actor: str) -> str:
    """log.md 的一条（计划 §2）：`## [日期] action | subject` + path/actor 两行。"""
    def one_line(value: str) -> str:
        return " ".join(str(value or "").split())

    return (
        f"## [{_today()}] {one_line(action)} | {one_line(subject)}\n"
        f"- path: {one_line(path)}\n"
        f"- actor: {one_line(actor)}\n"
    )


def append_log(action: str, subject: str, path: str, actor: str) -> None:
    """往 log.md 追加一条（追加式，永不改写历史条目；文件不在就先建）。"""
    _ensure_dirs()
    log_path = _path(LOG_FILE)
    current = _read_text(log_path) or LOG_HEADER
    if current and not current.endswith("\n"):
        current += "\n"
    log_path.write_text(current + log_block(action, subject, path, actor), encoding="utf-8")


@_serialized_write
def ensure_vault(conn=None) -> dict:
    """幂等地把 vault 准备好：目录 + 旧布局迁移 + SCHEMA/index/log，缺什么补什么。

    lifespan 与 cli seed 都调它。顺序是有讲究的：先迁移（旧平文件按 type 搬进目录），
    再补特殊文件，最后用**一次 commit** 把这一轮的所有变化提交（计划 §7 Step 5）；
    已经是新布局时一个文件都不动、也不产生提交。

    返回 {"created":[...], "changed":[...], "migrated": {...}, "changed_any": bool}。
    """
    from . import migrate as migrate_module  # 懒导入：migrate 反过来也要 store（避免环）

    _ensure_dirs()
    repo.ensure_repo()
    migration = migrate_module.migrate(conn, commit=False)
    changed = list(migration["changed"])
    created: list[str] = []

    log_path = _path(LOG_FILE)
    if not log_path.exists():
        log_path.write_text(LOG_HEADER, encoding="utf-8")
        changed.append(LOG_FILE)
        created.append(LOG_FILE)

    # SCHEMA.md / index.md 是生成物：每次启动按配置/页面重写，但**不进 commit**。
    schema_path = _path(SCHEMA_FILE)
    schema_existed = schema_path.exists()
    schema_text = _schema_text()
    if _read_text(schema_path) != schema_text:
        schema_path.write_text(schema_text, encoding="utf-8")
        if not schema_existed:
            created.append(SCHEMA_FILE)

    index_path = _path(INDEX_FILE)
    index_existed = index_path.exists()
    index_text = _index_text()
    if _read_text(index_path) != index_text:
        index_path.write_text(index_text, encoding="utf-8")
        if not index_existed:
            created.append(INDEX_FILE)

    # 仓库级 .gitignore/.gitattributes：生成物不进 git；log 追加用 union 合并。
    for name, lines in (
        (".gitignore", GITIGNORE_LINES),
        (".gitattributes", GITATTRIBUTES_LINES),
    ):
        try:
            if _append_missing_lines(_path(name), lines) and name not in changed:
                changed.append(name)
        except OSError as exc:
            raise StoreError(f"{name} 写不下去：{exc}") from exc

    if changed:
        subject = (
            migrate_module.MIGRATION_SUBJECT
            if migration["changed_any"]
            else "wiki|edit|_vault_|system|初始化 LLM-Wiki vault"
        )
        try:
            repo.commit(changed, subject)
        except (repo.GitError, subprocess.TimeoutExpired, OSError) as exc:
            raise StoreError(f"vault 没初始化成：{exc}") from exc

    # 旧 vault 里 index/SCHEMA 曾被跟踪：这里做一次迁移，把它们从索引里移除，
    # 文件留在磁盘。新 vault 没有 tracked 的生成物，untrack 是 no-op。
    try:
        untracked = repo.untrack(GENERATED_FILES, GENERATED_COMMIT_SUBJECT)
    except (repo.GitError, subprocess.TimeoutExpired, OSError) as exc:
        raise StoreError(f"生成物从版本库移除失败：{exc}") from exc
    return {
        "created": created,
        "changed": changed,
        "migrated": migration,
        "untracked": untracked,
        "changed_any": bool(changed),
    }


def _safe_raw_name(filename: str, limit: int = 60) -> str:
    """raw 文件名里的 safe-name：只留 ASCII 字母数字 `.` `_` `-`，其余压成 `-`。"""
    base = Path(str(filename or "")).name
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-._")
    return (cleaned or "upload")[:limit]


def raw_path_for(upload_id: int, filename: str) -> str:
    """raw md 的相对 path：`raw/uploads/{upload_id}-{safe-name}.md`（计划 §1/§3）。"""
    return f"{serialize.RAW_UPLOADS_DIR}/{int(upload_id)}-{_safe_raw_name(filename)}.md"


@_serialized_write
def ingest_raw_upload(conn, upload_id: int, filename: str, text: str, actor: str) -> dict:
    """上传解析成功后把原文归进 raw 层：写不可变 md + 追加 log + 一次 commit（计划 §3）。

    返回 {"ok","path","sha","noop","created","failed"}。同一份上传重复摄入是幂等的
    （文件已在就返回 noop，不重复写 log、不产生第二个 commit）。解析文本为空时
    不生成 raw（failed，调用方据此不回 raw_path）。
    """
    _ensure_dirs()
    if not (text or "").strip():
        return {
            "ok": False, "path": None, "sha": None, "noop": False, "created": False,
            "failed": [{"key": "", "quote": "", "note": "这份材料没有解析文本，不生成 raw"}],
        }
    try:
        upload_id = int(upload_id)
    except (TypeError, ValueError):
        return {
            "ok": False, "path": None, "sha": None, "noop": False, "created": False,
            "failed": [{"key": "", "quote": "", "note": "上传编号不是数字，不生成 raw"}],
        }
    rel = raw_path_for(upload_id, filename)
    path = _path(rel)
    if path.exists():
        return {
            "ok": True, "path": rel, "noop": True, "created": False,
            "sha": _git_read("raw", repo.head_sha, rel), "failed": [],
        }

    row = conn.execute(
        "SELECT sha256, source_url FROM upload_files WHERE id = ?", (upload_id,)
    ).fetchone()
    sha256 = row["sha256"] if row else ""
    source_file = " ".join(str(filename or "").split()) or "未命名材料"
    front = [
        "---",
        f"source_file: {serialize.scalar(source_file)}",
        f"upload_id: {upload_id}",
        f"sha256: {content_hash((text or '').rstrip() + chr(10))}",
        f"content_sha256: {content_hash((text or '').rstrip() + chr(10))}",
        f"original_sha256: {sha256}",
        f"source_url: {row['source_url'] or '' if row else ''}",
        f"ingested: {_today()}",
        "---",
        "",
        (text or "").rstrip(),
    ]
    content = "\n".join(front) + "\n"

    log_path = _path(LOG_FILE)
    page_snapshot = _snapshot(path)
    log_snapshot = _snapshot(log_path)
    try:
        path.write_text(content, encoding="utf-8")
        current_log = _read_text(log_path) or LOG_HEADER
        if current_log and not current_log.endswith("\n"):
            current_log += "\n"
        log_path.write_text(
            current_log + log_block("ingest", source_file, rel, actor), encoding="utf-8"
        )
    except OSError as exc:
        _restore(path, page_snapshot)
        _restore(log_path, log_snapshot)
        return {
            "ok": False, "path": rel, "sha": None, "noop": False, "created": False,
            "failed": [{"key": "", "quote": "", "note": f"raw 写不下去，这份材料没归档：{exc}"}],
        }

    subject = f"raw|ingest|{upload_id}|{_subject_actor(actor)}|{_subject_note(source_file)}"
    try:
        sha = repo.commit([rel, LOG_FILE], subject)
    except (repo.GitError, subprocess.TimeoutExpired, OSError) as exc:
        repo.unstage([rel, LOG_FILE])
        _restore(path, page_snapshot)
        _restore(log_path, log_snapshot)
        return {
            "ok": False, "path": rel, "sha": None, "noop": False, "created": False,
            "failed": [{"key": "", "quote": "", "note": f"版本库没跑通，raw 没归档：{exc}"}],
        }
    return {"ok": True, "path": rel, "sha": sha, "noop": sha is None, "created": True,
            "failed": []}


@_serialized_write
def refresh_raw_upload(conn, upload_id: int, filename: str, text: str, actor: str) -> dict:
    """重新解析后刷新 raw 正文（2026-10-30 图片补录用），保留 frontmatter 与 sha256。

    raw 的常规口径是"不可变归档"，但补录内嵌图片描述属于**同一次摄入的正文更新**：
    sha256 仍指原件（没变），frontmatter 原样保留，只换正文。文件还在就更新并提交
    `raw|update|…`；不在就退回 ingest_raw_upload（新归档）。
    """
    if not (text or "").strip():
        return {
            "ok": False, "path": None, "sha": None, "noop": False, "created": False,
            "failed": [{"key": "", "quote": "", "note": "这份材料没有解析文本，不生成 raw"}],
        }
    rel = raw_path_for(upload_id, filename)
    path = _path(rel)
    if not path.exists():
        return ingest_raw_upload(conn, upload_id, filename, text, actor)
    try:
        old = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "ok": False, "path": rel, "sha": None, "noop": False, "created": False,
            "failed": [{"key": "", "quote": "", "note": f"raw 读不回来：{exc}"}],
        }
    lines = old.splitlines(keepends=True)
    front_end = -1
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                front_end = index
                break
    meta, _ = split_raw(old)
    body = (text or "").rstrip() + "\n"
    meta.setdefault("original_sha256", meta.get("sha256", ""))
    meta["sha256"] = meta["content_sha256"] = content_hash(body)
    front = "\n".join(["---", *(f"{k}: {serialize.scalar(v)}" for k, v in meta.items()), "---"])
    new_content = front + "\n\n" + body
    if new_content == old:
        return {
            "ok": True, "path": rel, "sha": _git_read("raw", repo.head_sha, rel),
            "noop": True, "created": False, "failed": [],
        }

    log_path = _path(LOG_FILE)
    page_snapshot = _snapshot(path)
    log_snapshot = _snapshot(log_path)
    source_file = " ".join(str(filename or "").split()) or "未命名材料"
    try:
        path.write_text(new_content, encoding="utf-8")
        current_log = _read_text(log_path) or LOG_HEADER
        if current_log and not current_log.endswith("\n"):
            current_log += "\n"
        log_path.write_text(
            current_log + log_block("update", source_file, rel, actor), encoding="utf-8"
        )
    except OSError as exc:
        _restore(path, page_snapshot)
        _restore(log_path, log_snapshot)
        return {
            "ok": False, "path": rel, "sha": None, "noop": False, "created": False,
            "failed": [{"key": "", "quote": "", "note": f"raw 更新写不下去：{exc}"}],
        }

    subject = f"raw|update|{int(upload_id)}|{_subject_actor(actor)}|{_subject_note(source_file + '（补录图片描述）')}"
    try:
        sha = repo.commit([rel, LOG_FILE], subject)
    except (repo.GitError, subprocess.TimeoutExpired, OSError) as exc:
        repo.unstage([rel, LOG_FILE])
        _restore(path, page_snapshot)
        _restore(log_path, log_snapshot)
        return {
            "ok": False, "path": rel, "sha": None, "noop": False, "created": False,
            "failed": [{"key": "", "quote": "", "note": f"版本库没跑通，raw 没更新：{exc}"}],
        }
    return {"ok": True, "path": rel, "sha": sha, "noop": sha is None, "created": True,
            "failed": []}


def lint(conn=None) -> dict:
    """vault lint（计划 §2 的十项 + §3 的 raw sha256 漂移）：{findings, summary}。只读，不写文件。"""
    from . import lint as lint_module  # 懒导入：lint 反过来 import store（避免环）

    return lint_module.lint(conn)


# --- 写侧 ---


def _failed(note: str, key: str = "", quote: str = "") -> dict:
    """写不成的统一形状。note 是给人看的中文。"""
    return {
        "ok": False, "sha": None, "noop": False,
        "failed": [{"key": key, "quote": quote, "note": note}],
    }


def _restore(path: Path, prev: bytes | None) -> None:
    """把文件恢复成写之前的样子：prev 是 None 就是本来没有这个文件，删掉。

    恢复不了（只读挂载、磁盘满、权限）就抛 StoreError：这时工作区里可能留着一份
    没提交的正文，必须让人知道去手工收拾，不能静默吞掉、也不出裸 traceback。
    """
    try:
        if prev is None:
            # 只删"我们刚写出来的普通文件"：一个名叫 x.md 的目录 / 断链 symlink 本来
            # 就不是我们写的，unlink 它们只会把别的错因盖成 StoreError。
            if path.is_file() or path.is_symlink():
                path.unlink(missing_ok=True)
        else:
            path.write_bytes(prev)
    except OSError as exc:
        raise StoreError(f"回滚没做成，{path.name} 可能留着没提交的正文，请手工检查：{exc}") from exc


def _snapshot(path: Path) -> bytes | None:
    """写之前磁盘上的字节；文件不在（或读不出来）返 None，那么"恢复"就是删掉。

    取磁盘而不是取 git 的 head 版本：会有**没提交过**的文件（上一次写崩在落盘与
    commit 之间、运维手工放进来的页）。按 head 回滚时这类文件会被**删掉**，是丢数据。
    """
    try:
        return path.read_bytes()
    except OSError:
        return None


def _subject_action(action) -> str:
    """提交主题里的 action 字段：白名单三值（裁定 R47），白名单外抛 ValueError。"""
    value = str(action if action is not None else "")
    if value not in ACTIONS:
        raise ValueError(f"action 不认：{value!r}，只许 {'、'.join(ACTIONS)}")
    return value


def _subject_actor(value) -> str:
    """actor：竖线换成全角（字段分隔符），换行压掉（%s 会在换行处截断）。"""
    return " ".join(str(value or "").replace("|", "｜").split())


def _subject_note(value) -> str:
    """note：只压掉换行与首尾空白；竖线留着（parse_subject 是 split("|", 4)）。"""
    return " ".join(str(value or "").split())


def _title_owner(title: str) -> str | None:
    """已有这个标题的页就返它的 path，没有就 None。create-intent 的冲突闸（裁定 R-S2-3）。

    冲突按**标题相等**判、不按 base slug 在不在磁盘上判：中文标题现在转成拼音 slug，
    但不同标题仍可能因拼音首段相同而撞 slug（由 new_slug 补 -2）；按 base 判存在会误杀。
    """
    want = (title or "").strip()
    if not want:
        return None
    for rel, page in all_pages().items():
        if page.title == want:
            return rel
    return None


def _slug_taken(slug: str) -> bool:
    """slug 全库唯一（计划 §1）：五个目录里任意一处有同名文件都算撞。"""
    return any((settings.wiki_dir / serialize.path_for(slug, page_type)).exists()
               for page_type in serialize.TYPES)


def new_slug(title: str, page_type: str = "entity") -> str:
    """给新页发一个不撞车的 slug：serialize.slugify + 撞了就 -2、-3……

    全站唯一的 slug 来源（裁定 R88）：判重按磁盘（事实源），跨目录判（slug 全库唯一）。
    "这个标题算不算重复"不归它管，那是 _title_owner 的冲突闸——两件事分开。
    """
    serialize.page_dir(page_type)  # type 白名单：白名单外在这里抛 PageFormatError
    base = serialize.slugify(title)
    slug = base
    n = 2
    while _slug_taken(slug):
        slug = f"{base}-{n}"
        n += 1
    return slug


def new_path(title: str, page_type: str) -> str:
    """按 title + type 发一个完整 path（`{dir}/{slug}.md`）。"""
    return serialize.path_for(new_slug(title, page_type), page_type)


# base_commit 的合法形状（裁定 R-S2-29）：现行 head 的 7..40 位十六进制前缀（大小写
# 不敏感）。工具端把页头显示成 12 位短 sha，模型照抄它不该撞 409。
_BASE_RE = re.compile(r"[0-9a-f]{7,40}")


def _base_matches_head(base_commit: str, head: str | None) -> bool:
    """base_commit 是否指着这一页的现行版本：head 的十六进制前缀匹配（裁定 R-S2-29）。"""
    if head is None:
        return False
    base = base_commit.lower()
    return _BASE_RE.fullmatch(base) is not None and head.lower().startswith(base)


def _conflict(rel: str, base_commit: str, head: str | None) -> ConflictError:
    """把"你手里的版本号不是现行的"做成 ConflictError（裁定 R33）。

    head 是 None 时没有"改前"可比，diff_text 给空串 + 一句中文说明；绝不把字面
    "HEAD" 交给 repo.diff（修订号形状闸拒一切符号修订）。
    """
    if head is None:
        return ConflictError("", "这一页在库里还没有版本，你交上来的版本号对不上")
    try:
        return ConflictError(repo.diff(base_commit, head, rel))
    except repo.GitError:
        try:
            current = repo.show(rel, head)
        except repo.GitError as exc:
            return ConflictError("", f"有人先改了这一页，而且差异没取出来：{exc}")
        return ConflictError(
            f"（拿不到 {base_commit[:12]} 与现行版本的差异，下面是现行版本全文）\n{current}"
        )


def _string_list(values) -> list[str]:
    """tags/sources/contradictions：去空白、去重、排序（输出稳定，no-op 才判得准）。"""
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    try:
        items = [str(value).strip() for value in values]
    except TypeError:
        raise ValueError("tags/sources/contradictions 要一个清单") from None
    return sorted({item for item in items if item})


def _build_page(slug: str, title: str, page_type: str, body_md: str,
                *, created: str, updated: str,
                tags, sources, confidence: str, contested: bool,
                contradictions, extra: dict[str, str] | None = None) -> serialize.Page:
    """把入参归一成 Page（frontmatter 形状）。形状不对抛 ValueError。"""
    if any(ch in (title or "") for ch in "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029"):
        raise ValueError("标题不能包含换行；请使用单行标题")
    body = "\n".join((body_md or "").splitlines()).strip("\n")
    return serialize.Page(
        slug=slug,
        title=(title or "").strip(),
        page_type=page_type,
        body_md=body,
        created=created,
        updated=updated,
        tags=_string_list(tags),
        sources=_string_list(sources),
        confidence=confidence_for(sources, confidence, contested=bool(contested or contradictions)),
        contested=bool(contested or contradictions),
        contradictions=_string_list(contradictions),
        extra={str(key): " ".join(str(value).split())
               for key, value in (extra or {}).items()
               if str(key).strip() and str(value).strip()},
    )


def _gate(page: serialize.Page) -> str:
    """恒等闸（裁定 R15）：dump 出去的字节必须能原样读回来。不恒等抛 PageFormatError。"""
    text = serialize.dump_md(page)
    if serialize.dump_md(serialize.parse_md(page.slug, text)) != text:
        raise serialize.PageFormatError("读回来的不是写出去的那一页")
    return text


def _content_equal(page: serialize.Page, existing: serialize.Page) -> bool:
    """除 created/updated 之外完全相同？用于保住"首写定 created、no-op 不改 updated"。"""
    return (
        page.title == existing.title
        and page.page_type == existing.page_type
        and page.body_md == existing.body_md
        and page.tags == existing.tags
        and page.sources == existing.sources
        and page.confidence == existing.confidence
        and page.contested == existing.contested
        and page.contradictions == existing.contradictions
        and page.extra == existing.extra
    )


def _result(res: dict, rel: str | None, slug: str | None) -> dict:
    """所有写路径返回都带 path/slug（B 的 API 与工具要靠它知道写到了哪儿）。"""
    res["path"] = rel
    res["slug"] = slug
    return res


# commit 成功后的 DB 副作用最多试 3 次（首次 + 重试 2 次）的退避。
POST_COMMIT_RETRY_DELAYS = (0.05, 0.1)


def _settle_after_commit(conn, rel: str) -> None:
    """commit/no-op 之后维护 FTS；失败只记日志，绝不向外抛。

    FTS 是派生数据（git 才是事实源）：页面已经不可逆地进了历史，reindex/下次写都能补。
    若让 sqlite3.Error 冒到调用方变成 500，前端/模型会以为"没写成"而重写一遍。
    """
    for attempt in range(3):
        try:
            search.index_page_path(conn, rel)
            return
        except sqlite3.Error as exc:
            if attempt >= len(POST_COMMIT_RETRY_DELAYS):
                logger.error("页面已提交，但 FTS 没写进去（path=%s，已重试）：%r", rel, exc)
                return
            logger.warning("页面已提交，FTS 写入失败，准备重试（path=%s）：%r", rel, exc)
            time.sleep(POST_COMMIT_RETRY_DELAYS[attempt])


def _write(
    conn: sqlite3.Connection, *, rel: str, slug: str, title: str, page_type: str,
    body_md: str, actor: str, action: str | None,
    note: str = "", base_commit: str | None = None, tags=None, sources=None,
    confidence: str = "", contested: bool = False, contradictions=None,
    created: str = "", updated: str = "", extra: dict[str, str] | None = None,
) -> dict:
    """写路径的核心。不自己上锁（锁在 write_page_sync），不自己 commit 数据库。"""
    _ensure_dirs()
    path = _path(rel)
    exists = path.exists()

    if action is None:
        # 裁定 R84：create/edit 判定在锁内按文件存在现定。
        action = "edit" if exists else "create"
    elif action == "create" and exists:
        # 裁定 R17：显式 create 撞上已存在的页一律 failed，一个字都不写。
        return _result(_failed(ALREADY_EXISTS), rel, slug)

    existing = _parse_file(rel) if exists else None
    existing_text = _read_text(path) if exists else None

    # extra（我们不认识的 frontmatter 键）默认沿用现有页的，避免改一页就丢
    # Obsidian/Dataview 自定义字段；同步器显式传解析结果时可以覆盖。
    page_extra = dict(existing.extra) if (extra is None and existing is not None) else dict(extra or {})
    try:
        page = _build_page(
            slug, title, page_type, body_md,
            created="", updated="", tags=tags, sources=sources, confidence=confidence,
            contested=contested, contradictions=contradictions, extra=page_extra,
        )
    except ValueError as exc:
        return _result(_failed(f"这一页写不进去：{exc}"), rel, slug)
    except (KeyError, TypeError) as exc:
        return _result(_failed(f"入参形状不对：{exc!r}"), rel, slug)

    # 计划 §1：created 首写定、updated 每写更新。内容与磁盘上那份逐字相同时保留旧的
    # updated ——否则隔天重写同样内容会凭"日期变了"造出一个空提交，破坏 no-op 语义。
    today = _today()
    # created/updated 正常由 store 算（首写定 created、内容变了才 bump updated）。
    # 同步器（2026-10-30）要保住本地 Hermes 页 frontmatter 里的真实日期，所以多两个
    # 显式覆盖入口；格式不是 YYYY-MM-DD 就退回下面的默认逻辑，不让坏日期写进文件。
    base_created = _date_or(existing.created, today) if existing is not None else today
    page.created = _date_or(created, base_created)
    if updated:
        page.updated = _date_or(updated, today)
    elif existing is not None:
        page.updated = _date_or(existing.updated, today) if _content_equal(
            page, existing
        ) else today
    else:
        page.updated = today

    try:
        text = _gate(page)
    except ValueError as exc:  # PageFormatError 是 ValueError 的子类（裁定 R20）
        return _result(
            _failed(f"这一页写不进 md 文件：{exc}。改一改标题、类型或正文再来"), rel, slug
        )

    try:
        head = repo.head_sha(rel)
    except repo.GitError as exc:
        return _result(_failed(f"版本库没跑通，这一页没写成：{exc}"), rel, slug)
    if base_commit is not None and not _base_matches_head(base_commit, head):
        raise _conflict(rel, base_commit, head)

    # 计划 §1：type 决定目录，创建后不可改（与 slug 同口径）。放在乐观锁之后判：
    # 拿着过期 base_commit 来改 type 的人该先看到"有人先改了"。
    if existing is not None and page.page_type != existing.page_type:
        return _result(
            _failed(
                f"这一页的类型不能改（现在是 {existing.page_type}，交上来的是 {page.page_type}）"
            ),
            rel, slug,
        )

    subject = (
        f"wiki|{_subject_action(action)}|{slug}"
        f"|{_subject_actor(actor)}|{_subject_note(note)}"
    )

    # 计划 §2：no-op 写（页面字节未变）不重写 index/log、不提交。引注记录与 FTS 照样
    # 按 head 重写/重建（裁定 R46/R51）：上次可能崩在 commit 与这两步之间。
    #
    # 判据不是"磁盘字节 == 新字节"就够：磁盘上那份可能是**没提交过**的手工文件
    # （head 为 None）或相对 HEAD 有未提交改动，这时字节虽同，git 里还没有这一版。
    # 所以再看一眼 head 里的那一份是不是逐字相同——只有"已提交且内容一致"才算 no-op。
    if existing_text is not None and text == existing_text and head is not None:
        try:
            unchanged = repo.show(rel, head) == text
        except repo.GitError:
            unchanged = False
        if unchanged:
            _settle_after_commit(conn, rel)
            return _result({"ok": True, "sha": head, "noop": True, "failed": []}, rel, slug)

    # 三份快照：页面 + index + log。任一写失败/commit 失败都要逐字退回来。
    index_path = _path(INDEX_FILE)
    log_path = _path(LOG_FILE)
    snapshots = [(path, _snapshot(path)), (index_path, _snapshot(index_path)),
                 (log_path, _snapshot(log_path))]
    try:
        path.write_text(text, encoding="utf-8")
        index_path.write_text(_index_text(), encoding="utf-8")
        current_log = _read_text(log_path) or LOG_HEADER
        if current_log and not current_log.endswith("\n"):
            current_log += "\n"
        log_path.write_text(
            current_log + log_block(action, title or note or slug, rel, actor),
            encoding="utf-8",
        )
    except OSError as exc:
        _restore(path, snapshots[0][1])
        _restore(index_path, snapshots[1][1])
        _restore(log_path, snapshots[2][1])
        return _result(_failed(f"文件写不下去，这一页没写成：{exc}"), rel, slug)

    try:
        sha = repo.commit([rel, LOG_FILE], subject)
    except (repo.GitError, subprocess.TimeoutExpired, OSError) as exc:
        # 裁定 R45：撤下三份的暂存 + 工作区逐字退回写之前，不留残局。
        repo.unstage([rel, LOG_FILE])
        _restore(path, snapshots[0][1])
        _restore(index_path, snapshots[1][1])
        _restore(log_path, snapshots[2][1])
        return _result(_failed(f"版本库没跑通，这一页没写成：{exc}"), rel, slug)

    _settle_after_commit(conn, rel)
    return _result({"ok": True, "sha": sha, "noop": sha is None, "failed": []}, rel, slug)


@_serialized_write
def write_page_sync(
    conn: sqlite3.Connection, *, path: str | None = None, title: str, page_type: str,
    body_md: str, actor: str, action: str | None = None,
    note: str = "", base_commit: str | None = None, tags=None, sources=None,
    confidence: str = "", contested: bool = False, contradictions=None,
    created: str = "", updated: str = "", extra: dict[str, str] | None = None,
    citations=None,  # 已下线（2026-10-30）：旧调用方传进来直接忽略
) -> dict:
    """写一页的同步入口：测试、cli、上传摄入、async write_page 底下都是它。

    path=None（create-intent，裁定 R91）= 服务端按 title + page_type 发目录与 slug：
    **冲突闸 key 在标题相等上**（裁定 R-S2-3）：已有同标题的页 → 按 R17 返 failed
    「这一页已存在」，不静默编辑；标题不撞车 → new_path 发名，同日的第二个中文标题
    靠 -2/-3 错开。`_CREATE_LOCK` **持到 _write 返回为止**（裁定 R-S2-4）：判标题冲突、
    发 path、落盘整段串行，并发建页不会拿到同一个 slug 互相覆盖。

    显式 path：先过 path 形状闸，再进 per-path 锁，不碰 _CREATE_LOCK（带的名字自己负责）。
    action=None 由 _write 在锁内按文件存在现定 create/edit；显式 "create" 保留 R17 撞车闸。

    返回 dict 在 {"ok","sha","noop","failed"} 之外带 "path" 与 "slug"：path=None 时调用方
    （路由、工具）要靠 path 才知道服务端把这一页写到了哪儿；冲突那条 failed 也带
    "path"，且带的是**撞上的那一页**的 path。
    """
    if path is None:
        outer = _CREATE_LOCK
    else:
        outer = contextlib.nullcontext()
    with outer:
        if path is None:
            try:
                serialize.page_dir(page_type)
            except ValueError as exc:
                return _result(
                    _failed(f"这一页写不进 md 文件：{exc}。改一改标题、类型或正文再来"),
                    None, None,
                )
            clash = _title_owner(title)
            if clash is not None:
                return _result(_failed(ALREADY_EXISTS), clash, slug_of(clash))
            slug = new_slug(title, page_type)
            rel = serialize.path_for(slug, page_type)
        else:
            rel = _canonical_write_path(path)
            if rel is None:
                raise ValueError(f"path 不合法：{path!r}")
            slug = slug_of(rel)
        with _lock_for(rel):
            return _write(
                conn, rel=rel, slug=slug, title=title, page_type=page_type,
                body_md=body_md, actor=actor, action=action, note=note,
                base_commit=base_commit, tags=tags, sources=sources,
                confidence=confidence, contested=contested, contradictions=contradictions,
                created=created, updated=updated, extra=extra,
            )


async def write_page(conn, **kwargs) -> dict:
    """写一页的异步入口：锁在 write_page_sync 里，这里只把子进程的活挪出事件循环。"""
    return await asyncio.to_thread(write_page_sync, conn, **kwargs)


@_serialized_write
def revert_sync(conn: sqlite3.Connection, path: str, sha: str, actor: str) -> dict:
    """回滚 = 把某个旧版本的全文当新内容，再走一遍写路径。"""
    rel = canonical_path(path)
    if rel is None:
        if isinstance(path, str) and valid_slug(path.strip()):
            return _result(_failed(f"回滚不了，取不到那个版本：没有这一页"), None,
                           path.strip())
        raise ValueError(f"path 不合法：{path!r}")
    try:
        old = repo.show(rel, sha)
    except repo.GitError as exc:
        return _result(_failed(f"回滚不了，取不到那个版本：{exc}"), rel, slug_of(rel))
    try:
        page = serialize.parse_md(slug_of(rel), old)
    except ValueError as exc:
        return _result(
            _failed(f"回滚不了，那个版本的文件读不回来：{exc}"), rel, slug_of(rel)
        )
    return write_page_sync(
        conn, path=rel, title=page.title, page_type=page.page_type,
        body_md=page.body_md, actor=actor, action="revert",
        note=f"回滚到 {sha[:8]}",
        tags=page.tags, sources=page.sources, confidence=page.confidence,
        contested=page.contested, contradictions=page.contradictions,
        extra=page.extra,
    )


async def revert(conn, path: str, sha: str, actor: str) -> dict:
    return await asyncio.to_thread(revert_sync, conn, path, sha, actor)


@_serialized_write
def delete_page_sync(
    conn: sqlite3.Connection, path: str, *, actor: str,
    base_commit: str | None = None, note: str = "",
) -> dict:
    """删一页：删文件 + 重写 index.md + 追加 log.md，**一次 commit**（action='delete'）。

    只删现行文件，历史仍在 git 里（版本/回滚照旧看得到）；不做归档目录。
    路径必须是合法且存在的页面，否则返 failed「没有这一页」。base_commit 给了就按
    现有乐观锁口径和 head 比，不一致抛 ConflictError（路由翻 409 / 工具给模型 diff）。
    删除后的 FTS 走 `_settle_after_commit`：页面文件已经不在，index_page_path 会把该页的
    FTS 行清掉；失败不影响 ok（git 是事实源）。
    """
    rel = canonical_path(path)
    if rel is None:
        return _result(_failed("没有这一页"), None, None)
    slug = slug_of(rel)
    page = _parse_file(rel)
    if page is None:
        if not _path(rel).exists():
            return _result(_failed("没有这一页"), rel, slug)
        return _result(_failed("这一页的文件读不回来，删不了"), rel, slug)

    try:
        head = repo.head_sha(rel)
    except repo.GitError as exc:
        return _result(_failed(f"版本库没跑通，这一页没删成：{exc}"), rel, slug)
    if base_commit is not None and not _base_matches_head(base_commit, head):
        raise _conflict(rel, base_commit, head)

    page_path = _path(rel)
    index_path = _path(INDEX_FILE)
    log_path = _path(LOG_FILE)
    snapshots = [
        (page_path, _snapshot(page_path)),
        (index_path, _snapshot(index_path)),
        (log_path, _snapshot(log_path)),
    ]
    subject = (
        f"wiki|{_subject_action('delete')}|{slug}"
        f"|{_subject_actor(actor)}|{_subject_note(note or '删除这一页')}"
    )
    try:
        page_path.unlink(missing_ok=True)
        index_path.write_text(_index_text(), encoding="utf-8")
        current_log = _read_text(log_path) or LOG_HEADER
        if current_log and not current_log.endswith("\n"):
            current_log += "\n"
        log_path.write_text(
            current_log + log_block("delete", page.title or slug, rel, actor),
            encoding="utf-8",
        )
    except OSError as exc:
        _restore(page_path, snapshots[0][1])
        _restore(index_path, snapshots[1][1])
        _restore(log_path, snapshots[2][1])
        return _result(_failed(f"文件删不下去，这一页没删成：{exc}"), rel, slug)

    try:
        sha = repo.commit([rel, LOG_FILE], subject)
    except (repo.GitError, subprocess.TimeoutExpired, OSError) as exc:
        # 与写路径同口径：撤下三份的暂存 + 工作区逐字退回删之前，不留残局。
        repo.unstage([rel, LOG_FILE])
        _restore(page_path, snapshots[0][1])
        _restore(index_path, snapshots[1][1])
        _restore(log_path, snapshots[2][1])
        return _result(_failed(f"版本库没跑通，这一页没删成：{exc}"), rel, slug)

    _settle_after_commit(conn, rel)
    res = {"ok": True, "sha": sha, "noop": sha is None, "failed": [], "action": "delete"}
    return _result(res, rel, slug)
