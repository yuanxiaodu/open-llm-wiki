"""服务端自动 git 同步（2026-11-13）。

覆盖：
1. 没有 remote 时 skip，不挡单机 vault / 测试；
2. 本地推上来的页，服务端 pull 后能读到、index 重建、FTS 有命中；
3. 服务端写页后 push，本地能看到；
4. 双方改同一页 → pull 冲突时 abort rebase、报冲突文件，不留在半途；
5. 工作区脏时拒绝 pull（不把未提交状态带进 rebase）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.config import settings
from app.services import git_sync, search
from app.wiki import repo, store


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ("git", *args), cwd=cwd, capture_output=True, text=True, check=True,
    )


def _page(title: str, body: str, *, page_type: str = "concept") -> str:
    return (
        f"---\ntitle: {title}\ncreated: 2026-11-01\nupdated: 2026-11-13\n"
        f"type: {page_type}\ntags: [concept]\nsources: []\n---\n\n{body}\n"
    )


class GitPair:
    """server（settings.wiki_dir） + bare remote + 一个本地 clone。"""

    def __init__(self, server: Path, remote: Path, local: Path):
        self.server = server
        self.remote = remote
        self.local = local


@pytest.fixture
def pair(tmp_wiki, conn, tmp_path) -> GitPair:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(tmp_wiki, "remote", "add", "origin", str(remote))
    store.ensure_vault(conn)
    _git(tmp_wiki, "push", "-q", "-u", "origin", "main")

    local = tmp_path / "local"
    _git(tmp_path, "clone", "-q", str(remote), str(local))
    _git(local, "config", "user.email", "hermes@local")
    _git(local, "config", "user.name", "Hermes")
    return GitPair(tmp_wiki, remote, local)


def _local_commit(local: Path, rel: str, text: str, message: str) -> None:
    path = local / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git(local, "add", "--", rel)
    _git(local, "commit", "-q", "-m", message)
    _git(local, "push", "-q", "origin", "HEAD:refs/heads/main")


def test_no_remote_is_skipped(tmp_wiki):
    report = git_sync.pull(reason="test")
    assert report["ok"] and report["skipped"] == "no-remote"
    assert git_sync.push(reason="test")["skipped"] == "no-remote"


def test_pull_local_page_rebuilds_index_and_search(pair: GitPair, conn):
    rel = "concepts/auto-sync.md"
    _local_commit(pair.local, rel, _page("自动同步", "本地推上来的页。"), "local page")

    report = git_sync.pull(conn, reason="test")

    assert report["ok"] and report["pulled"], report
    page = store.get_page(conn, rel)
    assert page and page["title"] == "自动同步"
    assert (pair.server / "index.md").is_file()
    assert "[[auto-sync|自动同步]]" in (pair.server / "index.md").read_text(encoding="utf-8")
    hits = conn.execute(
        "SELECT ref_id FROM fts_shared WHERE kind='page' AND ref_id=?", (rel,)
    ).fetchall()
    assert [hit["ref_id"] for hit in hits] == [rel]


def test_push_server_page_is_visible_to_local(pair: GitPair, conn):
    rel = "concepts/server-page.md"
    res = store.write_page_sync(
        conn, path=rel, title="服务端页", page_type="concept",
        body_md="服务端写的。", actor="服务器", note="测试",
    )
    assert res["ok"], res

    report = git_sync.push(reason="test")
    assert report["ok"] and report.get("pushed"), report

    _git(pair.local, "pull", "-q", "--rebase", "origin", "main")
    assert (pair.local / rel).is_file()
    assert "服务端写的。" in (pair.local / rel).read_text(encoding="utf-8")


def test_pull_conflict_aborts_and_names_the_file(pair: GitPair, conn):
    rel = "concepts/conflict.md"
    res = store.write_page_sync(
        conn, path=rel, title="冲突页", page_type="concept",
        body_md="第一段\n\n服务端版本", actor="服务器", note="初始",
    )
    assert res["ok"], res
    assert git_sync.push(reason="test")["ok"]

    # 本地从服务端版本 clone 一份，再和服务端改同一行。
    _git(pair.local, "pull", "-q", "--rebase", "origin", "main")
    _local_commit(pair.local, rel, _page("冲突页", "第一段\n\n本地版本"), "local edit")

    head = store.get_page(conn, rel)["head"]
    res = store.write_page_sync(
        conn, path=rel, title="冲突页", page_type="concept",
        body_md="第一段\n\n服务端版本二", actor="服务器", note="服务端再改",
        base_commit=head,
    )
    assert res["ok"], res

    report = git_sync.pull(conn, reason="test")

    assert not report["ok"]
    assert rel in report["conflicted"], report
    assert not repo.rebase_in_progress()
    # abort 之后线上仍是服务端自己的版本，没有被半途 rebase 改坏
    assert "服务端版本二" in store.get_page(conn, rel)["body_md"]


def test_dirty_worktree_refuses_pull(pair: GitPair, conn):
    (pair.server / "dirty.txt").write_text("未提交", encoding="utf-8")
    report = git_sync.pull(conn, reason="test")
    assert not report["ok"] and report.get("dirty")
    (pair.server / "dirty.txt").unlink()
