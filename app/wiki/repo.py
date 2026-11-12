"""data/wiki 这个 git 仓库的子进程包装。版本、diff、回滚全靠它。

全项目只有这里 shell out 到 git。管的是 data/wiki 这个**嵌套**仓库，
不是项目仓库，所以每次调用都把 GIT_DIR/GIT_WORK_TREE 显式钉死（见 _git）。
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import subprocess
import threading
from dataclasses import dataclass

from ..config import settings

TIMEOUT_SECONDS = 60

# git log 的字段分隔符（%x1f）。竖线留给提交主题里的 note，不能拿它当分隔符。
FIELD_SEP = "\x1f"

# 写在 wiki 仓库自己身上的本地配置（部署机没有全局身份也能提交）。
# `maintenance.auto=false` 是 2026-10-30 补的：Git 2.54 在每次 commit 后会**异步**
# 起 `git maintenance run --auto --detach`，它会在 .git 里写临时文件/打包 refs，和我们
# 的写锁、以及「删/建工作树」的运维动作互相踩（测试里表现为 rmtree 与 detached
# maintenance 抢文件）。自动维护对 wiki 仓库没有收益，关掉；需要时手工 `git gc`。
IDENTITY = {
    "user.email": "openllmwiki@local",
    "user.name": "openllmwiki",
    "maintenance.auto": "false",
}

# 修订号的合法形状：4-64 位十六进制（整 sha 或它的缩写）。
# diff/show 把调用方给的修订号放在 git 的**选项解析位**上（`git diff a b -- path`、
# `git show <sha>:<path>`），以 `-` 开头的字符串会被 git 当成选项吃掉：
# `--output=<file>` 是写文件原语、`--ext-diff-cmd=<prog>` 会执行程序。
# T9 把 diff 的 a/b 直接接成 HTTP 查询参数，所以进门先按形状拦掉。
# （选型：正则 fullmatch，不用 `--end-of-options`——顺带把"这不是 sha"变成一句人话。）
REV_RE = re.compile(r"[0-9a-fA-F]{4,64}")

# add → 查暂存 → commit → 取 sha 必须整段原子（裁定 R24）。git 的暂存区是全仓库
# 共享的一份：两个线程交错会互相把对方 staged 的文件带进自己的提交，或者撞 index.lock。
# 模块级一把锁，同进程内所有 commit 共用。
_INDEX_LOCK = threading.Lock()


class GitError(RuntimeError):
    """git 子进程非零退出。消息是 git 的 stderr，给人看的那句由上层翻。"""


# --- 跨进程写锁 --------------------------------------------------------------
#
# 2026-10-30：同步器改成独立进程（`python -m app.cli sync`），会和在线 app 同时写
# 同一个 data/wiki。store 里的 per-path / _CREATE_LOCK / _INDEX_LOCK 都是**进程内**
# 的 threading 锁，挡不住另一个进程；而且同步器要判标题、发 slug、读 head、写三份
# 文件、commit —— 这一整段必须跨进程串行。所以 store 的写入口外面再套这一把：
#   - `_WRITE_LOCK`（RLock）负责同进程内的排队，可重入（revert → write_page 会套两层）；
#   - `wiki_write_lock.lock` + flock 负责跨进程（app / cli / 以后多实例）。
# 锁序：wiki_write_lock → _CREATE_LOCK → per-path 锁 → _INDEX_LOCK。
_WRITE_LOCK = threading.RLock()
_LOCK_STATE = threading.local()


def _lock_path():
    git_dir = settings.wiki_dir / ".git"
    base = git_dir if git_dir.exists() else settings.wiki_dir
    return base / "openllmwiki-write.lock"


@contextlib.contextmanager
def wiki_write_lock():
    """所有会改 data/wiki 的操作共用的跨进程排他锁（可重入）。

    持锁期间会跑 git 子进程（add/diff/commit），最长受 TIMEOUT_SECONDS 约束；这是
    "7 人规模、单机"的取舍——把并发写整段串起来，换掉"index.md/log.md 的读改写互相
    踩、跨进程撞 index.lock"这两类静默损坏。
    """
    with _WRITE_LOCK:
        depth = getattr(_LOCK_STATE, "depth", 0)
        fd = None
        if depth == 0:
            path = _lock_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError:
                os.close(fd)
                raise
        _LOCK_STATE.depth = depth + 1
        try:
            yield
        finally:
            _LOCK_STATE.depth = depth
            if depth == 0 and fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)


def rebase_in_progress() -> bool:
    """版本库是否卡在 rebase/merge 半途（2026-10-30 的守卫）。

    旧的双向同步用 `git pull --rebase`，冲突会留下 `.git/rebase-merge`。那种状态下
    应用写页会提交到 detached HEAD（分支引用不动），人工 abort 后页就丢了。同步改成
    "本地分支 → 服务端逐页重放"之后，正常流程里不会再出现 rebase；这里只做**拒绝写**
    的兜底：检测到就报错，让人先把版本库处理干净。
    """
    git_dir = settings.wiki_dir / ".git"
    for name in ("rebase-merge", "rebase-apply", "MERGE_HEAD"):
        if (git_dir / name).exists():
            return True
    return False


@dataclass
class LogEntry:
    sha: str
    kind: str
    action: str
    slug: str
    actor: str
    note: str
    at: str


def _wiki_env() -> dict[str, str]:
    """把子进程的 git 钉在 data/wiki 这个仓库上的环境变量。"""
    wiki_dir = settings.wiki_dir
    return {
        **os.environ,
        "GIT_DIR": str(wiki_dir / ".git"),
        "GIT_WORK_TREE": str(wiki_dir),
        "GIT_PREFIX": "/",
        # slug 里出现 * [ ? 时按字面路径解释，不做 glob（裁定 R26）。
        # 否则 `a[1]*.md` 这种页会串到同形状的别的页上（head_sha/log/diff 全串）。
        "GIT_LITERAL_PATHSPECS": "1",
        # 自动同步的 git 子进程不许停下来等输入：没有终端可答凭据/编辑器提示，
        # 等下去就是 systemd 里挂死。凭据走部署机的 ssh key 或本地 file remote。
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_EDITOR": "true",
        "GIT_SEQUENCE_EDITOR": "true",
    }


def _run(args: tuple[str, ...], *, text: bool) -> subprocess.CompletedProcess:
    """所有 git 子进程调用的唯一出口。

    text 模式一律显式 `encoding="utf-8"`（裁定 R27）：部署机的 systemd unit 不设 LANG，
    C locale 下 text=True 会按 ASCII 解码，show() 取中文正文直接 UnicodeDecodeError。
    只看退出码的调用（_has_staged_change）走 bytes，不解码也就没这回事。
    """
    return subprocess.run(
        ("git", *args),
        cwd=settings.wiki_dir,
        env=_wiki_env(),
        capture_output=True,
        text=text,
        encoding="utf-8" if text else None,
        timeout=TIMEOUT_SECONDS,
    )


def _failure(args: tuple[str, ...], proc: subprocess.CompletedProcess) -> GitError:
    """非零退出翻成 GitError。

    stderr/stdout 都空时兜底一句自解释的话（裁定 R28）：git 有一类失败是静默的
    （`git config user.email` 未设时 rc=1 且什么都不打印），上层 T5/T9 要拿这个消息
    翻人话，不能拿到空字符串。
    """
    stderr, stdout = proc.stderr, proc.stdout
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", "replace")
    detail = (stderr or "").strip() or (stdout or "").strip()
    if not detail:
        detail = f"git {args[0]} 失败 (rc={proc.returncode})"
    return GitError(detail)


def _git(*args: str) -> str:
    """在 wiki 仓库里跑一条 git，非零退出抛 GitError。

    data/wiki 是嵌在项目仓库里的另一个仓库，而 git 找仓库是从 cwd 往上找的：
    一旦 data/wiki/.git 不在（被删、路径配错），cwd 兜不住，下面的 add/commit
    就会静默改进项目仓库。所以这里用 GIT_DIR + GIT_WORK_TREE 把操作钉死在
    wiki 仓库上：.git 不在就直接 fatal，翻成 GitError，绝不殃及项目仓库。
    GIT_PREFIX=/ 让调用方传的相对路径按仓库根解析，与 cwd 无关。
    """
    proc = _run(args, text=True)
    if proc.returncode != 0:
        raise _failure(args, proc)
    return proc.stdout


def ensure_repo() -> None:
    """建目录 + 首次 git init，并**无条件**把提交身份写进 wiki 仓库自己的 config（裁定 R25）。

    部署机没有全局 user.email/user.name，缺这两条 commit 会直接失败。
    无条件写（不是只在建库时写）：从备份/镜像恢复出来的 .git 可能缺 [user] 段，
    只在建库时写就永远补不回来。git config 写同值幂等，重复调用不动历史。
    """
    settings.wiki_dir.mkdir(parents=True, exist_ok=True)
    if not (settings.wiki_dir / ".git").exists():
        _git("init", "-q")
    for key, value in IDENTITY.items():
        _git("config", "--local", key, value)


def _has_head() -> bool:
    """仓库里有没有提交。刚 init 完 HEAD 是未生的，此时 log/rev-list 都会 fatal。

    三态：rc==0 且有输出=有提交（True）；rc==1=有效仓库但一个提交都没有
    （未生 HEAD，False）；其它非零（如 128=根本不是 git 仓库）是真错误，抛出去，
    不能静默当成空仓库——那会把 DATA_DIR 配错一类问题藏起来。
    """
    args = ("rev-parse", "-q", "--verify", "HEAD")
    proc = _run(args, text=True)
    if proc.returncode == 0 and proc.stdout.strip():
        return True
    if proc.returncode == 1:
        return False
    raise _failure(args, proc)


def tracked(paths: str | list[str] | tuple[str, ...]) -> list[str]:
    """这些 path 里已经被版本库跟踪的那些（相对仓库根）。"""
    files = _paths(paths)
    out = _git("ls-files", "--", *files)
    return [line for line in out.splitlines() if line]


def track(paths: str | list[str] | tuple[str, ...], subject: str) -> str | None:
    """把工作区里存在的文件强制加入版本库并提交（可越过 .gitignore）。

    2026-11-13：SCHEMA.md / index.md 是 wiki 自己的文件，必须跟页面一起同步；
    旧 vault 的 .gitignore 可能还列着它们，所以 re-track 要 `-f`。
    """
    files = [name for name in _paths(paths) if (settings.wiki_dir / name).exists()]
    if not files:
        return None
    with _INDEX_LOCK:
        _git("add", "-f", "--", *files)
        if not _has_staged_change(files):
            return None
        _git("commit", "-q", "-m", subject, "--", *files)
        return _git("rev-parse", "HEAD").strip()


def current_branch() -> str:
    """当前分支名；detached HEAD 返回 "HEAD"。"""
    return _git("rev-parse", "--abbrev-ref", "HEAD").strip()


def has_remote(name: str) -> bool:
    """remote 是否存在；没有 remote 的测试/单机 vault 让自动同步直接跳过。"""
    try:
        out = _git("remote")
    except (GitError, subprocess.TimeoutExpired, OSError):
        return False
    return name in out.split()


def is_clean() -> bool:
    """工作区（含未跟踪文件）是否干净。"""
    return not _git("status", "--porcelain").strip()


def status_short() -> str:
    """`git status --porcelain` 原文；同步报告的诊断用。"""
    return _git("status", "--porcelain")


def fetch_branch(remote: str, branch: str) -> None:
    """fetch 一个分支到 `refs/remotes/<remote>/<branch>`，不动工作树。"""
    _check_ref(remote)
    _check_ref(branch)
    _git("fetch", "--prune", remote, f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}")


def ahead_behind(remote: str, branch: str) -> tuple[int, int]:
    """(behind, ahead)：本地 HEAD 相对 `<remote>/<branch>` 落后/领先几个提交。"""
    _check_ref(remote)
    _check_ref(branch)
    out = _git("rev-list", "--left-right", "--count", f"{remote}/{branch}...HEAD").strip()
    behind, ahead = out.split()
    return int(behind), int(ahead)


def pull_rebase(remote: str, branch: str) -> None:
    """标准 `git pull --rebase`：有本地提交就 rebase 到远端，没有就快进。"""
    _check_ref(remote)
    _check_ref(branch)
    _git("pull", "--rebase", "--no-stat", remote, branch)


def push_head(remote: str, branch: str) -> None:
    """把当前 HEAD 推到远端分支；非快进会抛 GitError，由同步层决定 pull+重试。"""
    _check_ref(remote)
    _check_ref(branch)
    _git("push", remote, f"HEAD:refs/heads/{branch}")


def abort_rebase() -> None:
    """尽力 abort 一个卡住的 rebase；没有 rebase 时静默。"""
    try:
        _git("rebase", "--abort")
    except (GitError, subprocess.TimeoutExpired, OSError):
        pass


def conflicted_paths() -> list[str]:
    """rebase/merge 冲突的文件（相对仓库根）。"""
    return [
        line for line in _git("diff", "--name-only", "--diff-filter=U").splitlines()
        if line
    ]


def head_sha(path: str) -> str | None:
    """这一页最后一次提交的 sha；没提交过返 None。"""
    if not _has_head():
        return None
    out = _git("rev-list", "-n", "1", "HEAD", "--", path).strip()
    return out or None


def _paths(paths: str | list[str] | tuple[str, ...]) -> list[str]:
    """把 str / list 统一成 list（commit 从单 path 升级成多 path，旧调用方零改动）。"""
    if isinstance(paths, str):
        return [paths]
    return [str(path) for path in paths]


def commit(paths: str | list[str] | tuple[str, ...], subject: str) -> str | None:
    """暂存这些 path 并**一次**提交，返回新 sha；全都没有改动返 None（store 的 no-op 靠这个）。

    S5 起一个成功的页面写要同时落三份：页面 + 自动重写的 index.md + 追加的 log.md
    （计划 §2「同一次 commit」），所以这里收 path 清单而不是单页。单 path 的旧调用
    （tests、revert、cli）也照样收。

    整段 add → 查暂存 → commit → 取 sha 在模块级锁里（裁定 R24）：暂存区是全仓库
    共享的一份，跨 slug 的并发提交经 to_thread 进线程池会交错。取 sha 也在锁内，
    返回的一定是**本次**提交，不会是别人刚提交的那个。

    三处都是 path-scoped：查暂存带 `-- <paths...>`，commit 也带 `-- <paths...>`。
    光查暂存不够——裸 `git commit` 提交的是**整个索引**，别处遗留的暂存改动
    （上一个请求崩在 add 之后、或有人在 data/wiki 里手工 git add）会被顺手
    提交进这一页的主题下。带 pathspec 的 commit 只收这几份，其余暂存原样留着。
    """
    files = _paths(paths)
    with _INDEX_LOCK:
        # 迁移会提交"从旧位置删掉、新位置加上"的成对 path：旧文件若从没进过版本库
        # （手工放进来的、上一次迁移没提交完的），对它 `git add` 会 fatal
        # （pathspec did not match）。已在库里（被跟踪）或工作区还在的才交出去。
        files = [name for name in files if _known_or_present(name)]
        if not files:
            return None
        _git("add", "--", *files)
        if not _has_staged_change(files):
            return None
        _git("commit", "-q", "-m", subject, "--", *files)
        return _git("rev-parse", "HEAD").strip()


def _known_or_present(path: str) -> bool:
    """这个 path 交不交得进 git：工作区在，或已经被版本库跟踪（删除也算改动）。"""
    if (settings.wiki_dir / path).exists():
        return True
    return bool(_git("ls-files", "--", path).strip())


def unstage(paths: str | list[str] | tuple[str, ...]) -> None:
    """把这些 path 从暂存区撤下来（尽力而为，不抛）。裁定 R50：R45 清残局的公开出口。

    收 str 或 list：页面写失败时页面 + index.md + log.md 三份都要撤（一次调用）。

    repo.commit 是 add → 查暂存 → commit → 取 sha 四步，中间任何一步挂掉（git 非零退出、
    子进程 60s 超时、磁盘满）都会把这一页留在暂存区里。索引是全仓库共享的一份：留着它，
    `git status` 从此一直脏（巡检与备份看到的不再是干净工作区），而运维手工一次不带
    pathspec 的 `git commit` 就会把这一页塞进别人的提交主题下。store._unstage 在 commit
    段的失败路径上调这里，不再自己碰 repo._git。

    用 `git reset -q -- <path>` 而不是 `git restore --staged -- <path>`：两者语义相同，
    但 restore 要解析 HEAD，**一个提交都没有的仓库**上直接 fatal（rc=128）——而那正是
    "开站后写第一页时 commit 挂了"的场景；reset 在没有 HEAD 时把索引条目删掉、在 HEAD
    里有这一页时把索引条目退回 HEAD 那一份，两种都对。与 commit 共用同一把 _INDEX_LOCK，
    不与并发提交交错。撤不下来（git 本身跑不通）也不抛：紧随其后的 _restore 与 failed
    已经把真正的错因说明白，再抛一层只会把它盖掉。
    """
    with _INDEX_LOCK:
        try:
            _git("reset", "-q", "--", *_paths(paths))
        except Exception:  # noqa: BLE001 - 尽力而为：GitError / TimeoutExpired / OSError 一律不冒
            pass


def _has_staged_change(paths: str | list[str]) -> bool:
    """这些 path 在暂存区里跟 HEAD 是否不同。

    `git diff --cached --quiet -- <paths...>` 的退出码就是答案：0=没改动，1=有改动。
    必须带 `-- <paths...>`（裁定 R24）：不带 path 时，别处遗留的暂存改动会让这一页的
    no-op 变成真提交，顺手把别人的文件一起提交进来。
    这里的 1 是有意义的返回，不能走 _git（会被当成错误抛 GitError）；
    0/1 之外的退出码（如 128=不是仓库）是真错误，照样抛。
    """
    files = _paths(paths)
    args = ("diff", "--cached", "--quiet", "--", *files)
    proc = _run(args, text=False)
    if proc.returncode in (0, 1):
        return proc.returncode == 1
    raise _failure(args, proc)  # bytes 的 stderr 由 _failure 自己解码


def log(path: str | None = None, limit: int = 200) -> list[LogEntry]:
    """按时间倒序取提交记录；给了 path 只要这一页的。空仓库返空列表。"""
    if not _has_head():
        return []
    args = ["log", f"--max-count={limit}", "--format=%H%x1f%s%x1f%aI"]
    if path:
        # --follow：批量改名（2026-10-30 的 158 页 page-日期→拼音）之后，改名后的页
        # 仍能把历史追到旧文件名；不加的话版本页只剩那条迁移提交。
        args += ["--follow", "--", path]
    out = _git(*args)
    entries = []
    for line in out.splitlines():
        sha, subject, at = line.split(FIELD_SEP, 2)
        entries.append(LogEntry(sha=sha, at=at, **parse_subject(subject)))
    return entries


def latest_touches() -> dict[str, LogEntry]:
    """每个 path 最近一条提交；一次 `git log` 扫全历史，新到旧取首次出现。

    list_pages 的目录页过去对**每一页**跑一趟 `git log -1 --follow`：287 页 × ~21ms
    ≈ 6s 一次目录请求，智能体的 list_wiki 工具也跟着付这笔钱。这里换成一条
    `git log --name-status -M`：输出顺序是提交从新到旧、每个提交下面列它改过的
    path，所以一个 path 第一次出现时的那个提交，就是"最近一次改了这一页"的提交——
    与 `log(path, limit=1)` 的口径逐个相同。`-M` 让改名提交同时带新旧两个 path：
    改名后的页在改名提交里以新 path 出现，与 `--follow` 追改名的结果是同一个。

    这个函数只服务"每页要最近一条"的 list_pages；单页的版本历史仍走 log(path)
    （那里要 --follow 追旧文件名，也只需要一趟子进程）。

    空仓库（没有 HEAD）返 {}，与 log() 的空列表同口径。
    """
    if not _has_head():
        return {}
    out = _git("log", "--format=%H%x1f%s%x1f%aI", "--name-status", "-M")
    latest: dict[str, LogEntry] = {}
    entry: LogEntry | None = None
    for line in out.splitlines():
        if FIELD_SEP in line:
            sha, subject, at = line.split(FIELD_SEP, 2)
            entry = LogEntry(sha=sha, at=at, **parse_subject(subject))
            continue
        if entry is None or not line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        if status[:1] == "R" and len(parts) >= 3:
            # 改名：新 path 是老 path 的后继，两边都算"被这次提交动过"。
            # （C### 是复制，源 path 没动，只认 parts[-1]。）
            latest.setdefault(parts[1], entry)
            latest.setdefault(parts[2], entry)
        else:
            # A/M/D/T 后面一个 path；C### 后面是 old、new 两个 path，取 new。
            latest.setdefault(parts[-1], entry)
    return latest


def show(path: str, sha: str) -> str:
    """某个提交里这一页的全文。回滚就是拿它重写工作区再 commit。"""
    return _git("show", f"{_check_rev(sha)}:{path}")


def diff(a: str, b: str, path: str) -> str:
    """两个提交之间这一页的 unified diff；没差异返空串（退出码 0，不抛）。"""
    return _git("diff", _check_rev(a), _check_rev(b), "--", path)


def _check_rev(rev: str) -> str:
    """修订号形状闸：只放 4-64 位十六进制，其余一律 GitError。

    安全考虑见 REV_RE 的注释。代价：`HEAD`、分支名、`<sha>^` 这类符号修订也被拒——
    本模块的调用方（T5 回滚、T9 版本对比）只用 log()/head_sha() 给出的 sha。
    """
    if not isinstance(rev, str) or not REV_RE.fullmatch(rev):
        raise GitError(f"不是合法的 git 修订号：{rev!r}")
    return rev


def parse_subject(subject: str) -> dict:
    """拆 `{kind}|{action}|{slug}|{actor}|{note}`。

    maxsplit=4：note 自己可以含竖线。竖线不足 4 个也不抛，缺的补空串。
    """
    kind, action, slug, actor, note = (subject.split("|", 4) + [""] * 5)[:5]
    return {"kind": kind, "action": action, "slug": slug, "actor": actor, "note": note}


# --- 同步器用的 git 原语（2026-10-30） ---------------------------------------
#
# 同步走的是"本地分支当 outbox，服务端逐页重放"：只需要 fetch、读某个 ref 的文件、
# 看两个 ref 之间改了哪些 path、push main。这些都不改工作树，所以不拿 wiki_write_lock；
# 真正的写页还是要过 store 的锁。

# ref 允许的形状：分支名/远端跟踪名/HEAD/完整 sha。挡掉选项注入（前导 -）与
# git 会特殊解释的字符（空格、冒号、问号、星号、反斜杠、控制字符）。
_REF_RE = re.compile(r"^(?!-)[A-Za-z0-9_./~^-]+$")


def _check_ref(ref: str) -> str:
    if not isinstance(ref, str) or not _REF_RE.fullmatch(ref or ""):
        raise GitError(f"不是合法的 git ref：{ref!r}")
    return ref


def fetch(remote: str, *refspecs: str) -> None:
    """拉远端 ref（只写 .git/refs，不动工作树）。refspec 由调用方拼，不含空格。"""
    _check_ref(remote)
    _git("fetch", "--prune", remote, *refspecs)


def rev_parse(ref: str) -> str:
    """ref → 完整 sha；不存在时抛 GitError（rc=128）。"""
    return _git("rev-parse", "--verify", _check_ref(ref)).strip()


def try_rev_parse(ref: str) -> str | None:
    """ref → sha；ref 不存在（或还不是一个提交）返 None，不抛。"""
    args = ("rev-parse", "--verify", "-q", _check_ref(ref))
    proc = _run(args, text=True)
    if proc.returncode == 0:
        return proc.stdout.strip() or None
    return None


def head_commit() -> str:
    """当前 HEAD 的完整 sha（仓库必须已有提交；同步器要它做基线）。"""
    return _git("rev-parse", "HEAD").strip()


def is_ancestor(older: str, newer: str) -> bool:
    """older 是不是 newer 的祖先（含相等）。"""
    args = ("merge-base", "--is-ancestor", _check_ref(older), _check_ref(newer))
    proc = _run(args, text=False)
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    raise _failure(args, proc)


def ls_tree(ref: str) -> list[str]:
    """某个 ref 里的全部文件（相对路径），按 git 的排序。"""
    # -c core.quotepath=false：中文文件名默认会被 git 转义成 "\344\270\255..."，
    # 同步器按 path 找本地 blob 时会对不上。
    out = _git("-c", "core.quotepath=false", "ls-tree", "-r", "--name-only", _check_ref(ref))
    return [line for line in out.splitlines() if line]


def diff_status(old: str, new: str) -> list[tuple[str, str, str | None]]:
    """old..new 的变更：[("A"/"M"/"D"/"R###"/"C###"/"T", old_path, new_path|None)]。

    带 `-M` 认改名：R 给 (R, 旧, 新)，其余给 (状态, path, None)。只列文件，不列子模块。
    """
    out = _git("-c", "core.quotepath=false", "diff", "--name-status", "-M",
               _check_ref(old), _check_ref(new), "--")
    changes: list[tuple[str, str, str | None]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        if status[:1] in ("R", "C") and len(parts) >= 3:
            changes.append((status, parts[1], parts[2]))
        elif len(parts) >= 2:
            changes.append((status, parts[1], None))
    return changes


def show_ref(ref: str, path: str) -> str:
    """某个 ref 里这一页的全文；文件不在就抛 GitError。只用于同步器读本地页。"""
    _check_ref(ref)
    return _git("show", f"{ref}:{path}")


def try_show_ref(ref: str, path: str) -> str | None:
    """同 show_ref，但文件不在时返 None（同步器要区分"新页"和"读失败"）。"""
    _check_ref(ref)
    args = ("show", f"{ref}:{path}")
    proc = _run(args, text=True)
    if proc.returncode != 0:
        return None
    return proc.stdout


def push(remote: str, refspec: str) -> None:
    """把当前 HEAD 推到远端（同步器最后一步：让本地能 fast-forward 回来）。"""
    _check_ref(remote)
    _git("push", remote, refspec)


def list_refs(prefix: str = "refs/remotes/hermes/") -> list[str]:
    """列出某个前缀下的 ref（同步器用来发现本地机器推上来的 outbox 分支）。"""
    out = _git("for-each-ref", "--format=%(refname)", prefix)
    return [line.strip() for line in out.splitlines() if line.strip()]


def merge_file(ours: str, base: str, theirs: str, *, favor: str | None = None) -> tuple[str, int]:
    """三方可合并文本：返回 (合并结果, 冲突块数)。

    `favor="theirs"` 时冲突块取第三份（本次同步里 = 本地 Hermes 的正文），非冲突块
    保留线上改动；`favor=None` 只做普通合并（冲突块保留标记，调用方可用它数冲突）。
    用 git merge-file 而不是自己实现 diff3：行的匹配/上下文规则与 git 一致。
    """
    import tempfile

    if favor not in (None, "ours", "theirs", "union"):
        raise GitError(f"不支持的合并方向：{favor!r}")
    with tempfile.TemporaryDirectory(prefix="openllmwiki-merge-") as tmp:
        paths = []
        for name, text in (("ours", ours), ("base", base), ("theirs", theirs)):
            path = os.path.join(tmp, name)
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
            paths.append(path)
        args = ["merge-file", "-p"]
        if favor is not None:
            args.append(f"--{favor}")
        args.extend(paths)
        proc = _run(tuple(args), text=True)
        # git merge-file 的退出码 = 冲突块数（0..127），255 才是真错误；不是"非零即失败"。
        if proc.returncode > 127:
            raise _failure(tuple(args), proc)
        return proc.stdout, proc.returncode
