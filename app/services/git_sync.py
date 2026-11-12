"""服务端自动 git 同步（2026-11-13）。

和旧的 `wiki_sync.py` 不同：这里不再逐页重放，也不维护 outbox 分支。服务器就是
一个普通 git writer：

- 每个 agent 回合开始：`git pull --rebase`，拿到本地/服务器的最新页；
- 每个写页：store 自己 commit（页面 + log）；
- 每个回合结束：`git push`；非快进就 pull --rebase 再推一次，冲突就停。

本地 Hermes 端不走这里：它由用户一句话触发 `scripts/sync-local.sh`，同样是标准
`commit → pull --rebase → push`。两端都不需要改 Hermes 原装 llm-wiki 技能。

没有 remote 的单机 vault（以及测试）自动跳过；`WIKI_AUTO_SYNC=0` 可整体关闭。
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import settings
from ..wiki import repo, store

logger = logging.getLogger("openllmwiki.git_sync")

_REMOTE = "origin"
_BRANCH = "main"


def _remote() -> str:
    return settings.wiki_git_remote or _REMOTE


def _branch() -> str:
    return settings.wiki_git_branch or _BRANCH


def _skip(reason: str) -> dict[str, Any]:
    return {"ok": True, "skipped": reason, "remote": _remote(), "branch": _branch()}


def _error(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, "remote": _remote(), "branch": _branch(), **extra}


def _configured() -> bool:
    return bool(settings.wiki_auto_sync) and repo.has_remote(_remote())


def status() -> dict[str, Any]:
    """fetch + 报告 ahead/behind；不改工作区。"""
    if not settings.wiki_auto_sync:
        return _skip("auto-sync-disabled")
    if not repo.has_remote(_remote()):
        return _skip("no-remote")
    try:
        repo.fetch_branch(_remote(), _branch())
        behind, ahead = repo.ahead_behind(_remote(), _branch())
    except (repo.GitError, ValueError) as exc:
        return _error(f"读取远端状态失败：{exc}")
    return {
        "ok": True,
        "remote": _remote(),
        "branch": _branch(),
        "behind": behind,
        "ahead": ahead,
        "dirty": not repo.is_clean(),
        "rebase_in_progress": repo.rebase_in_progress(),
    }


def _refresh_after_pull(conn) -> dict[str, Any]:
    """pull 进来的是普通 git 提交，绕过了 store 的写路径：补生成物与检索。"""
    from ..services import search

    result: dict[str, Any] = {}
    try:
        result["vault"] = store.ensure_vault(conn)
    except (store.StoreError, repo.GitError, OSError, ValueError) as exc:
        result["vault_error"] = f"{type(exc).__name__}: {exc}"
        logger.warning("pull 后重整 vault 失败：%r", exc)
    if conn is not None:
        try:
            result["search"] = search.reindex_all(conn)
            conn.commit()
        except Exception as exc:  # noqa: BLE001 —— 检索是派生数据，失败不该丢页面
            result["search_error"] = f"{type(exc).__name__}: {exc}"
            logger.warning("pull 后重建检索失败：%r", exc)
    return result


def pull(conn=None, *, reason: str = "manual", allow_dirty: bool = False) -> dict[str, Any]:
    """标准 `git pull --rebase`，返回可直接 JSON 化的报告。

    `conn` 有值时同时重建 index/SCHEMA 与 FTS；没有连接时只动 git（CLI 的
    `git-pull` 会传连接）。
    """
    if not settings.wiki_auto_sync:
        return _skip("auto-sync-disabled")
    if not repo.has_remote(_remote()):
        return _skip("no-remote")

    with repo.wiki_write_lock():
        if repo.rebase_in_progress():
            return _error("仓库卡在 rebase/merge 半途，先处理干净再同步",
                          conflicted=repo.conflicted_paths())
        dirty = not repo.is_clean()
        if dirty and not allow_dirty:
            return _error(
                "工作区有未提交改动，拒绝 pull；先 commit 或清理后再同步",
                dirty=True,
            )
        try:
            repo.fetch_branch(_remote(), _branch())
            behind, ahead = repo.ahead_behind(_remote(), _branch())
            if behind == 0:
                return {
                    "ok": True, "reason": reason, "pulled": False,
                    "behind": 0, "ahead": ahead,
                    "remote": _remote(), "branch": _branch(),
                }
            repo.pull_rebase(_remote(), _branch())
        except repo.GitError as exc:
            conflicted = repo.conflicted_paths()
            # 冲突时一定 abort：把工作区留在能继续写页的状态，交给人/agent 处理。
            if conflicted or repo.rebase_in_progress():
                repo.abort_rebase()
            return _error(f"pull 失败：{exc}", conflicted=conflicted)
        except (OSError, ValueError) as exc:
            return _error(f"pull 失败：{exc}")

        refresh = _refresh_after_pull(conn)
        return {
            "ok": True, "reason": reason, "pulled": True,
            "behind": behind, "ahead": 0,
            "remote": _remote(), "branch": _branch(),
            "refresh": refresh,
        }


def push(conn=None, *, reason: str = "manual") -> dict[str, Any]:
    """标准 `git push`；非快进时 pull --rebase 后重试一次，冲突就停。"""
    if not settings.wiki_auto_sync:
        return _skip("auto-sync-disabled")
    if not repo.has_remote(_remote()):
        return _skip("no-remote")

    with repo.wiki_write_lock():
        if repo.rebase_in_progress():
            return _error("仓库卡在 rebase/merge 半途，先处理干净再同步")
        try:
            repo.push_head(_remote(), _branch())
            return {
                "ok": True, "reason": reason, "pushed": True,
                "remote": _remote(), "branch": _branch(),
            }
        except repo.GitError as first:
            # 最常见的原因是别人先推了：pull --rebase 再推一次。网络/权限错误
            # 会在这里第二次失败，原样报出来，不做无限重试。
            logger.info("push 被拒，pull --rebase 后重试：%s", first)
            try:
                repo.fetch_branch(_remote(), _branch())
                repo.pull_rebase(_remote(), _branch())
                repo.push_head(_remote(), _branch())
            except repo.GitError as second:
                conflicted = repo.conflicted_paths()
                if conflicted or repo.rebase_in_progress():
                    repo.abort_rebase()
                return _error(f"push 失败：{second}", conflicted=conflicted)
            except (OSError, ValueError) as exc:
                return _error(f"push 失败：{exc}")
            # rebase 把远端的新页带进来了：索引与检索要跟着刷新，否则下一次
            # pull 看到 behind=0 就不会再补。
            _refresh_after_pull(conn)
            return {
                "ok": True, "reason": reason, "pushed": True, "retried": True,
                "remote": _remote(), "branch": _branch(),
            }


def pull_on_startup(conn) -> dict[str, Any]:
    """启动时 pull；冲突/脏工作区让人看见，网络故障只告警不挡启动。"""
    report = pull(conn, reason="startup")
    if report.get("skipped"):
        logger.info("启动同步跳过：%s", report["skipped"])
    elif report.get("ok"):
        if report.get("pulled"):
            logger.info("启动同步：pull 了 %s 个提交", report.get("behind"))
    elif report.get("conflicted"):
        raise RuntimeError(
            "wiki 仓库同步冲突，服务不能带着冲突启动："
            + "、".join(report["conflicted"])
        )
    else:
        logger.warning("启动同步没做成（继续以本地版本启动）：%s", report.get("error"))
    return report


def pull_before_turn(conn) -> dict[str, Any]:
    """agent 回合开始：先对齐远端，保证这一轮读到的库现状是新的。"""
    return pull(conn, reason="turn-start")


def push_after_turn(conn=None) -> dict[str, Any]:
    """agent 回合结束：把这一轮 store 的提交推回共享仓库；失败只记日志。"""
    report = push(conn, reason="turn-end")
    if not report.get("ok"):
        logger.warning("回合结束 push 失败：%s", report.get("error"))
    return report
