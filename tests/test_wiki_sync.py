"""本机 Hermes → 线上 wiki 的同步器（2026-10-30，本地权威、本地优先）。

覆盖：
1. 第一次发布：本地页全量重放进线上，派生文件（index/log/SCHEMA/raw）不参与；
2. 第二次发布：没有新 commit → 全 no-op；
3. 本地改页 / 删页 / 中文文件名 / 全角括号；
4. 双方改同一页 → 本地优先，线上版进 git 历史；
5. 本地页形状非法 → 该页失败、线上不受影响、其他页照常同步；
6. 失败时状态不推进（下次重试仍能补上）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.config import settings
from app.services import wiki_sync
from app.wiki import repo, store


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ("git", *args), cwd=cwd, capture_output=True, text=True, check=check,
    )


def _page_text(title: str, *, page_type: str, body: str, created="2026-10-01",
               updated="2026-10-02", extra: str = "") -> str:
    return (
        f"---\ntitle: {title}\ncreated: {created}\nupdated: {updated}\n"
        f"type: {page_type}\ntags: [curriculum]\nsources: []\nconfidence: medium\n---\n\n"
        f"{body}\n{extra}"
    )


class LocalVault:
    """一台本机的 Hermes vault：独立的 git 仓库 + 指向 test 的 bare remote。"""

    def __init__(self, root: Path, remote: Path):
        self.root = root
        self.remote = remote
        self.root.mkdir(parents=True, exist_ok=True)
        _git(self.root, "init", "-q", "-b", "main")
        _git(self.root, "config", "user.email", "hermes@local")
        _git(self.root, "config", "user.name", "Hermes")
        _git(self.root, "remote", "add", "origin", str(remote))

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def remove(self, rel: str) -> None:
        (self.root / rel).unlink()

    def publish(self, branch: str = "hermes/mac", message: str = "hermes: batch") -> None:
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", message)
        _git(self.root, "push", "-q", "origin", f"main:refs/heads/{branch}")

    def reset_to_main(self, branch: str = "hermes/mac") -> None:
        """模拟发布脚本成功后的动作：对齐 origin/main + 把 outbox 指回 main。"""
        _git(self.root, "fetch", "-q", "origin", "main")
        _git(self.root, "reset", "--hard", "-q", "origin/main")
        _git(self.root, "push", "-q", "--force", "origin", f"HEAD:refs/heads/{branch}")


@pytest.fixture
def remote(tmp_path) -> Path:
    path = tmp_path / "remote.git"
    subprocess.run(("git", "init", "-q", "--bare", str(path)), check=True)
    return path


@pytest.fixture
def local(remote, tmp_path) -> LocalVault:
    return LocalVault(tmp_path / "hermes-vault", remote)


@pytest.fixture
def server(conn, tmp_wiki, remote):
    """线上工作副本：空 vault + 指向同一个 bare remote 的 origin。"""
    # tmp_wiki 只建了 .git（可能还是 master）：统一成 main，并先有一个提交
    repo._git("symbolic-ref", "HEAD", "refs/heads/main")
    store.ensure_vault()
    repo._git("remote", "add", "origin", str(remote))
    return settings.wiki_dir


def _sync(conn, **kwargs):
    return wiki_sync.sync(conn, branch="hermes/mac", **kwargs)


def test_first_publish_imports_pages_and_ignores_derived_files(conn, server, local):
    local.write("entities/fudan-ai-best.md",
                _page_text("复旦大学AI-BEST课程体系", page_type="entity",
                           body="## 概况\n复旦的 AI 大课。\n\n- [[tsinghua-ai]]"))
    local.write("concepts/ai-ethics.md",
                _page_text("AI伦理", page_type="concept", body="## 现状\n伦理很重要。"))
    # 本机 Hermes 自己维护的派生文件/材料：不该进线上
    local.write("index.md", "# Wiki Index\n\n- 本机自己的索引\n")
    local.write("log.md", "# Wiki Log\n\n## [2026-10-30] x\n")
    local.write("raw/articles/x.md", "---\nsource_title: x\n---\n\nraw\n")
    local.publish()

    report = _sync(conn)
    assert report["ok"], report
    assert report["applied"] == 2
    assert report["ignored"] >= 3

    page = store.get_page(conn, "entities/fudan-ai-best.md")
    assert page and page["title"] == "复旦大学AI-BEST课程体系"
    assert page["created_at"] == "2026-10-01" and page["updated_at"] == "2026-10-02"
    assert store.get_page(conn, "concepts/ai-ethics.md") is not None
    # 派生文件由服务端重建，不是本机那两份
    index_text = (settings.wiki_dir / "index.md").read_text(encoding="utf-8")
    assert "本机自己的索引" not in index_text
    assert "fudan-ai-best" in index_text
    assert "本机自己的索引" not in (settings.wiki_dir / "log.md").read_text(encoding="utf-8")


def test_same_batch_again_is_noop(conn, server, local):
    local.write("entities/a.md", _page_text("甲", page_type="entity", body="正文"))
    local.publish()
    first = _sync(conn)
    assert first["ok"] and first["applied"] == 1

    second = _sync(conn)
    assert second["ok"], second
    assert second["applied"] == 0
    assert second["details"] == []


def test_edit_and_delete_and_chinese_filenames(conn, server, local):
    rel = "entities/中国新闻史.md"
    local.write(rel, _page_text("中国新闻史", page_type="entity", body="第一版"))
    local.publish()
    report = _sync(conn)
    assert report["ok"], report
    assert report["applied"] == 1
    assert store.get_page(conn, rel)["body_md"].strip() == "第一版"

    # 改一页 + 加一页全角括号文件名
    local.write(rel, _page_text("中国新闻史", page_type="entity", body="第二版"))
    rel2 = "concepts/国际传播（英）.md"
    local.write(rel2, _page_text("国际传播（英）", page_type="concept", body="双语"))
    local.publish()
    report = _sync(conn)
    assert report["ok"], report
    assert "第二版" in store.get_page(conn, rel)["body_md"]
    assert store.get_page(conn, rel2) is not None

    # 删页
    local.remove(rel)
    local.publish()
    report = _sync(conn)
    assert report["ok"], report
    assert store.get_page(conn, rel) is None


def test_local_wins_on_same_page_conflict(conn, server, local):
    rel = "entities/conflict.md"
    local.write(rel, _page_text("冲突页", page_type="entity", body="第一段\n\n本地基线"))
    local.publish()
    assert _sync(conn)["ok"]

    # 线上先改（写到正文另一段之外 + 同段），本地再改同段
    head = store.get_page(conn, rel)["head"]
    store.write_page_sync(
        conn, path=rel, title="冲突页", page_type="entity",
        body_md="第一段\n\n线上新增的一段", citations=[], actor="线上用户",
        base_commit=head, note="线上改",
    )
    local.write(rel, _page_text("冲突页", page_type="entity",
                                body="第一段\n\n本地新版本"))
    local.publish()
    report = _sync(conn)
    assert report["ok"], report
    page = store.get_page(conn, rel)
    assert "本地新版本" in page["body_md"]
    assert "线上新增的一段" not in page["body_md"]
    # 线上版还在 git 历史里（版本页可查）
    versions = store.versions(rel)
    assert len(versions) >= 3


def test_invalid_local_page_fails_without_touching_online(conn, server, local):
    good = "entities/good.md"
    bad = "entities/bad.md"
    local.write(good, _page_text("好的页", page_type="entity", body="正文"))
    local.write(bad, "---\ntitle: 坏页\ntype: 不存在的类型\n---\n\n正文")
    local.publish()

    report = _sync(conn)
    assert not report["ok"]
    assert [item["path"] for item in report["failed"]] == [bad]
    assert store.get_page(conn, good) is not None
    assert store.get_page(conn, bad) is None
    # 失败 → 状态不推进：修好后再发一次能补上
    local.write(bad, _page_text("坏页", page_type="entity", body="修好了"))
    local.publish()
    report = _sync(conn)
    assert report["ok"], report
    assert store.get_page(conn, bad) is not None


def test_after_local_reset_edit_and_delete_are_still_detected(conn, server, local):
    """发布成功后本机会 reset 到 origin/main 并把 outbox 指回 main。

    下一次发布必须还能从 last_main_sha 算出编辑与删除（不能因为 outbox 被重写就退化成
    全量 no-op、更不能漏掉删除）。
    """
    local.write("entities/keep.md", _page_text("保留", page_type="entity", body="v1"))
    local.write("entities/gone.md", _page_text("删掉", page_type="entity", body="v1"))
    local.publish()
    assert _sync(conn)["ok"]
    local.reset_to_main()

    local.write("entities/keep.md", _page_text("保留", page_type="entity", body="v2"))
    local.remove("entities/gone.md")
    local.publish()
    report = _sync(conn)
    assert report["ok"], report
    assert report["applied"] == 2, report
    assert "v2" in store.get_page(conn, "entities/keep.md")["body_md"]
    assert store.get_page(conn, "entities/gone.md") is None


def test_after_reset_a_new_sync_does_not_replay_old_commits(conn, server, local):
    """线上在两次发布之间自己提交过（如引注迁移），本机 reset 后再发一次：

    分支已经是线上 HEAD，diff 应该从 HEAD 算（0 页），不能从 last_local_sha 算成
    "重放中间那一批提交"。
    """
    local.write("entities/base.md", _page_text("基础", page_type="entity", body="v1"))
    local.publish()
    assert _sync(conn)["ok"]

    # 线上自己提交一页（模拟迁移/运维批量提交），再让本机对齐
    store.write_page_sync(
        conn, path="entities/online.md", title="线上页", page_type="entity",
        body_md="线上", citations=[], actor="线上", action="create",
    )
    assert _sync(conn)["ok"]          # 把这次线上提交推回裸仓库
    local.reset_to_main()             # 本机 reset 到新的 main

    report = _sync(conn)
    assert report["ok"], report
    assert report["applied"] == 0, report


def test_adopt_aligns_a_clone_without_replaying_pages(conn, server, local):
    """本机是生产 clone、先 reset 到 origin/main 之后：adopt 只写状态，不重放页面。

    adopt 之后第一次发布应该只带"本机新改动"，并且不能把 adopt 之后线上新增的页覆盖掉。
    """
    local.write("entities/base.md", _page_text("基础页", page_type="entity", body="v1"))
    local.publish()
    assert _sync(conn)["ok"]
    local.reset_to_main()   # 本机对齐 origin/main（= 上一次 sync 推上去的 main）

    # 线上又写了一页（本机分支仍是旧 main，是它的祖先）
    store.write_page_sync(
        conn, path="entities/online-only.md", title="线上新增", page_type="entity",
        body_md="线上写的", citations=[], actor="线上用户", action="create",
    )
    # 模拟一次性接入：清掉状态，adopt 不重放
    conn.execute("DELETE FROM wiki_sync_state")
    conn.commit()
    report = wiki_sync.adopt(conn, branch="hermes/mac")
    assert report["ok"] and report["adopted"], report

    local.write("entities/base.md", _page_text("基础页", page_type="entity", body="v2"))
    local.publish()
    report = _sync(conn)
    assert report["ok"], report
    assert report["applied"] == 1, report
    assert "v2" in store.get_page(conn, "entities/base.md")["body_md"]
    assert store.get_page(conn, "entities/online-only.md") is not None


def test_dry_run_writes_nothing(conn, server, local):
    local.write("entities/dry.md", _page_text("干跑", page_type="entity", body="正文"))
    local.publish()
    report = _sync(conn, dry_run=True)
    assert report["ok"] and report["dry_run"]
    assert report["applied"] == 1
    assert store.get_page(conn, "entities/dry.md") is None
    assert conn.execute("SELECT COUNT(*) AS n FROM wiki_sync_state").fetchone()["n"] == 0
