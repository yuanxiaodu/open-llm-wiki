"""cli backup 的档里到底有什么（裁定 R35）。

data/wiki 现在是事实源，它的 .git 就是全部版本历史。backup 只备 db 与 uploads 的话，
恢复出来的站点是"正文全丢、历史全无"——所以这三条测试钉的是：
档里有 wiki 的 md、有 data/wiki/.git（连提交对象一起），解出来还是一个能跑 git log 的仓库，
而且 --keep 的剪法对新老三种档一致。
"""

from __future__ import annotations

import shutil
import subprocess
import tarfile

import pytest

from app.config import settings
from app.wiki import store

SLUG = "baoming"
PAGE_PATH = f"entities/{SLUG}.md"
BODY = "v1 正文。"
SUBJECT = f"wiki|create|{SLUG}|张三|新建"

# (档名前缀, 扩展名)。四种档一份 backup 各出一个，同一个时间戳
# （裁定 R81 之后 extracted_text 也在列）。
KINDS = (
    ("db-", ".sqlite3"), ("uploads-", ".tar.gz"),
    ("extracted_text-", ".tar.gz"), ("wiki-", ".tar.gz"),
)


@pytest.fixture
def backups():
    """每条测试从空的 backups 目录开始。

    --keep 的断言要数得出份数，不能被上一条测试留下的档搅浑；
    同一个时间戳（精确到秒）里跑两次 backup 也会互相覆盖，一并躲开。
    """
    if settings.backups_dir.exists():
        shutil.rmtree(settings.backups_dir)
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    yield settings.backups_dir


def _seed_page(conn) -> None:
    """走 store 的共享写路径塞一页进 data/wiki（文件 + commit + 引注记录 + 检索）。"""
    res = store.write_page_sync(
        conn, path=PAGE_PATH, title="报名流程", page_type="entity", body_md=BODY,
        citations=[], actor="张三", action="create", note="新建",
    )
    assert res["ok"], res["failed"]
    conn.commit()


def _names(archive) -> list[str]:
    with tarfile.open(archive, "r:gz") as tar:
        return tar.getnames()


def test_backup_archive_carries_wiki_pages_and_their_git(conn, tmp_wiki, backups):
    """档里既有 wiki 的 md，也有 data/wiki/.git（含提交对象），db 与 uploads 两份档照旧。"""
    from argparse import Namespace

    from app.cli import backup

    _seed_page(conn)
    backup(Namespace(keep=14))

    wikis = sorted(backups.glob("wiki-*.tar.gz"))
    assert len(wikis) == 1, [p.name for p in backups.iterdir()]
    names = _names(wikis[0])
    assert f"wiki/{PAGE_PATH}" in names, names            # 事实源本身
    assert "wiki/.git/HEAD" in names, names               # 版本库在档里
    assert any(n.startswith("wiki/.git/objects/") for n in names), names  # 提交对象=历史

    # 既有覆盖不许被这次改动弄丢（R35 只要求"加上 wiki"，不是换个档）
    for prefix, ext in KINDS:
        assert len(sorted(backups.glob(f"{prefix}*{ext}"))) == 1, (prefix, ext)


def test_backup_archive_carries_extracted_text(conn, tmp_wiki, backups):
    """裁定 R81：data/extracted_text 的解析文本也进档，与 wiki/uploads 那份对称。

    db 里 upload_files.parsed_text_path 指着这个目录：缺了它，恢复出来的站点是
    "材料在、引注定位与检索全断"，而且 reindex 补不回来（它只重读、不重解析）。
    """
    from argparse import Namespace

    from app.cli import backup

    _seed_page(conn)
    parsed = settings.extracted_text_dir / "u1.txt"
    parsed.parent.mkdir(parents=True, exist_ok=True)
    parsed.write_text("第三章 报名流程\n漏单降到 1 单\n", encoding="utf-8")
    backup(Namespace(keep=14))

    archives = sorted(backups.glob("extracted_text-*.tar.gz"))
    assert len(archives) == 1, [p.name for p in backups.iterdir()]
    names = _names(archives[0])
    assert "extracted_text/u1.txt" in names, names


def test_wiki_archive_restores_a_repo_with_history(conn, tmp_wiki, backups, tmp_path):
    """解档 = 恢复：解出来的目录还能跑 git log，主题与正文与写进去的逐字相同。"""
    from argparse import Namespace

    from app.cli import backup

    _seed_page(conn)
    backup(Namespace(keep=14))
    archive = sorted(backups.glob("wiki-*.tar.gz"))[-1]

    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(tmp_path / "restore", filter="data")
    restored = tmp_path / "restore" / "wiki"

    assert BODY in (restored / PAGE_PATH).read_text(encoding="utf-8")
    log = subprocess.run(
        ("git", "log", "--format=%s", "--", PAGE_PATH), cwd=restored,
        capture_output=True, text=True, timeout=60,
    )
    assert log.returncode == 0, log.stderr
    assert log.stdout.strip().splitlines()[-1] == SUBJECT


def test_keep_prunes_every_kind_of_archive_the_same_way(client, tmp_wiki, backups):
    """--keep 的剪法照旧（按名字排序留最后 N 份），只是多认一种档名。"""
    from argparse import Namespace

    from app.cli import backup

    for day in ("01", "02", "03"):
        for prefix, ext in KINDS:
            (backups / f"{prefix}202001{day}-000000{ext}").write_bytes(b"old")

    backup(Namespace(keep=2))

    for prefix, ext in KINDS:
        left = sorted(p.name for p in backups.glob(f"{prefix}*{ext}"))
        old = [n for n in left if n.startswith(f"{prefix}2020")]
        new = [n for n in left if not n.startswith(f"{prefix}2020")]
        assert old == [f"{prefix}20200103-000000{ext}"], left   # 最老的两份被剪掉
        assert len(new) == 1, left                              # 这一趟的新档留下


def test_backup_refuses_to_run_without_a_wiki_repo(tmp_path):
    """data/wiki/.git 不在就当场退出，而不是"少一份档也算备份成功"。

    那正是 R35 这个洞的形状：定时器绿着，恢复时才发现事实源没进去。
    """
    from argparse import Namespace

    from app.cli import backup
    from app.db import init_db
    from app.wiki import repo

    init_db()
    if settings.wiki_dir.exists():
        shutil.rmtree(settings.wiki_dir)
    before = sorted(p.name for p in settings.backups_dir.iterdir())

    with pytest.raises(SystemExit) as exc:
        backup(Namespace(keep=14))

    assert "data/wiki" in str(exc.value)
    assert sorted(p.name for p in settings.backups_dir.iterdir()) == before  # 什么都没落下
    repo.ensure_repo()  # 别把 data/wiki 的空缺留给后面的测试
