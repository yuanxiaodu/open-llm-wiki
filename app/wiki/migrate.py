"""旧布局（`data/wiki/*.md` 平文件）→ Hermes 物理 vault 的幂等迁移。

1. 顶层 `*.md`（非特殊文件）按 type 移进 `entities/` 等目录，`topic` → `entity`；
   `outline` 与 `_outline_.md` 不再支持，原样跳过（本版本已移除文档模块）；
2. 补 frontmatter（created/updated/tags/sources）；
3. SCHEMA.md/index.md/log.md 由 store.ensure_vault 补（本模块只负责页面搬家）；
4. 一次 commit（kind=wiki, action=edit, note=迁移到 LLM-Wiki vault）。

幂等的判据是"vault 根上还有没有要搬的 md"：已经是新布局时本模块一个文件都不动，
`git commit` 也自然不产生提交（repo.commit 无改动返 None）。
"""

from __future__ import annotations

import re
from datetime import date, datetime

from ..config import settings
from . import repo, serialize

# 迁移提交的主题（计划 §7 Step 5）。_MIGRATE_ 不是页面 slug，是这次搬家的固定标识。
MIGRATION_SUBJECT = "wiki|edit|_migrate_|system|迁移到 LLM-Wiki vault"

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _date_or(value: str, fallback: str) -> str:
    """frontmatter 里的日期只认 YYYY-MM-DD（计划 §1）；其它形状用 fallback 顶。"""
    text = (value or "").strip()
    return text if _DATE_RE.fullmatch(text) else fallback


def _file_date(path) -> str:
    """文件的 mtime 日期——旧页没有 created 时，这是最接近"首次写"的可得事实。"""
    try:
        return date.fromtimestamp(path.stat().st_mtime).isoformat()
    except OSError:
        return date.today().isoformat()


def _migrate_page(path, moved: list, skipped: list, changed: list) -> None:
    """第 2/3 步：一页旧平文件 → 按 type 的新目录 + 补齐 frontmatter。"""
    from . import store  # 懒导入：store 在 ensure_vault 里反过来调本模块（避免 import 环）

    rel_old = path.name
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        skipped.append(f"{rel_old}：读不出来（{exc}）")
        return
    try:
        page = serialize.parse_md(path.stem, text, allow_legacy=True)
    except ValueError as exc:  # PageFormatError 是 ValueError 的子类
        skipped.append(f"{rel_old}：形状读不回来（{exc}）")
        return
    if page.page_type == "outline":
        # 旧 outline 类型已移除：旧的 outline 页不再有归处，原样跳过。
        skipped.append(f"{rel_old}：outline 类型已不再支持，原样跳过")
        return
    if not store.valid_slug(page.slug):
        # 形状不对的 slug 进不了新布局（get_page 也认不了它，读侧会当没有这一页）。
        skipped.append(f"{rel_old}：slug 形状不合法，原样留着不搬")
        return

    target_rel = serialize.page_path(page)
    target = settings.wiki_dir / target_rel
    if target.exists():
        skipped.append(f"{rel_old}：目标 {target_rel} 已存在，不覆盖")
        return

    mtime = _file_date(path)
    page.created = _date_or(page.created, mtime)
    page.updated = _date_or(page.updated, page.created)
    page.tags = sorted(set(page.tags))
    page.sources = sorted(set(page.sources))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(serialize.dump_md(page), encoding="utf-8")
    path.unlink()
    changed += [rel_old, target_rel]
    moved.append((rel_old, target_rel))


def migrate(conn=None, *, commit: bool = True) -> dict:
    """把旧平文件布局迁到新 vault。幂等：没有可搬的文件时什么都不做、也不提交。

    commit=False 给 store.ensure_vault 用：它要把"迁移 + 补 SCHEMA/index/log"合成
    计划 §7 说的**一次** commit，所以先收集路径、由它统一提交。
    返回 {"changed":[rel...], "moved":[(old,new)...], "skipped":[人话...], "changed_any": bool}。
    """
    changed: list[str] = []
    moved: list[tuple[str, str]] = []
    skipped: list[str] = []

    wiki_dir = settings.wiki_dir
    if not wiki_dir.exists():
        return {"changed": [], "moved": [], "skipped": [], "changed_any": False}

    for path in sorted(wiki_dir.glob("*.md")):
        if path.name in serialize.SPECIAL_FILES or path.name == "_outline_.md":
            continue
        _migrate_page(path, moved, skipped, changed)

    if commit and changed:
        repo.commit(changed, MIGRATION_SUBJECT)
    return {
        "changed": changed,
        "moved": moved,
        "skipped": skipped,
        "changed_any": bool(changed),
    }
