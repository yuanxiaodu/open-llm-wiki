"""Rebuildable navigation and append-only audit maintenance under the wiki write lock."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date

from ..config import settings
from . import repo, store


def maintain(*, actor: str = "维护", action: str | None = None, subject: str = "") -> dict:
    with repo.wiki_write_lock():
        if repo.rebase_in_progress():
            raise store.StoreError("版本库正在合并，暂不维护导航")
        changes = {}
        log_path = settings.wiki_dir / "log.md"
        log = log_path.read_text(encoding="utf-8") if log_path.exists() else store.LOG_HEADER
        if action:
            log += store.log_block(action, subject, "（审计）", actor)
        if log.count("\n## [") > 500:
            archive = f"log-{date.today().year}.md"
            path = settings.wiki_dir / archive
            prior = path.read_text(encoding="utf-8") if path.exists() else "# 历史行动日志\n\n"
            changes[archive] = prior + "\n" + log
            log = store.LOG_HEADER + store.log_block("archive", archive, archive, actor)
        if not log_path.exists() or log_path.read_text(encoding="utf-8") != log:
            changes["log.md"] = log
        pages = store.all_pages()
        if len(pages) > 200:
            groups = defaultdict(list)
            for rel, page in pages.items():
                if page.extra.get("status") == "draft": continue
                for tag in page.tags or ["待分类"]:
                    groups[tag].append(page)
            lines = ["# 主题地图", "", "按标签导航；同组不表示观点一致。"]
            for tag, items in sorted(groups.items()):
                lines += ["", f"## {tag}"]
                lines.extend(f"- [[{p.slug}|{p.title}]]" for p in sorted(items, key=lambda p: p.title))
            changes["_meta/topic-map.md"] = "\n".join(lines) + "\n"
        snapshots = {}
        written = []
        try:
            for rel, text in changes.items():
                path = settings.wiki_dir / rel
                if path.exists() and path.read_text(encoding="utf-8") == text: continue
                snapshots[rel] = path.read_bytes() if path.exists() else None
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                written.append(rel)
            if written:
                repo.commit(written, f"meta|{action or 'maintenance'}|navigation|{store._subject_actor(actor)}|导航与审计")
        except Exception:
            repo.unstage(written)
            for rel, before in snapshots.items():
                store._restore(settings.wiki_dir / rel, before)
            raise
        return {"changed": written}
