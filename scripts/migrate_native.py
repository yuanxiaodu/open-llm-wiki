#!/usr/bin/env python3
"""一次性把 PM_wiki / open-llm-wiki 的 vault 迁移到 Hermes llm-wiki 2.1.0 原生结构。

用法：
  python scripts/migrate_native.py --wiki "$HOME/Documents/知识库/pm-wiki" --dry-run
  python scripts/migrate_native.py --wiki "$HOME/Documents/知识库/pm-wiki" \
      --apply --drafts publish|archive

只做数据迁移，不改应用代码；迁移前请给 wiki 仓库打 tag/分支并备份。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

PAGE_DIRS = {
    "entity": "entities",
    "concept": "concepts",
    "comparison": "comparisons",
    "query": "queries",
}
TYPE_ORDER = ("entity", "concept", "comparison", "query")
LABELS = {
    "entity": "Entities",
    "concept": "Concepts",
    "comparison": "Comparisons",
    "query": "Queries",
}
NATIVE_KEYS = (
    "title", "created", "updated", "type", "tags", "sources",
    "confidence", "contested", "contradictions",
)
DROP_KEYS = {"status", "analysis", "author_id", "basis_versions", "review_notes"}

SUMMARY_TO_QUERY = {
    "about-face-4-jiao-hu-she-ji-jing-sui-zhi-shi-ku-dao-hang",
    "ai-agent",
    "chan-pin-jing-li-qiu-zhi-yu-zhuan-xing-zhuan-ti-dao-hang",
    "hu-lian-wang-chan-pin-jing-li-01-jiang-yi-zhi-shi-ku-dao",
    "kua-yue-hong-gou-zhi-shi-ku-dao-hang",
    "manus-agent-2",
    "mu-hou-chan-pin-zhi-shi-ku-dao-hang",
    "yu-jun-chan-pin-fang-fa-lun-zhi-shi-ku-dao-hang",
    "zhang-xiao-long-wei-xin-gong-kai-ke-zhuan-ti-dao-hang",
}
SUMMARY_TO_CONCEPT = {
    "chan-pin-jing-li-ru-he-tui-dong-kua-tuan-dui-xie-zuo",
    "chan-pin-jing-li-wu-nian-hou-de-si-tiao-fa-zhan-lu-xian",
    "fu-neng-zu-zhi-ti-chang-yu-jin-zhi-de-wen-hua-fen-wei-qing",
    "take-away-timing",
    "xiao-hong-fu-pan-2",
    "zheng-ti-chan-pin-guan-li-de-ba-ge-zhu-yi-shi-xiang",
}
RAW_ARTICLE_IDS = {26, 27, 28, 30, *range(32, 45)}
RAW_PAPER_IDS = {10, 13, 20, 21, 22, 23, 24, 25, 29, 31}
RAW_TRANSCRIPT_IDS = {11, 12, 14, 15, 16, 17, 18, 19}


def _run(wiki: Path, *args: str) -> str:
    proc = subprocess.run(("git", *args), cwd=wiki, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{proc.stderr.strip()}")
    return proc.stdout


def _status(wiki: Path) -> str:
    return _run(wiki, "status", "--porcelain")


def _split(text: str) -> tuple[list[str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return [], text
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return lines[1:i], "\n".join(lines[i + 1:]).lstrip("\n")
    return [], text


def _meta(front: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in front:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


def _flow(value: str) -> list[str]:
    value = (value or "").strip()
    if value.startswith("[") and value.endswith("]"):
        return [item.strip().strip("'\"") for item in value[1:-1].split(",") if item.strip()]
    return [value] if value else []


def _scalar(value: str) -> str:
    value = " ".join(str(value or "").split())
    if not value:
        return '""'
    if any(ch in value for ch in ':#[]{}"') or value in ("true", "false"):
        return json.dumps(value, ensure_ascii=False)
    return value


def _write_front(meta: dict[str, str], body: str) -> str:
    lines = ["---"]
    for key in NATIVE_KEYS:
        if key in meta and meta[key] != "":
            lines.append(f"{key}: {meta[key]}")
    lines += ["---", "", body.rstrip(), ""]
    return "\n".join(lines)


def _pages(wiki: Path) -> list[Path]:
    out: list[Path] = []
    for name in ("entities", "concepts", "comparisons", "queries", "summaries"):
        path = wiki / name
        if path.is_dir():
            out += sorted(path.glob("*.md"))
    return out


def _page_map(wiki: Path) -> dict[Path, tuple[Path, str | None]]:
    mapping: dict[Path, tuple[Path, str | None]] = {}
    for path in _pages(wiki):
        rel = path.relative_to(wiki)
        slug = path.stem
        if rel.parts[0] == "summaries":
            page_type = "query" if slug in SUMMARY_TO_QUERY else "concept"
            mapping[path] = (wiki / PAGE_DIRS[page_type] / path.name, page_type)
    return mapping


def _raw_map(wiki: Path) -> dict[Path, Path]:
    mapping: dict[Path, Path] = {}
    uploads = wiki / "raw" / "uploads"
    if uploads.is_dir():
        for path in sorted(uploads.glob("*.md")):
            match = re.match(r"^(\d+)-", path.name)
            if not match:
                continue
            uid = int(match.group(1))
            if uid in RAW_ARTICLE_IDS:
                dest = wiki / "raw" / "articles" / path.name
            elif uid in RAW_PAPER_IDS:
                dest = wiki / "raw" / "papers" / path.name
            elif uid in RAW_TRANSCRIPT_IDS:
                dest = wiki / "raw" / "transcripts" / path.name
            else:
                raise SystemExit(f"raw 文件无法分类：{path}")
            mapping[path] = dest
    messages = wiki / "raw" / "messages"
    if messages.is_dir():
        for path in sorted(messages.glob("*.md")):
            mapping[path] = wiki / "raw" / "transcripts" / path.name
    return mapping


def _rewrite_page(path: Path, new_type: str | None, drafts: str) -> tuple[str, bool]:
    text = path.read_text(encoding="utf-8")
    front, body = _split(text)
    meta = _meta(front)
    changed = False
    if new_type:
        meta["type"] = new_type
        changed = True
    analysis = meta.get("analysis", "")
    for key in list(meta):
        if key in DROP_KEYS:
            meta.pop(key)
            changed = True
    if meta.get("type") == "summary":
        meta["type"] = "concept"
        changed = True
    if body and not body.lstrip().startswith("## 范围与依据") and analysis:
        try:
            spec = json.loads(analysis)
        except ValueError:
            spec = {}
        bits = []
        for label, key in (("问题", "question"), ("适用条件", "scope"),
                           ("分析时间", "as_of"), ("不确定性", "uncertainties")):
            value = spec.get(key)
            if value:
                bits.append(f"- {label}：{value}")
        basis = spec.get("basis_paths") or spec.get("basis_versions") or []
        if basis:
            bits.append("- 依据页面：" + "、".join(
                str(item.get("path") if isinstance(item, dict) else item) for item in basis))
        if bits:
            body = "## 范围与依据\n\n" + "\n".join(bits) + "\n\n" + body.lstrip("\n")
            changed = True
    # draft query pages: publish or archive
    if meta.get("status") == "draft":
        if drafts != "publish":
            # caller handles archive separately; keep type/body but strip status
            meta.pop("status", None)
        changed = True
    meta.pop("status", None)
    return _write_front(meta, body), changed


def _native_schema(wiki: Path) -> str:
    old = (wiki / "SCHEMA.md").read_text(encoding="utf-8") if (wiki / "SCHEMA.md").exists() else ""
    tags = []
    pattern = re.compile(r"^##\s+(?:tags|tag taxonomy)\s*$", re.I | re.M)
    match = pattern.search(old)
    if match:
        rest = old[match.end():]
        nxt = re.search(r"^##\s+", rest, re.M)
        if nxt:
            rest = rest[:nxt.start()]
        for line in rest.splitlines():
            item = line.strip().lstrip("-* ").strip()
            if item:
                tags.append(item.strip("`"))
    if not tags:
        tags = ["用户研究", "需求洞察", "产品设计", "数据指标", "增长", "团队协作",
                "转型路径", "案例", "方法论", "工具", "商业化", "竞争策略", "组织管理", "决策方法"]
    tag_lines = "\n".join(f"- {tag}" for tag in dict.fromkeys(tags))
    return f"""# Wiki Schema

## Domain

新传专业产品经理转型指南：众筹共写的知识库。事实句必须有出处，页面之间用 wikilink 连成网。

## Conventions

- 页面 path 形如 `{{dir}}/{{slug}}.md`；`type` 决定目录，创建后不可改（entity/concept/comparison/query）。
- 来源写在 frontmatter 的 `sources` 里；正文不强制逐句引注。
- 页面互链用 `[[slug]]` 或 `[[slug|显示文字]]`；每页至少 2 个出链。
- 每页 ≤ 200 行；超过就拆页。
- `index.md`、`SCHEMA.md`、`log.md` 都是 wiki 的一部分，跟页面一起跟踪、同步。
- `log.md` 追加合并用 `merge=union`。
- 多来源综合在段落末尾用 `^[raw/路径]` 标来源；不要写 `[^c1]` 脚注。
- 矛盾显式记录：frontmatter 写 `contested: true` 与 `contradictions: [other-slug]`。

## Tag Taxonomy

{tag_lines}

## Page Thresholds

- 一个实体/概念在 2+ 来源出现，或是一份材料的核心，才建页。
- 通过提及、细节、域外内容不建页；同主题先改既有页。
- 页面超过 200 行拆成子主题并互链。
- 内容被完全取代时移到 `_archive/`，从 index 删除。

## Entity Pages

一页一个实体：概述、关键事实/日期、与其它实体的关系、来源。

## Concept Pages

一页一个概念/主题：定义、当前知识状态、开放问题/争议、相关概念。

## Comparison Pages

对比页：比较什么、维度、结论/综合、来源。

## Update Policy

1. 先看日期，新来源一般覆盖旧来源。
2. 真矛盾时并列两说并标日期/来源。
3. frontmatter 写 `contradictions`，lint 报告给人复核。
"""


def _native_index(wiki: Path) -> str:
    pages = []
    for name in PAGE_DIRS.values():
        for path in sorted((wiki / name).glob("*.md")):
            text = path.read_text(encoding="utf-8")
            front, body = _split(text)
            meta = _meta(front)
            summary = ""
            for line in body.splitlines():
                if line.strip():
                    summary = re.sub(r"^#{1,6}\s*", "", line.strip())
                    summary = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", r"\1", summary)
                    summary = re.sub(r"\^\[raw/[^\]]+\]", "", summary)
                    summary = " ".join(summary.split())
                    if len(summary) > 120:
                        summary = summary[:119].rstrip() + "…"
                    break
            pages.append((meta, path, summary, body))
    updated = max((m.get("updated", "") for m, _, _, _ in pages), default="")
    lines = [
        "# Wiki Index", "",
        f"> Content catalog. Every wiki page listed under its type with a one-line summary.",
        f"> Last updated: {updated or '—'} | Total pages: {len(pages)}", "",
    ]
    for page_type in TYPE_ORDER:
        group = [(m, p, s) for m, p, s, _ in pages if m.get("type") == page_type]
        if not group:
            continue
        lines.append(f"## {LABELS[page_type]}")
        for meta, path, summary in sorted(group, key=lambda item: item[1].stem):
            entry = f"- [[{path.stem}|{meta.get('title') or path.stem}]]"
            if summary:
                entry += f" — {summary}"
            lines.append(entry)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _native_log(wiki: Path, raw_map: dict[Path, Path]) -> str:
    path = wiki / "log.md"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    for old, new in raw_map.items():
        text = text.replace(str(old.relative_to(wiki)), str(new.relative_to(wiki)))
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    header = [
        "# Wiki Log", "",
        "> Chronological record of all wiki actions. Append-only.",
        "> Format: `## [YYYY-MM-DD] action | subject`",
        "> Actions: ingest, update, query, lint, create, archive, delete",
        "",
    ]
    body = "\n".join(lines).lstrip("\n")
    entry = f"## [{date.today().isoformat()}] migrate | native wiki structure\n- path: SCHEMA.md\n- path: index.md\n"
    return "\n".join(header) + body.rstrip() + "\n\n" + entry


def _rewrite_raw(path: Path, new: Path) -> None:
    text = path.read_text(encoding="utf-8")
    front, body = _split(text)
    meta = _meta(front)
    sha = meta.get("content_sha256") or meta.get("sha256") or ""
    source_url = meta.get("source_url", "")
    ingested = meta.get("ingested") or date.today().isoformat()
    lines = ["---"]
    if source_url:
        lines.append(f"source_url: {_scalar(source_url)}")
    lines.append(f"ingested: {ingested}")
    if sha:
        lines.append(f"sha256: {sha}")
    lines += ["---", "", body.rstrip(), ""]
    new.parent.mkdir(parents=True, exist_ok=True)
    new.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wiki", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--drafts", choices=("publish", "archive"), default=None)
    args = parser.parse_args()
    wiki = Path(args.wiki).expanduser().resolve()
    if not (wiki / ".git").is_dir():
        raise SystemExit(f"不是 git 仓库：{wiki}")
    if _status(wiki).strip():
        raise SystemExit("工作区不干净，先 commit/stash")
    if args.apply and args.drafts is None:
        raise SystemExit("--apply 必须指定 --drafts publish|archive")

    page_map = _page_map(wiki)
    raw_map = _raw_map(wiki)
    print(f"wiki: {wiki}")
    print(f"summaries → 四目录: {len(page_map)} 页")
    for old, (new, typ) in sorted(page_map.items(), key=lambda item: str(item[0])):
        print(f"  {old.relative_to(wiki)} -> {new.relative_to(wiki)} ({typ})")
    print(f"raw → 原生目录: {len(raw_map)} 个文件")
    for old, new in sorted(raw_map.items(), key=lambda item: str(item[0])):
        print(f"  {old.relative_to(wiki)} -> {new.relative_to(wiki)}")

    if args.dry_run or not args.apply:
        print("\ndry-run：没有写任何文件。确认后用 --apply --drafts publish|archive 执行。")
        return

    # 1) pages: move, rewrite, archive drafts if requested
    archived = []
    for old, (new, typ) in page_map.items():
        new.parent.mkdir(parents=True, exist_ok=True)
        _run(wiki, "mv", str(old.relative_to(wiki)), str(new.relative_to(wiki)))
        is_draft = "status: draft" in new.read_text(encoding="utf-8")
        text, _changed = _rewrite_page(new, typ, args.drafts)
        if is_draft and args.drafts == "archive":
            dest = wiki / "_archive" / "queries" / new.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            _run(wiki, "mv", str(new.relative_to(wiki)), str(dest.relative_to(wiki)))
            archived.append(dest)
            dest.write_text(text, encoding="utf-8")
        else:
            new.write_text(text, encoding="utf-8")
    # non-summary pages: strip extension keys
    for path in _pages(wiki):
        text = path.read_text(encoding="utf-8")
        front, body = _split(text)
        if not front:
            continue
        meta = _meta(front)
        if any(key in meta for key in DROP_KEYS) or meta.get("type") == "summary":
            if meta.get("status") == "draft" and args.drafts == "archive":
                dest = wiki / "_archive" / "queries" / path.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                _run(wiki, "mv", str(path.relative_to(wiki)), str(dest.relative_to(wiki)))
                archived.append(dest)
                text, _ = _rewrite_page(dest, "concept" if meta.get("type") == "summary" else None,
                                        args.drafts)
                dest.write_text(text, encoding="utf-8")
                continue
            text, _ = _rewrite_page(path, "concept" if meta.get("type") == "summary" else None,
                                    args.drafts)
            path.write_text(text, encoding="utf-8")

    # 2) raw: move + rewrite frontmatter
    for old, new in raw_map.items():
        if not old.exists():
            continue
        new.parent.mkdir(parents=True, exist_ok=True)
        _run(wiki, "mv", str(old.relative_to(wiki)), str(new.relative_to(wiki)))
        _rewrite_raw(new, new)

    # 3) replace old paths in every markdown file
    replacements = {
        str(old.relative_to(wiki)): str(new.relative_to(wiki))
        for old, new in raw_map.items()
    }
    replacements.update({
        str(old.relative_to(wiki)): str(new.relative_to(wiki))
        for old, (new, _typ) in page_map.items()
    })
    for path in wiki.rglob("*.md"):
        if ".git" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        changed = text
        for old, new in sorted(replacements.items(), key=lambda item: -len(item[0])):
            changed = changed.replace(old, new)
        if changed != text:
            path.write_text(changed, encoding="utf-8")

    # 4) native SCHEMA / index / log
    (wiki / "SCHEMA.md").write_text(_native_schema(wiki), encoding="utf-8")
    (wiki / "index.md").write_text(_native_index(wiki), encoding="utf-8")
    (wiki / "log.md").write_text(_native_log(wiki, raw_map), encoding="utf-8")

    # 5) remove empty old dirs
    for old in ("summaries", "raw/uploads", "raw/messages"):
        path = wiki / old
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    print(f"\n完成：迁移 {len(page_map)} 页 / {len(raw_map)} 个 raw；归档 {len(archived)} 页。")
    print("请 git diff 复核后 commit；再重启服务端/同步。")


if __name__ == "__main__":
    main()
