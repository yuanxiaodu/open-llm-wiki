import os
import shutil
import subprocess
import threading
from types import SimpleNamespace

import pytest

from app.wiki import repo


# --- 测试自己用的 git 出口：不经过被测模块，免得"用实现验实现" ---


def _git_proc(args, cwd, pin=True):
    """跑一条 git，返回 CompletedProcess（不断言退出码）。

    pin=True 把 GIT_DIR/GIT_WORK_TREE 钉在 cwd 上；pin=False 走 git 自己的发现逻辑
    （造外层仓库、git init 时用）。cwd 一律在 pytest 的 tmp_path / 临时 DATA_DIR 下，
    绝不在项目仓库里。
    """
    env = dict(os.environ)
    if pin:
        env.update(
            {
                "GIT_DIR": str(cwd / ".git"),
                "GIT_WORK_TREE": str(cwd),
                "GIT_PREFIX": "/",
                "GIT_LITERAL_PATHSPECS": "1",
            }
        )
    return subprocess.run(
        ("git", *args),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )


def _raw_git(cwd, *args) -> str:
    proc = _git_proc(args, cwd)
    assert proc.returncode == 0, (args, proc.stdout, proc.stderr)
    return proc.stdout


def _plain_git(cwd, *args) -> str:
    proc = _git_proc(args, cwd, pin=False)
    assert proc.returncode == 0, (args, proc.stdout, proc.stderr)
    return proc.stdout


def test_commit_and_log(tmp_wiki):
    (tmp_wiki / "a.md").write_text("v1\n", encoding="utf-8")
    sha1 = repo.commit("a.md", "wiki|create|a|tester|新建")
    assert sha1
    (tmp_wiki / "a.md").write_text("v2\n", encoding="utf-8")
    sha2 = repo.commit("a.md", "wiki|edit|a|tester|改")
    assert sha2 != sha1
    entries = repo.log("a.md")
    assert [e.action for e in entries] == ["edit", "create"]
    assert repo.show("a.md", sha1) == "v1\n"


def test_commit_noop_returns_none(tmp_wiki):
    (tmp_wiki / "a.md").write_text("v1\n", encoding="utf-8")
    repo.commit("a.md", "wiki|create|a|tester|新建")
    assert repo.commit("a.md", "wiki|edit|a|tester|没改") is None


def test_head_sha_none_for_unknown(tmp_wiki):
    assert repo.head_sha("nope.md") is None


def test_diff_between_two_commits(tmp_wiki):
    (tmp_wiki / "a.md").write_text("v1\n", encoding="utf-8")
    sha1 = repo.commit("a.md", "wiki|create|a|tester|新建")
    (tmp_wiki / "a.md").write_text("v2\n", encoding="utf-8")
    sha2 = repo.commit("a.md", "wiki|edit|a|tester|改")
    out = repo.diff(sha1, sha2, "a.md")
    assert "-v1" in out and "+v2" in out


def test_parse_subject_maxsplit():
    got = repo.parse_subject("wiki|edit|baoming|张三|合并|两段")
    assert got == {"kind": "wiki", "action": "edit", "slug": "baoming", "actor": "张三", "note": "合并|两段"}


# --- 以下为本任务补充：钉住空仓库与管道分隔符的边界，简报代码在这两处会抛 ---


def test_log_entry_fields_and_pipe_free_at(tmp_wiki):
    """%x1f 三字段要拆得开：at 是纯 ISO 时间，note 里的竖线不串位。"""
    (tmp_wiki / "a.md").write_text("v1\n", encoding="utf-8")
    sha = repo.commit("a.md", "wiki|create|a|张三|合并|两段")
    (entry,) = repo.log("a.md")
    assert entry.sha == sha
    assert entry.kind == "wiki"
    assert entry.action == "create"
    assert entry.slug == "a"
    assert entry.actor == "张三"
    assert entry.note == "合并|两段"
    assert "\x1f" not in entry.at and "|" not in entry.at
    from datetime import datetime

    assert datetime.fromisoformat(entry.at)  # %aI 是严格 ISO 8601，能被解析


def test_parse_subject_never_raises():
    for raw in ("", "wiki", "wiki|edit", "|", "|||", "a|b|c|d|e|f|g"):
        got = repo.parse_subject(raw)
        assert set(got) == {"kind", "action", "slug", "actor", "note"}
        assert all(isinstance(v, str) for v in got.values())
    assert repo.parse_subject("") == {
        "kind": "", "action": "", "slug": "", "actor": "", "note": "",
    }
    assert repo.parse_subject("a|b|c|d|e|f|g")["note"] == "e|f|g"


def test_log_limit_and_no_path(tmp_wiki):
    for i in range(3):
        (tmp_wiki / "a.md").write_text(f"v{i}\n", encoding="utf-8")
        repo.commit("a.md", f"wiki|edit|a|tester|第{i}次")
    assert len(repo.log("a.md", limit=2)) == 2
    assert len(repo.log()) == 3
    assert [e.note for e in repo.log("a.md")] == ["第2次", "第1次", "第0次"]


def test_head_sha_tracks_latest_commit_for_path(tmp_wiki):
    (tmp_wiki / "a.md").write_text("v1\n", encoding="utf-8")
    repo.commit("a.md", "wiki|create|a|tester|新建")
    (tmp_wiki / "b.md").write_text("b1\n", encoding="utf-8")
    sha_b = repo.commit("b.md", "wiki|create|b|tester|新建")
    (tmp_wiki / "b.md").write_text("b2\n", encoding="utf-8")
    sha_b2 = repo.commit("b.md", "wiki|edit|b|tester|改")
    assert repo.head_sha("b.md") == sha_b2 != sha_b
    # 别人的提交不动 a.md 的 head
    assert repo.log("a.md")[0].sha != sha_b2


# --- latest_touches：一次 git log 替掉 list_pages 的逐页 N+1 -----------------


def test_latest_touches_maps_each_path_to_its_newest_commit(tmp_wiki):
    (tmp_wiki / "a.md").write_text("v1\n", encoding="utf-8")
    sha_a1 = repo.commit("a.md", "wiki|create|a|张三|新建")
    (tmp_wiki / "b.md").write_text("b1\n", encoding="utf-8")
    sha_b = repo.commit("b.md", "wiki|create|b|李四|新建")
    (tmp_wiki / "a.md").write_text("v2\n", encoding="utf-8")
    sha_a2 = repo.commit("a.md", "wiki|edit|a|王五|改")

    latest = repo.latest_touches()
    assert {path: entry.sha for path, entry in latest.items()} == {
        "a.md": sha_a2,
        "b.md": sha_b,
    }
    assert latest["a.md"].sha != sha_a1
    assert (latest["a.md"].actor, latest["a.md"].action) == ("王五", "edit")
    assert (latest["b.md"].actor, latest["b.md"].action) == ("李四", "create")
    # 与单页 log(limit=1) 的"最近一条"逐字段一致（时间、主题拆出来的字段）
    single = repo.log("a.md", limit=1)[0]
    assert latest["a.md"] == single
    # 没有提交过的 path 不在映射里
    assert "nope.md" not in latest


def test_latest_touches_marks_paths_of_one_multi_path_commit(tmp_wiki):
    """一次提交改多页（store 的一页写会同时落 index/log，迁移也会成对提交）。"""
    (tmp_wiki / "a.md").write_text("a\n", encoding="utf-8")
    (tmp_wiki / "b.md").write_text("b\n", encoding="utf-8")
    sha = repo.commit(["a.md", "b.md"], "wiki|create|a|张三|两页一起")
    latest = repo.latest_touches()
    assert latest["a.md"].sha == latest["b.md"].sha == sha


def test_latest_touches_follows_rename_to_new_path(tmp_wiki):
    """改名提交里新 path 必须能对上（list_pages 的 path 是改后的）。"""
    (tmp_wiki / "old.md").write_text("x\n", encoding="utf-8")
    repo.commit("old.md", "wiki|create|old|张三|新建")
    # 不用 `git mv`：它把改名直接写进暂存区，repo.commit 的 _known_or_present 会认不出
    # 已从索引消失的旧 path；库里真实的改名是"旧文件删掉、新文件放上"再成对提交。
    (tmp_wiki / "old.md").rename(tmp_wiki / "new.md")
    sha = repo.commit(["old.md", "new.md"], "wiki|edit|new|李四|改名")

    latest = repo.latest_touches()
    assert latest["new.md"].sha == sha
    assert (latest["new.md"].actor, latest["new.md"].action) == ("李四", "edit")
    # 改名的源 path 也算这次提交动过（它已不在库里的页目录中，list_pages 不会查它）
    assert latest["old.md"].sha == sha


def test_latest_touches_empty_repo_is_empty(tmp_wiki):
    assert repo.latest_touches() == {}


# --- fix round 1：I1 + R25，身份必须落在 wiki 仓库**自己的** config 上 ---


def test_ensure_repo_sets_identity_on_wiki_repo(tmp_wiki):
    """部署机没有全局 git 身份，提交要能成，靠的是仓库级 user.email/user.name。

    判别性（I1）：只读 `git config user.email` 是读**有效链**，机器上装了全局身份时
    即便本模块一行没写也会通过。这里读 --local、并直接看 .git/config 的字面内容。
    """
    # 仓库 LOCAL 配置就是这两个字面值（不受全局身份影响）
    assert repo._git("config", "--local", "user.email").strip() == "openllmwiki@local"
    assert repo._git("config", "--local", "user.name").strip() == "openllmwiki"
    # 字面出现在 wiki 自己的 .git/config 里
    config_text = (tmp_wiki / ".git" / "config").read_text(encoding="utf-8")
    assert "openllmwiki@local" in config_text and "openllmwiki" in config_text

    # R25：从备份恢复出来的 .git 缺 [user] 段，ensure_repo 也要无条件补回来
    _raw_git(tmp_wiki, "config", "--local", "--unset", "user.email")
    _raw_git(tmp_wiki, "config", "--local", "--unset", "user.name")
    assert "openllmwiki@local" not in (tmp_wiki / ".git" / "config").read_text(encoding="utf-8")
    repo.ensure_repo()  # 已有 .git，不再 init，但身份要写回去
    assert repo._git("config", "--local", "user.email").strip() == "openllmwiki@local"
    assert repo._git("config", "--local", "user.name").strip() == "openllmwiki"

    # 幂等：重复调用不清历史、不改全局配置
    (tmp_wiki / "a.md").write_text("v1\n", encoding="utf-8")
    sha = repo.commit("a.md", "wiki|create|a|tester|新建")
    global_before = _git_proc(("config", "--global", "--list"), tmp_wiki, pin=False).stdout
    repo.ensure_repo()
    repo.ensure_repo()
    global_after = _git_proc(("config", "--global", "--list"), tmp_wiki, pin=False).stdout
    assert global_after == global_before
    assert (tmp_wiki / ".git").exists()
    assert repo.head_sha("a.md") == sha
    assert repo._git("config", "--local", "user.email").strip() == "openllmwiki@local"


def test_fresh_repo_log_and_head_sha_are_empty(tmp_wiki):
    """一个 commit 都没有时：log 空列表、head_sha None，不是 GitError（新装机会走到）。"""
    assert repo.log() == []
    assert repo.log("a.md") == []
    assert repo.head_sha("a.md") is None


# --- fix round 1：I2，红线要判别——外层仓库必须一个字都不动 ---


def _parent_snapshot(parent) -> dict:
    """外层仓库的全部可观察状态：HEAD、提交、reflog、status、索引。"""
    return {
        "head": _plain_git(parent, "rev-parse", "HEAD").strip(),
        "commits": _plain_git(parent, "log", "--format=%H %s"),
        "reflog": _plain_git(parent, "log", "-g", "--format=%H %gs"),
        "status": _plain_git(parent, "status", "--porcelain"),
        "index": _plain_git(parent, "ls-files"),
    }


def test_git_ops_raise_when_wiki_repo_missing(tmp_path, monkeypatch):
    """data/wiki/.git 不在时必须抛 GitError，且**外层仓库完全不受影响**。

    判别性（I2）：只把 wiki 目录放在 tmp_path 下测不出红线——那儿上面没有任何仓库，
    只靠 cwd 的实现也会 fatal。这里在 tmp_path 里造一个外层仓库（与"项目仓库"同形），
    wiki 目录嵌在它里面，提交过一次，然后删掉 wiki/.git 再调四个入口。
    只靠 cwd 兜的实现会顺着外层仓库往上找，把下面的 add/commit **静默提交进外层仓库**；
    钉死 GIT_DIR/GIT_WORK_TREE 的实现只会 fatal。断言外层快照前后逐字相同才算钉住。
    """
    parent = tmp_path / "project"
    wiki = parent / "data" / "wiki"
    wiki.mkdir(parents=True)

    # 外层仓库：一笔提交，作为"绝不能被碰"的基线
    _plain_git(parent, "init", "-q", ".")
    _plain_git(parent, "config", "user.email", "project@local")
    _plain_git(parent, "config", "user.name", "project")
    (parent / "README.md").write_text("项目自己的文件\n", encoding="utf-8")
    _plain_git(parent, "add", "--", "README.md")
    _plain_git(parent, "commit", "-q", "-m", "项目自身的提交")

    # wiki 先是嵌在里面的真仓库（与部署形态一致），随后 .git 消失
    _plain_git(wiki, "init", "-q", ".")
    _plain_git(wiki, "config", "user.email", "openllmwiki@local")
    _plain_git(wiki, "config", "user.name", "openllmwiki")
    (wiki / "old.md").write_text("旧页\n", encoding="utf-8")
    _plain_git(wiki, "add", "--", "old.md")
    _plain_git(wiki, "commit", "-q", "-m", "wiki|create|old|tester|旧页")
    shutil.rmtree(wiki / ".git")
    assert not (wiki / ".git").exists()

    # settings 是 frozen dataclass，改不了实例；换掉 repo 模块看到的那个对象
    monkeypatch.setattr(repo, "settings", SimpleNamespace(wiki_dir=wiki))

    before = _parent_snapshot(parent)
    (wiki / "a.md").write_text("偷渡进外层仓库的内容\n", encoding="utf-8")

    with pytest.raises(repo.GitError):
        repo.head_sha("a.md")
    with pytest.raises(repo.GitError):
        repo.log()
    with pytest.raises(repo.GitError):
        repo.commit("a.md", "wiki|create|a|tester|偷渡")
    with pytest.raises(repo.GitError):
        repo.show("a.md", "deadbeef")

    assert _parent_snapshot(parent) == before  # HEAD/status/reflog/索引全没动
    assert "a.md" not in _plain_git(parent, "ls-files", "--", "data/wiki")
    assert _plain_git(parent, "log", "--format=%s").strip() == "项目自身的提交"


# --- fix round 1：R24，暂存判定 path-scoped + add/commit 整段在锁里 ---


def test_commit_noop_ignores_unrelated_staged_file(tmp_wiki):
    """别处遗留的暂存改动，不能让这一页的 no-op 变成真提交（R24 的 path-scoped）。

    不带 `-- <path>` 的 `git diff --cached --quiet` 看的是**整个索引**：b.md 刚被
    另一个请求 add 完还没 commit，就会让 a.md 的无改动提交变成真提交，顺手把 b.md
    一起提交进 a.md 的主题下。
    """
    (tmp_wiki / "a.md").write_text("v1\n", encoding="utf-8")
    sha = repo.commit("a.md", "wiki|create|a|tester|新建")

    # 手工把 b.md 塞进暂存区但不提交
    (tmp_wiki / "b.md").write_text("b1\n", encoding="utf-8")
    _raw_git(tmp_wiki, "add", "--", "b.md")
    assert _raw_git(tmp_wiki, "diff", "--cached", "--name-only").split() == ["b.md"]

    assert repo.commit("a.md", "wiki|edit|a|tester|没改") is None
    assert repo.head_sha("a.md") == sha
    assert repo.head_sha("b.md") is None  # b.md 没被顺手提交
    assert [e.sha for e in repo.log()] == [sha]  # HEAD 没动
    assert _raw_git(tmp_wiki, "diff", "--cached", "--name-only").split() == ["b.md"]

    # a.md 真改了才提交，且只带走 a.md（b.md 仍留在暂存区）
    (tmp_wiki / "a.md").write_text("v2\n", encoding="utf-8")
    sha2 = repo.commit("a.md", "wiki|edit|a|tester|改了")
    assert sha2 != sha
    assert _raw_git(tmp_wiki, "show", "--name-only", "--format=", sha2).split() == ["a.md"]
    assert _raw_git(tmp_wiki, "diff", "--cached", "--name-only").split() == ["b.md"]


def test_commit_records_deletion(tmp_wiki):
    """带 pathspec 的 commit 也要记得住删除（revert/删页走的是同一条 commit）。

    `git commit -- <path>` 取的是工作区内容，文件已经不在工作区时最容易出问题，
    所以单独钉一条：删掉文件后 commit 返新 sha、head_sha 指向这笔、旧版本仍可 show。
    """
    (tmp_wiki / "a.md").write_text("v1\n", encoding="utf-8")
    sha1 = repo.commit("a.md", "wiki|create|a|tester|新建")
    (tmp_wiki / "a.md").unlink()
    sha2 = repo.commit("a.md", "wiki|delete|a|tester|删页")
    assert sha2 and sha2 != sha1
    assert repo.head_sha("a.md") == sha2
    assert [e.action for e in repo.log("a.md")] == ["delete", "create"]
    assert repo.show("a.md", sha1) == "v1\n"  # 历史没丢
    assert not (tmp_wiki / "a.md").exists()


def test_commit_runs_under_module_lock(tmp_wiki):

    """commit 的 add→查暂存→commit→取 sha 整段在模块级锁里（R24）。

    判别性：主线程先占住这把锁，另一线程的 commit 必须**卡住**；
    没上锁的实现会在几十毫秒内提交完，这条断言就红。
    """
    (tmp_wiki / "a.md").write_text("v1\n", encoding="utf-8")
    done: list = []
    assert repo._INDEX_LOCK.acquire(blocking=False)
    worker = threading.Thread(
        target=lambda: done.append(repo.commit("a.md", "wiki|create|a|tester|新建"))
    )
    try:
        worker.start()
        worker.join(timeout=1.5)
        assert worker.is_alive(), "commit 没在模块锁里跑：主线程占着锁它却也提交了"
        assert done == []
    finally:
        repo._INDEX_LOCK.release()
    worker.join(timeout=60)
    assert not worker.is_alive()
    assert done and done[0]
    assert repo.head_sha("a.md") == done[0]  # 返回的 sha 就是自己这笔


def test_parallel_commits_never_mix_subjects(tmp_wiki):
    """8 个线程各提交自己那一页：不报错、主题与实际改动的文件一一对应。

    T5 的 asyncio 锁按 slug 分，跨 slug 的并发写经 to_thread 进线程池会同时打到这一份索引上。
    没有 R24 的锁，这里会撞 index.lock（GitError）或把别人 staged 的文件带进自己的提交。
    """
    total = 8
    shas: dict = {}
    errors: list = []

    def worker(i: int) -> None:
        name = f"p{i}.md"
        try:
            (tmp_wiki / name).write_text(f"第{i}页正文\n", encoding="utf-8")
            shas[name] = repo.commit(name, f"wiki|create|p{i}|tester|第{i}页")
        except Exception as exc:  # noqa: BLE001 - 线程里的异常要带回主线程断言
            errors.append(f"{name}: {exc!r}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(total)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not any(t.is_alive() for t in threads)
    assert errors == []
    assert all(shas.values()) and len(set(shas.values())) == total

    entries = repo.log(limit=total + 10)
    assert len(entries) == total
    assert {e.slug for e in entries} == {f"p{i}" for i in range(total)}
    for entry in entries:
        changed = _raw_git(
            tmp_wiki, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", entry.sha
        ).split()
        # 每笔提交只改了它主题里说的那一页，sha 也正好是那个线程拿到的
        assert changed == [f"{entry.slug}.md"], (entry.slug, changed)
        assert shas[f"{entry.slug}.md"] == entry.sha
        assert repo.head_sha(f"{entry.slug}.md") == entry.sha


# --- fix round 1：R26，路径里的 glob 魔法字符按字面解释 ---


def test_glob_magic_path_is_literal(tmp_wiki):
    """slug 里出现 [ ] * 时不能被当成 glob 串到别的页上（R26）。

    判别性：造一个诱饵页 a1z.md，它正好匹配 glob `a[1]*.md`。
    不钉 GIT_LITERAL_PATHSPECS 时 head_sha 会返诱饵的 sha、log 会多出一条、
    diff 会凭空出现诱饵的改动。
    """
    magic = "a[1]*.md"
    decoy = "a1z.md"
    (tmp_wiki / magic).write_text("魔法页\n", encoding="utf-8")
    sha_magic = repo.commit(magic, "wiki|create|a1|tester|魔法页")
    (tmp_wiki / decoy).write_text("诱饵页\n", encoding="utf-8")
    sha_decoy = repo.commit(decoy, "wiki|create|a1z|tester|诱饵页")
    assert sha_magic and sha_decoy != sha_magic

    assert repo.head_sha(magic) == sha_magic
    assert [e.sha for e in repo.log(magic)] == [sha_magic]
    assert repo.show(magic, sha_magic) == "魔法页\n"
    assert repo.diff(sha_magic, sha_decoy, magic) == ""  # 这两笔之间魔法页没变
    assert repo.head_sha(decoy) == sha_decoy
    assert repo.show(decoy, sha_decoy) == "诱饵页\n"


# --- fix round 1：R27 + R26 的环境变量，钉在唯一出口 _run 上 ---


def test_subprocess_calls_pin_utf8_and_wiki_env(tmp_wiki, monkeypatch):
    """text 模式的子进程一律显式 utf-8（R27），env 一律钉死 wiki 仓库 + 字面 pathspec（R26）。

    判别性：部署机的 systemd unit 不设 LANG，C locale 下 text=True 会按 ASCII 解码，
    show() 取中文正文直接 UnicodeDecodeError；开发机是 UTF-8，光看功能测不出来，
    所以直接把传给 subprocess.run 的 kwargs 抓下来断言。
    """
    calls: list = []
    real_run = subprocess.run

    def spy(cmd, **kwargs):
        calls.append(kwargs)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(repo.subprocess, "run", spy)

    (tmp_wiki / "中文页.md").write_text("第一版中文正文\n", encoding="utf-8")
    sha = repo.commit("中文页.md", "wiki|create|zhong-wen-ye|tester|新建")
    assert repo.show("中文页.md", sha) == "第一版中文正文\n"
    (tmp_wiki / "中文页.md").write_text("第二版中文正文\n", encoding="utf-8")
    sha2 = repo.commit("中文页.md", "wiki|edit|zhong-wen-ye|tester|改")
    assert "-第一版中文正文" in repo.diff(sha, sha2, "中文页.md")
    assert repo.head_sha("中文页.md") == sha2
    assert repo.log("中文页.md")[0].note == "改"
    repo.ensure_repo()

    assert calls, "一条 git 都没跑，测不出东西"
    text_calls = [c for c in calls if c.get("text")]
    bytes_calls = [c for c in calls if not c.get("text")]
    assert text_calls and bytes_calls  # text 的走 utf-8，bytes 的只看退出码
    assert [c.get("encoding") for c in text_calls] == ["utf-8"] * len(text_calls)
    for c in calls:
        env = c["env"]
        assert env.get("GIT_DIR") == str(tmp_wiki / ".git")
        assert env.get("GIT_WORK_TREE") == str(tmp_wiki)
        assert env.get("GIT_PREFIX") == "/"
        assert env.get("GIT_LITERAL_PATHSPECS") == "1"
        assert c.get("cwd") == tmp_wiki
        assert c.get("timeout") == repo.TIMEOUT_SECONDS


# --- fix round 1：R28，git 静默失败时 GitError 不能是空消息 ---


def test_git_error_message_falls_back_when_git_prints_nothing(tmp_wiki):
    """未设的 config 键：rc=1、stdout/stderr 全空。消息兜底成一句自解释的话（R28）。"""
    _raw_git(tmp_wiki, "config", "--local", "--unset", "user.email")
    silent = _git_proc(("config", "--local", "user.email"), tmp_wiki)
    assert silent.returncode == 1
    assert not silent.stdout.strip() and not silent.stderr.strip()  # 确实是静默失败

    with pytest.raises(repo.GitError) as excinfo:
        repo._git("config", "--local", "user.email")
    assert str(excinfo.value) == "git config 失败 (rc=1)"

    # 有 stderr 时照抄 stderr，不走兜底（排查要看 git 原话）
    with pytest.raises(repo.GitError) as noisy:
        repo._git("definitely-not-a-git-command")
    assert "definitely-not-a-git-command" in str(noisy.value)
    assert "失败 (rc=" not in str(noisy.value)


# --- fix round 1：I3，修订号形状闸（选项注入） ---


@pytest.mark.parametrize(
    "bad",
    [
        "--output=/tmp/openllmwiki-pwned.diff",  # 写文件原语
        "--ext-diff-cmd=/bin/sh",  # 执行程序
        "-v1",
        "-",
        "",
        "HEAD",
        "main",
        "deadbeef extra",
        "0" * 65,
        "café1234",
        "abcd;rm -rf /",
    ],
)
def test_diff_and_show_reject_bad_revisions(tmp_wiki, bad):
    """a/b/sha 落在 git 的选项解析位上，形状不对就 GitError，绝不到 git 手里（I3）。"""
    (tmp_wiki / "a.md").write_text("v1\n", encoding="utf-8")
    sha = repo.commit("a.md", "wiki|create|a|tester|新建")
    with pytest.raises(repo.GitError):
        repo.diff(bad, sha, "a.md")
    with pytest.raises(repo.GitError):
        repo.diff(sha, bad, "a.md")
    with pytest.raises(repo.GitError):
        repo.show("a.md", bad)


def test_diff_cannot_be_turned_into_a_file_write(tmp_wiki, tmp_path):
    """I3 的原始攻击面：`git diff --output=<file>` 是写文件原语，T9 会把 a/b 接成查询参数。

    形状闸必须在 git 之前拦掉，落盘的文件一个字节都不能有。
    """
    target = tmp_path / "pwned.diff"
    (tmp_wiki / "a.md").write_text("v1\n", encoding="utf-8")
    sha = repo.commit("a.md", "wiki|create|a|tester|新建")
    for bad in (f"--output={target}", f"--ext-diff-cmd={target}"):
        with pytest.raises(repo.GitError):
            repo.diff(bad, sha, "a.md")
        with pytest.raises(repo.GitError):
            repo.diff(sha, bad, "a.md")
        with pytest.raises(repo.GitError):
            repo.show("a.md", bad)
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_diff_and_show_accept_full_and_abbreviated_sha(tmp_wiki):
    """形状闸不能过严：整 sha 与十六进制缩写都要照常工作。"""
    (tmp_wiki / "a.md").write_text("v1\n", encoding="utf-8")
    sha1 = repo.commit("a.md", "wiki|create|a|tester|新建")
    (tmp_wiki / "a.md").write_text("v2\n", encoding="utf-8")
    sha2 = repo.commit("a.md", "wiki|edit|a|tester|改")
    for a, b in ((sha1, sha2), (sha1[:7], sha2[:12]), (sha1.upper(), sha2)):
        out = repo.diff(a, b, "a.md")
        assert "-v1" in out and "+v2" in out
    assert repo.show("a.md", sha1[:7]) == "v1\n"
    assert repo._check_rev("abcd") == "abcd"  # 4 位十六进制是下限
    with pytest.raises(repo.GitError):
        repo._check_rev("abc")  # 3 位太短，拦掉


# --- S2-T1（裁定 R50）：repo.unstage 公开化，store._unstage 不再碰 repo._git ---


def test_unstage_clears_staged_path(tmp_wiki):
    """暂存一个文件后调 repo.unstage：暂存区里这一页被撤下来（git diff --cached 为空）。

    这是 R45 那条 commit 失败路径清残局的公开出口。索引是全仓库共享的一份，unstage
    与 commit 的 add/commit 段共用同一把 _INDEX_LOCK，不会与并发提交交错；它只动索引、
    不动工作区（工作区里那份未提交的正文由 store._restore 另行收拾）。
    """
    (tmp_wiki / "a.md").write_text("v1\n", encoding="utf-8")
    repo.commit("a.md", "wiki|create|a|tester|新建")
    # 再改一版，只 add 进暂存区、不 commit
    (tmp_wiki / "a.md").write_text("v2\n", encoding="utf-8")
    _raw_git(tmp_wiki, "add", "--", "a.md")
    assert _raw_git(tmp_wiki, "diff", "--cached", "--name-only").split() == ["a.md"]

    repo.unstage("a.md")

    assert _raw_git(tmp_wiki, "diff", "--cached", "--name-only").split() == []
    # 只撤暂存，不动工作区：v2 仍在工作区里（head 里是 v1）
    assert (tmp_wiki / "a.md").read_text(encoding="utf-8") == "v2\n"
    assert repo.show("a.md", repo.head_sha("a.md")) == "v1\n"


def test_unstage_is_best_effort_and_never_raises(tmp_wiki):
    """git 跑不通（仓库被删）时 unstage 也不抛——它是尽力而为的清理，不能盖掉真正的错因。"""
    (tmp_wiki / "a.md").write_text("v1\n", encoding="utf-8")
    _raw_git(tmp_wiki, "add", "--", "a.md")
    shutil.rmtree(tmp_wiki / ".git")
    repo.unstage("a.md")  # 不抛 GitError / OSError


# --- S5：commit 支持多 path 一次提交（页面 + index.md + log.md 同一个 commit） ---


def test_commit_multiple_paths_in_one_commit(tmp_wiki):
    """一页成功写要同时落页面 + index.md + log.md（计划 §2）：三份一个 sha。"""
    (tmp_wiki / "entities").mkdir()
    (tmp_wiki / "entities" / "a.md").write_text("v1\n", encoding="utf-8")
    (tmp_wiki / "index.md").write_text("索引 v1\n", encoding="utf-8")
    (tmp_wiki / "log.md").write_text("日志 v1\n", encoding="utf-8")
    sha = repo.commit(
        ["entities/a.md", "index.md", "log.md"], "wiki|create|a|tester|新建"
    )
    assert sha
    assert repo.head_sha("entities/a.md") == sha
    assert repo.head_sha("index.md") == sha
    assert repo.head_sha("log.md") == sha
    name_only = _raw_git(
        tmp_wiki, "show", "--name-only", "--format=", sha
    ).split()
    assert sorted(name_only) == ["entities/a.md", "index.md", "log.md"]
    assert repo.show("index.md", sha) == "索引 v1\n"
    assert repo.show("log.md", sha) == "日志 v1\n"

    # 三份都没改 → no-op（返回 None，HEAD 不动）
    assert repo.commit(
        ["entities/a.md", "index.md", "log.md"], "wiki|edit|a|tester|没改"
    ) is None
    assert repo.head_sha("index.md") == sha


def test_commit_multiple_paths_ignores_unrelated_staged_file(tmp_wiki):
    """别处遗留的暂存改动不许被顺手带进这次多 path 提交（R24 的多 path 版）。"""
    (tmp_wiki / "entities").mkdir()
    (tmp_wiki / "entities" / "a.md").write_text("v1\n", encoding="utf-8")
    (tmp_wiki / "index.md").write_text("索引 v1\n", encoding="utf-8")

    (tmp_wiki / "b.md").write_text("b1\n", encoding="utf-8")
    _raw_git(tmp_wiki, "add", "--", "b.md")

    sha = repo.commit(["entities/a.md", "index.md"], "wiki|create|a|tester|新建")
    assert sha
    changed = _raw_git(
        tmp_wiki, "show", "--name-only", "--format=", sha
    ).split()
    assert sorted(changed) == ["entities/a.md", "index.md"]
    assert repo.head_sha("b.md") is None
    # b.md 还在暂存区里，原样留着
    assert _raw_git(tmp_wiki, "diff", "--cached", "--name-only").split() == ["b.md"]


def test_commit_multiple_paths_records_deletion(tmp_wiki):
    """迁移的"旧位置删掉、新位置加上"：删除与新增可以进同一个 commit。"""
    (tmp_wiki / "a.md").write_text("v1\n", encoding="utf-8")
    first = repo.commit("a.md", "wiki|create|a|tester|旧布局")
    (tmp_wiki / "entities").mkdir()
    (tmp_wiki / "entities" / "a.md").write_text("v1\n", encoding="utf-8")
    (tmp_wiki / "a.md").unlink()
    sha = repo.commit(
        ["a.md", "entities/a.md"], "wiki|edit|_migrate_|system|迁移到 LLM-Wiki vault"
    )
    assert sha and sha != first
    # git 会把"同内容换目录"认成 rename（R100）：--name-status 里两边的名字都在
    changed = _raw_git(tmp_wiki, "show", "--name-status", "--format=", sha)
    assert "a.md" in changed and "entities/a.md" in changed
    assert repo.head_sha("entities/a.md") == sha
    # rename 的源名也算"这笔提交碰过它"，所以 a.md 的最近一笔就是这次搬家
    assert repo.head_sha("a.md") == sha
    assert repo.show("a.md", first) == "v1\n"  # 旧路径的历史还在


def test_commit_skips_paths_that_are_neither_tracked_nor_present(tmp_wiki):
    """迁移会交上"从没进过版本库、现在也不在了"的旧文件名：不许 fatal，只当没有。"""
    (tmp_wiki / "a.md").write_text("v1\n", encoding="utf-8")
    # 只有 ghost（不存在也没被跟踪）→ 没有东西可提交
    assert repo.commit("ghost.md", "wiki|edit|a|tester|搬家") is None
    assert repo.log() == []
    # 同一批里既有真改动又有 ghost → ghost 被跳过，真改动照常提交
    sha = repo.commit(["a.md", "ghost.md"], "wiki|create|a|tester|新建")
    assert sha
    assert _raw_git(tmp_wiki, "show", "--name-only", "--format=", sha).split() == ["a.md"]


def test_unstage_accepts_a_list_of_paths(tmp_wiki):
    for name in ("a.md", "index.md"):
        (tmp_wiki / name).write_text(f"{name} v1\n", encoding="utf-8")
    _raw_git(tmp_wiki, "add", "--", "a.md", "index.md")
    assert sorted(_raw_git(tmp_wiki, "diff", "--cached", "--name-only").split()) == [
        "a.md", "index.md",
    ]
    repo.unstage(["a.md", "index.md"])
    assert _raw_git(tmp_wiki, "diff", "--cached", "--name-only").split() == []
    # 只撤暂存，不动工作区
    assert (tmp_wiki / "a.md").read_text(encoding="utf-8") == "a.md v1\n"
