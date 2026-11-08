"""本机 Hermes vault → 线上 wiki 的同步器（2026-10-30，委托人裁定：本地权威、手动发布）。

拓扑与职责：

    本地 vault(Hermes，纯文件)  ──commit/push──▶  bare repo  ──fetch──▶  本模块
                                                                         │
                                          逐页重放（本地优先） ◀─────────┘
                                          store 写路径：校验/乐观锁/派生文件/commit
                                                                         │
                                          push main ──▶ 本地 fetch + reset

为什么是"逐页重放"而不是 `git merge`：
- 页面必须过 store 的校验（frontmatter/type/引注/恒等闸），git 直接合进来的文件会绕过；
- `index.md` / `log.md` 是派生文件（每次写由服务端重建），不参与合并；
- 线上 main 保持线性、sha 稳定，`base_commit` 与引注核对记录（按 sha 绑定）不会因为
  rebase 失效。

冲突策略（本地优先）：
- 双方改同一页：三方合并（base = 上次同步时线上那一版），冲突块取本地；
  线上非冲突改动保留；被覆盖的线上版留在 git 历史/版本页里（不通知、不做冲突页）。
- 本地删页/改名：按本地执行（线上原版同样留在历史里）。
- 本地页形状非法 / type 与目录不符 / 引注核对不过：这一页不落库，记进 runs 的
  detail，线上保持合法版本；其余页照常同步（不整批停摆）。

状态推进：只在整批成功（含 push）后写 wiki_sync_state；有失败页时状态不动，
下次发布从同一个点重算（成功页内容相同会走 store 的 no-op）。
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess

from ..config import settings
from ..db import utcnow
from ..wiki import repo, serialize, store

# 一台机器一个 outbox 分支：refs/heads/hermes/<机器名>
BRANCH_PREFIX = "hermes/"
DEFAULT_ACTOR = "Hermes"
MAX_RETRIES = 3
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


class SyncError(RuntimeError):
    """同步没法开始（远端没有分支、vault 不是仓库一类的配置问题）。"""


def _valid_page(path: str) -> bool:
    return store.valid_page_path(path)


def _read_page(rel: str) -> str | None:
    """线上工作区里这一页的字节；不在（或读不回来）返 None。"""
    path = settings.wiki_dir / rel
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise SyncError(f"线上文件读不回来：{rel}：{exc}") from exc


def _parse_page(rel: str, text: str) -> serialize.Page:
    """本地页文本 → Page；形状/type 不对抛 ValueError（带人能看懂的原因）。"""
    slug = store.slug_of(rel)
    if slug is None:
        raise ValueError(f"path 形状不对：{rel}")
    page = serialize.parse_md(slug, text)
    expected_type = store.page_type_of(rel)
    if expected_type is not None and page.page_type != expected_type:
        raise ValueError(
            f"type 与目录不一致：frontmatter 是 {page.page_type}，文件在 {rel}"
        )
    return page


def _merge(base: str | None, current: str, local: str) -> tuple[str, int]:
    """三方合并，冲突块取本地；返回 (正文, 冲突块数)。base=None 表示本地新增。"""
    if base is None:
        # add/add（双方各自新建同名页）或线上没有这一页：本地权威，整页取本地。
        return local, 0
    merged, conflicts = repo.merge_file(current, base, local)
    if conflicts:
        merged, _ = repo.merge_file(current, base, local, favor="theirs")
    return merged, conflicts


def _write_page(
    conn: sqlite3.Connection, rel: str, text: str, *, base_sha: str | None,
    actor: str, local_sha: str,
) -> dict:
    """把一页本地正文重放进线上：三方合并 + store 写路径 + 并发撞车重试。"""
    last_error = ""
    for _attempt in range(MAX_RETRIES):
        try:
            current = _read_page(rel)
        except SyncError as exc:
            return {"ok": False, "reason": str(exc), "path": rel, "retry": False}
        current_head = repo.head_sha(rel) if current is not None else None
        base = repo.try_show_ref(base_sha, rel) if base_sha else None
        try:
            page = _parse_page(rel, text)
        except ValueError as exc:
            return {"ok": False, "reason": str(exc), "path": rel, "retry": False}

        if current is None:
            merged, conflicts = text, 0
        else:
            merged, conflicts = _merge(base, current, text)
        try:
            parsed = _parse_page(rel, merged)
        except ValueError as exc:
            return {"ok": False, "reason": f"合并后的页读不回来：{exc}", "path": rel, "retry": False}

        try:
            res = store.write_page_sync(
                conn, path=rel, title=parsed.title, page_type=parsed.page_type,
                body_md=parsed.body_md,
                actor=actor, action=None,
                note=f"从本机同步 {local_sha[:8]}",
                base_commit=current_head,
                tags=parsed.tags, sources=parsed.sources,
                confidence=parsed.confidence, contested=parsed.contested,
                contradictions=parsed.contradictions,
                created=parsed.created, updated=parsed.updated,
                extra=parsed.extra,
            )
        except store.ConflictError:
            # 重放期间线上有人写了这一页：重新读 current/head 再算一次，最多 3 次。
            last_error = "同步时线上又改了同一页（已重试）"
            continue
        except (store.StoreError, repo.GitError, OSError, ValueError, SyncError) as exc:
            return {"ok": False, "reason": str(exc), "path": rel, "retry": False}

        if res["ok"]:
            return {
                "ok": True, "path": rel, "sha": res.get("sha"),
                "noop": bool(res.get("noop")), "conflicts": conflicts,
            }
        note = res["failed"][0]["note"] if res.get("failed") else "这一页没写成"
        return {"ok": False, "reason": note, "path": rel, "retry": False}
    return {"ok": False, "reason": last_error or "重试次数用尽", "path": rel, "retry": False}


def _delete_page(
    conn: sqlite3.Connection, rel: str, *, actor: str, local_sha: str,
) -> dict:
    """本地删页 → 线上删页（本地优先；线上哪一版都留在 git 历史里）。"""
    try:
        current = _read_page(rel)
    except SyncError as exc:
        return {"ok": False, "reason": str(exc), "path": rel}
    if current is None:
        return {"ok": True, "path": rel, "deleted": False, "noop": True}
    try:
        head = repo.head_sha(rel)
        res = store.delete_page_sync(
            conn, rel, actor=actor, base_commit=head,
            note=f"从本机同步 {local_sha[:8]}（本地删除）",
        )
    except (store.StoreError, repo.GitError, OSError, ValueError, SyncError) as exc:
        return {"ok": False, "reason": str(exc), "path": rel}
    if res["ok"]:
        return {"ok": True, "path": rel, "deleted": True, "noop": bool(res.get("noop"))}
    note = res["failed"][0]["note"] if res.get("failed") else "这一页没删成"
    return {"ok": False, "reason": note, "path": rel}


def _content_diff(local_ref: str, base_sha: str | None) -> list[tuple[str, str, str | None]]:
    """历史对不上时的兜底：按"本地文件 vs 上次同步时的线上版本"逐个比内容。

    不做删除判定（宁可漏一次删除，也不要把"本地库比线上旧"误判成"本地删了这些页"）；
    新增/改动的页按 A 处理，后面照样走三方合并（内容相同就是 no-op）。
    """
    changes: list[tuple[str, str, str | None]] = []
    for path in repo.ls_tree(local_ref):
        if not _valid_page(path):
            # 派生文件/材料：交给 _split_changes 计数为 ignored（不在这里静默丢掉）
            changes.append(("A", path, None))
            continue
        local_text = repo.try_show_ref(local_ref, path)
        base_text = repo.try_show_ref(base_sha, path) if base_sha else None
        if local_text is not None and local_text != base_text:
            changes.append(("A", path, None))
    return changes


def _changes(
    local_ref: str, last_local_sha: str | None, last_main_sha: str | None,
    head_sha: str | None = None,
) -> list[tuple[str, str, str | None]]:
    """上次同步点..本地分支 HEAD 的变更。

    依次试三个锚点：
    0. 线上当前 `HEAD`：发布脚本成功后本机会 reset 到 main，再从 main 上继续编辑；
       此时分支是 HEAD 的后代，从 HEAD 算 diff 只包含"本机新改动"，不会把上次
       reset 之前的历史（例如引注迁移提交）再重放一遍。
    1. `last_local_sha`：正常发布（分支只追加提交）时是干净的 diff；
    2. `last_main_sha`：发布成功后本机会 `reset --hard origin/main` 并把 outbox 分支
       强制指回 main —— 这时分支是 main 的后代，从 main 算 diff 仍然完整、且能识别删除。
    三个都不是祖先（历史被重写/本地库比线上旧）→ 内容比对兜底，不做删除。
    """
    for anchor in (head_sha, last_local_sha, last_main_sha):
        if not anchor or not repo.try_rev_parse(anchor):
            continue
        try:
            if repo.is_ancestor(anchor, local_ref):
                return repo.diff_status(anchor, local_ref)
        except repo.GitError:
            continue
    return _content_diff(local_ref, last_main_sha)


def _split_changes(
    changes: list[tuple[str, str, str | None]],
) -> tuple[list[str], list[str], int]:
    """变更 → (要重放的页面, 要删的页面, 忽略的 path 数)。"""
    writes: list[str] = []
    deletes: list[str] = []
    ignored = 0
    for status, old, new in changes:
        kind = status[:1]
        if kind == "D":
            if _valid_page(old):
                deletes.append(old)
            else:
                ignored += 1
            continue
        if kind in ("R", "C"):
            if new and _valid_page(new):
                writes.append(new)
            else:
                ignored += 1
            if kind == "R":
                if _valid_page(old):
                    deletes.append(old)
                else:
                    ignored += 1
            continue
        if _valid_page(old):
            writes.append(old)
        else:
            ignored += 1
    # 去重并保持稳定顺序（同一批里 rename 的新旧 path 可能重复出现）
    dedup_writes = list(dict.fromkeys(writes))
    dedup_deletes = [path for path in dict.fromkeys(deletes) if path not in dedup_writes]
    return dedup_writes, dedup_deletes, ignored


def _state(conn: sqlite3.Connection, branch: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM wiki_sync_state WHERE branch = ?", (branch,)
    ).fetchone()


def _save_state(conn: sqlite3.Connection, branch: str,
                last_local_sha: str | None, last_main_sha: str | None) -> None:
    conn.execute(
        "INSERT INTO wiki_sync_state(branch, last_local_sha, last_main_sha, updated_at)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(branch) DO UPDATE SET"
        " last_local_sha=excluded.last_local_sha,"
        " last_main_sha=excluded.last_main_sha,"
        " updated_at=excluded.updated_at",
        (branch, last_local_sha, last_main_sha, utcnow()),
    )


def _record_run(conn: sqlite3.Connection, *, branch: str, local_sha: str | None,
                main_sha: str | None, ok: bool, applied: int, ignored: int,
                failed: int, detail: dict) -> None:
    conn.execute(
        "INSERT INTO wiki_sync_runs(branch, local_sha, main_sha, ok, applied, ignored,"
        " failed, detail_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (branch, local_sha, main_sha, 1 if ok else 0, applied, ignored, failed,
         json.dumps(detail, ensure_ascii=False), utcnow()),
    )


def adopt(
    conn: sqlite3.Connection, *, branch: str, remote: str = "origin",
) -> dict:
    """把一个"已经和线上同源、只是落后"的本地库接入同步（一次性）。

    场景：本机 pm-wiki 是生产 wiki 的 clone，先 `fetch + reset` 对齐到 origin/main；
    这时本地分支是线上历史的祖先、没有独有内容。adopt 只写状态（两个锚点都记为本地
    tip），不重放任何页——随后第一次 `sync` 会把 main 推回裸仓库并刷新状态，发布脚本
    再 reset 本机，之后才是正常的增量发布。

    拒绝本地分支不是线上历史祖先的情况（那说明本地有独有提交，不能静默 adopt）。
    """
    try:
        repo.fetch(remote, "+refs/heads/hermes/*:refs/remotes/hermes/*")
        repo.fetch(remote, "+refs/heads/main:refs/remotes/origin/main")
    except repo.GitError as exc:
        return {"ok": False, "error": f"fetch 失败：{exc}"}
    local_ref = f"refs/remotes/{branch}"
    local_sha = repo.try_rev_parse(local_ref)
    head = repo.try_rev_parse("HEAD")
    if local_sha is None:
        return {"ok": False, "error": f"远端没有这个分支：{branch}"}
    if head is None:
        return {"ok": False, "error": "线上 wiki 还没有任何提交，先建库再来 adopt"}
    try:
        if not repo.is_ancestor(local_sha, head):
            return {
                "ok": False,
                "error": "本地分支不是线上历史的祖先（本地可能有独有提交），不能 adopt；"
                         "先人工确认并合并，或改用正常 sync",
            }
    except repo.GitError as exc:
        return {"ok": False, "error": f"比较历史失败：{exc}"}
    _save_state(conn, branch, local_sha, local_sha)
    _record_run(
        conn, branch=branch, local_sha=local_sha, main_sha=head, ok=True,
        applied=0, ignored=0, failed=0, detail={"adopt": True},
    )
    conn.commit()
    return {
        "ok": True, "adopted": True, "branch": branch,
        "local_sha": local_sha, "main_sha": head,
        "note": "已接入；下一次发布会把 main 推回裸仓库并让本机对齐",
    }


def sync(
    conn: sqlite3.Connection, *, branch: str | None = None, remote: str = "origin",
    dry_run: bool = False, actor: str = DEFAULT_ACTOR,
) -> dict:
    """把本地 outbox 分支重放进线上，返回一份可直接打印/JSON 化的报告。"""
    # 1) fetch 所有 hermes/* outbox 分支与 main（只动 .git/refs，不动工作树）。
    # 第一次发布时远端还没有 main（只有 hermes/<机器>），main 那一条单独 fetch 并容忍失败。
    try:
        repo.fetch(remote, "+refs/heads/hermes/*:refs/remotes/hermes/*")
    except repo.GitError as exc:
        return {"ok": False, "error": f"fetch 失败：{exc}", "dry_run": dry_run}
    try:
        repo.fetch(remote, "+refs/heads/main:refs/remotes/origin/main")
    except repo.GitError:
        pass  # 远端还没有 main：第一次发布，正常

    refs = repo.list_refs("refs/remotes/hermes/")
    if branch is None:
        if len(refs) != 1:
            return {
                "ok": False,
                "error": "没有指定分支，且远端 hermes/* 分支不是恰好一个："
                         f"{', '.join(refs) or '（一个都没有）'}",
                "dry_run": dry_run,
            }
        branch = refs[0].removeprefix("refs/remotes/")
    local_ref = f"refs/remotes/{branch}"
    local_sha = repo.try_rev_parse(local_ref)
    if local_sha is None:
        return {
            "ok": False,
            "error": f"远端没有这个分支：{branch}（先把本地 vault 推到 refs/heads/{branch}）",
            "dry_run": dry_run,
        }

    main_sha_before = repo.try_rev_parse("HEAD")
    state = _state(conn, branch)
    last_local_sha = state["last_local_sha"] if state else None
    base_sha = (state["last_main_sha"] if state else None) or main_sha_before

    writes, deletes, ignored = _split_changes(
        _changes(local_ref, last_local_sha, base_sha, main_sha_before)
    )

    applied = 0
    failures: list[dict] = []
    details: list[dict] = []

    for rel in writes:
        text = repo.try_show_ref(local_ref, rel)
        if text is None:
            failures.append({"path": rel, "reason": "本地分支里读不到这一页"})
            continue
        if dry_run:
            try:
                _parse_page(rel, text)
                details.append({"path": rel, "action": "write"})
                applied += 1
            except ValueError as exc:
                failures.append({"path": rel, "reason": str(exc)})
            continue
        result = _write_page(
            conn, rel, text, base_sha=base_sha, actor=actor, local_sha=local_sha,
        )
        if result["ok"]:
            applied += 1
            details.append({
                "path": rel, "action": "write", "noop": result["noop"],
                "sha": result.get("sha"), "conflicts": result["conflicts"],
            })
        else:
            failures.append({"path": rel, "reason": result["reason"]})

    for rel in deletes:
        if dry_run:
            details.append({"path": rel, "action": "delete"})
            applied += 1
            continue
        result = _delete_page(conn, rel, actor=actor, local_sha=local_sha)
        if result["ok"]:
            applied += 1
            details.append({"path": rel, "action": "delete", "noop": result["noop"]})
        else:
            failures.append({"path": rel, "reason": result["reason"]})

    # 2) 推 main（本地才能 fast-forward 回来）；任何失败都不推进状态
    push_error = ""
    main_sha_after = repo.try_rev_parse("HEAD")
    if not dry_run:
        try:
            repo.push(remote, "main")
        except (repo.GitError, OSError, subprocess.SubprocessError) as exc:
            push_error = f"push 失败：{exc}"
        main_sha_after = repo.try_rev_parse("HEAD")

    ok = not failures and not push_error
    if ok and not dry_run:
        _save_state(conn, branch, local_sha, main_sha_after)
    _record_run(
        conn, branch=branch, local_sha=local_sha, main_sha=main_sha_after,
        ok=ok, applied=applied, ignored=ignored, failed=len(failures),
        detail={"dry_run": dry_run, "push_error": push_error,
                "failures": failures, "changes": details},
    )
    conn.commit()
    return {
        "ok": ok,
        "branch": branch,
        "local_sha": local_sha,
        "main_sha": main_sha_after,
        "applied": applied,
        "ignored": ignored,
        "failed": failures,
        "push_error": push_error,
        "dry_run": dry_run,
        "details": details,
    }
