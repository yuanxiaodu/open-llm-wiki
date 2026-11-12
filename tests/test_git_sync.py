"""服务端自动 git 同步（2026-11-13）。

覆盖：
1. 没有 remote 时 skip，不挡单机 vault / 测试；
2. 本地推上来的页，服务端 pull 后能读到、index 重建、FTS 有命中；
3. 服务端写页后 push，本地能看到；
4. 双方改同一页 → pull 冲突时 abort rebase、报冲突文件，不留在半途；
5. 工作区脏时拒绝 pull（不把未提交状态带进 rebase）。
"""

from __future__ import annotations

import os
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


def test_run_turn_pulls_before_start_and_pushes_after(conn, member, monkeypatch):
    """runner 的接线：回合开始 pull，失败也走封口；finally 仍会 push。"""
    import asyncio

    from app.agent import runner

    calls: list[str] = []

    def fake_pull(_conn):
        calls.append("pull")
        return {"ok": False, "error": "模拟冲突", "conflicted": ["concepts/x.md"]}

    def fake_push(_conn):
        calls.append("push")
        return {"ok": True}

    monkeypatch.setattr(runner.git_sync, "pull_before_turn", fake_pull)
    monkeypatch.setattr(runner.git_sync, "push_after_turn", fake_push)

    asyncio.run(runner.run_turn(
        conn, member.session_id, {"id": member.user_id, "name": member.name},
    ))

    assert calls == ["pull", "push"]
    event = conn.execute(
        "SELECT type, payload_json FROM agent_events WHERE session_id=? ORDER BY seq DESC LIMIT 1",
        (member.session_id,),
    ).fetchone()
    assert event and event["type"] == "error"
    assert "同步失败" in event["payload_json"]


def _git_ok(cwd: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=cwd, check=True, capture_output=True, text=True)


def _seed_bare(tmp_path: Path) -> tuple[Path, Path, Path]:
    """bare + seed（先推 main）+ local clone。"""
    bare = tmp_path / "bare.git"
    _git_ok(tmp_path, "init", "-q", "--bare", str(bare))
    seed = tmp_path / "seed"
    _git_ok(tmp_path, "clone", "-q", str(bare), str(seed))
    _git_ok(seed, "config", "user.email", "seed@local")
    _git_ok(seed, "config", "user.name", "Seed")
    (seed / "concepts").mkdir()
    (seed / "concepts" / "base.md").write_text(_page("基线", "第一版"), encoding="utf-8")
    _git_ok(seed, "add", "-A")
    _git_ok(seed, "commit", "-q", "-m", "base")
    _git_ok(seed, "push", "-q", "origin", "HEAD:refs/heads/main")

    local = tmp_path / "local"
    _git_ok(tmp_path, "clone", "-q", str(bare), str(local))
    _git_ok(local, "config", "user.email", "hermes@local")
    _git_ok(local, "config", "user.name", "Hermes")
    return bare, seed, local


def _run_local_script(local: Path, message: str) -> subprocess.CompletedProcess:
    script = Path(__file__).resolve().parents[1] / "scripts" / "sync-local.sh"
    env = {
        **os.environ,
        "WIKI_LOCAL_VAULT": str(local),
        "WIKI_REMOTE": "origin",
        "WIKI_BRANCH": "main",
        "WIKI_LOCAL_PYTHON": "/nonexistent/python",
    }
    return subprocess.run(
        ("bash", str(script), "-m", message),
        env=env, capture_output=True, text=True, timeout=60,
    )


def test_local_script_commits_pulls_and_pushes(tmp_path):
    bare, _seed, local = _seed_bare(tmp_path)
    rel = "concepts/script-sync.md"
    (local / rel).write_text(_page("脚本同步", "本地先写"), encoding="utf-8")

    result = _run_local_script(local, "本地脚本同步")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "本地先写" in _git_ok_show(bare, f"main:{rel}")
    assert (local / ".gitattributes").is_file()
    assert (local / ".gitignore").is_file()


def test_local_script_stops_on_conflict(tmp_path):
    _bare, seed, local = _seed_bare(tmp_path)
    rel = "concepts/base.md"
    (local / rel).write_text(_page("基线", "本地版本"), encoding="utf-8")
    assert _run_local_script(local, "本地先推").returncode == 0

    _git_ok(seed, "pull", "-q", "--rebase", "origin", "main")
    (seed / rel).write_text(_page("基线", "服务端版本"), encoding="utf-8")
    _git_ok(seed, "commit", "-qam", "server edit")
    _git_ok(seed, "push", "-q", "origin", "HEAD:refs/heads/main")

    (local / rel).write_text(_page("基线", "本地再改"), encoding="utf-8")
    result = _run_local_script(local, "本地再推")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "同步冲突" in result.stderr
    assert not (local / ".git" / "rebase-merge").exists()
    assert not (local / ".git" / "rebase-apply").exists()


def _git_show(cwd: Path, spec: str) -> str:
    return subprocess.run(
        ("git", "show", spec), cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout


def _git_ok_show(cwd: Path, spec: str) -> str:
    return subprocess.run(
        ("git", "show", spec), cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout


def test_push_retries_after_non_fast_forward(pair: GitPair, conn):
    """两端各写一页：先推的一方赢，后推的一方 push 被拒 → pull --rebase → 再推成功。"""
    server_rel = "concepts/server-race.md"
    local_rel = "concepts/local-race.md"
    res = store.write_page_sync(
        conn, path=server_rel, title="服务端竞态页", page_type="concept",
        body_md="服务端写的。", actor="服务器", note="竞态",
    )
    assert res["ok"], res
    _local_commit(pair.local, local_rel, _page("本地竞态页", "本地写的。"), "local race")

    report = git_sync.push(conn, reason="test")

    assert report["ok"] and report.get("retried"), report
    assert _git_show(pair.remote, f"main:{server_rel}") 
    assert _git_show(pair.remote, f"main:{local_rel}")
