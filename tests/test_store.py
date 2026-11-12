"""store 的共享写路径：Hermes 物理 vault（S5）。

覆盖四块：
1. 写路径的硬性质：引注逐字校验不过即整页退回、乐观锁、恒等闸、no-op、
   commit 失败三份回滚（页面 + index.md + log.md）、单进程锁、create 撞车；
2. 读侧：path/slug 兼容、坏文件不出 500、引注来源元数据、目录摘要、wikilink/backlink；
3. vault 的自动维护：ensure_vault / index.md / log.md / raw 摄入 / lint；
4. 检索重建（语料进 FTS 的 ref_id 是页面 path）。

旧的平文件布局断言全部迁到新布局（`entities/baoming.md` 一类），没有放宽：
失败路径仍然一个字都不落盘、不留 commit、不留暂存残局。
"""

import asyncio
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pytest

from app.services import search
from app.wiki import lint as lint_module
from app.wiki import repo, serialize, store
from app.wiki.serialize import PageFormatError


def _p(slug: str, page_type: str = "entity") -> str:
    """页面相对 path：`entities/baoming.md`。"""
    return serialize.path_for(slug, page_type)


def _file(tmp_wiki, rel: str):
    return tmp_wiki / rel


def _cite(conn, message_id, quote):
    return {"key": "c1", "source_kind": "message", "source_id": message_id, "quote": quote}


def _write(conn, slug="baoming", *, title="报名流程线上化", page_type="entity",
           body_md="正文", citations=None, actor="张三", **kwargs):
    """写一页的测试快捷方式：显式 path（目录由 page_type 决定）。"""
    return store.write_page_sync(
        conn, path=_p(slug, page_type), title=title, page_type=page_type,
        body_md=body_md, citations=citations or [], actor=actor, **kwargs,
    )


def _multiline_message(conn, member, content):
    """插一条正文含换行的 human 消息，返回 message_id（R18 要用）。"""
    cur = conn.execute(
        "INSERT INTO messages (session_id, seq, role, user_id, content, created_at)"
        " VALUES (?, 99, 'human', ?, ?, '2026-10-21T00:00:00')",
        (member.session_id, member.user_id, content),
    )
    return int(cur.lastrowid)


def _chinese(note: str) -> bool:
    """是不是给人看的中文（不是空串、不是纯 ASCII 的机器话）。"""
    return bool(note) and not note.isascii()


def _raw_git(cwd, *args) -> str:
    """测试自己用的 git 出口：不经过 repo/store，免得"用实现验实现"。

    看的是工作区与索引的**可观察状态**（status --porcelain、diff --cached），
    这两个是 R45"撤下暂存"的唯一判据。
    """
    env = dict(os.environ)
    env.update({
        "GIT_DIR": str(cwd / ".git"), "GIT_WORK_TREE": str(cwd),
        "GIT_PREFIX": "/", "GIT_LITERAL_PATHSPECS": "1",
    })
    proc = subprocess.run(
        ("git", *args), cwd=cwd, env=env, capture_output=True,
        text=True, encoding="utf-8", timeout=60,
    )
    assert proc.returncode == 0, (args, proc.stdout, proc.stderr)
    return proc.stdout


def _index_and_worktree(cwd) -> tuple[str, str]:
    """(暂存区里有哪些路径, 工作区/索引整体脏不脏)。都该是空的。

    2026-11-13 修正后 SCHEMA/index 重新跟踪；这里保留对旧 `??` 行的兼容过滤，
    不改变"写完后暂存区/工作区应当干净"的判据。
    """
    status = "".join(
        line for line in _raw_git(cwd, "status", "--porcelain").splitlines(keepends=True)
        if line.strip() not in ("?? index.md", "?? SCHEMA.md")
    )
    return (
        _raw_git(cwd, "diff", "--cached", "--name-only").split(),
        status,
    )


def _count_commits(cwd) -> int:
    return int(_raw_git(cwd, "rev-list", "--count", "HEAD").strip())


# --- 写路径基本功：一次 commit 落页面 + index.md + log.md --------------------


def test_write_ok_commits_page_index_log_and_records(conn, tmp_wiki, member):
    rel = _p("baoming")
    res = store.write_page_sync(
        conn, path=rel, title="报名流程线上化", page_type="entity",
        body_md="漏单降到 1 单[^c1]。",
        citations=[_cite(conn, member.message_id, member.quote)],
        actor="张三", action="create", note="新建", tags=["增长"],
    )
    assert res["ok"] and res["sha"]
    assert (res["path"], res["slug"]) == (rel, "baoming")
    assert _file(tmp_wiki, rel).exists()

    page = _file(tmp_wiki, rel).read_text(encoding="utf-8")
    assert "type: entity" in page
    assert "tags: [增长]" in page
    assert "created: " in page and "updated: " in page

    # FTS 的 ref_id 也是 path
    hits = conn.execute(
        "SELECT ref_id, heading, body FROM fts_shared WHERE kind='page'"
    ).fetchall()
    assert [hit["ref_id"] for hit in hits] == [rel]
    assert "漏单" in hits[0]["body"]

    # index.md：按 type 分节 + 一行 summary；log.md：追加式条目
    index_text = _file(tmp_wiki, "index.md").read_text(encoding="utf-8")
    assert "总页数：1" in index_text
    assert "## 实体（1）" in index_text
    assert "- [[baoming|报名流程线上化]] — 漏单降到 1 单。" in index_text
    log_text = _file(tmp_wiki, "log.md").read_text(encoding="utf-8")
    assert "create | 报名流程线上化" in log_text
    assert f"- path: {rel}" in log_text
    assert "- actor: 张三" in log_text

    # 2026-11-13 修正：SCHEMA/index 跟页面一起跟踪；写页提交页面 + index + log。
    changed = _raw_git(tmp_wiki, "show", "--name-only", "--format=", res["sha"]).split()
    assert sorted(changed) == sorted([rel, "index.md", "log.md"])


def test_write_creates_vault_dirs_on_demand(conn, tmp_wiki):
    """没跑过 ensure_vault 的空仓库也能写：_ensure_dirs 会把目录建出来。"""
    assert not (tmp_wiki / "entities").exists()
    res = _write(conn, body_md="正文")
    assert res["ok"]
    assert (tmp_wiki / "entities").is_dir()
    assert (tmp_wiki / "raw" / "uploads").is_dir()


def test_explicit_path_writes_into_that_directory(conn, tmp_wiki):
    res = store.write_page_sync(
        conn, path="concepts/crossing-the-chasm", title="跨越鸿沟", page_type="concept",
        body_md="概念页正文", citations=[], actor="张三",
    )
    assert res["ok"]
    assert (tmp_wiki / "concepts" / "crossing-the-chasm.md").exists()
    assert store.get_page(conn, res["path"])["page_type"] == "concept"


def test_path_none_allocates_dir_and_slug(conn, tmp_wiki):
    res = store.write_page_sync(
        conn, path=None, title="报名流程线上化", page_type="entity",
        body_md="漏单降到 1 单。", citations=[], actor="张三", note="人建",
    )
    assert res["ok"] is True
    assert res["path"].startswith("entities/")
    assert res["slug"] == "bao-ming-liu-cheng-xian-shang-hua"
    assert _file(tmp_wiki, res["path"]).exists()
    assert store.get_page(conn, res["path"])["title"] == "报名流程线上化"


def test_path_none_two_different_chinese_titles_same_day_get_distinct_slugs(
    conn, tmp_wiki
):
    """同日的两个**不同**中文标题不许被误判成同一页（裁定 R-S2-3）。"""
    a = store.write_page_sync(
        conn, path=None, title="跨越鸿沟", page_type="concept", body_md="一",
        citations=[], actor="张三",
    )
    b = store.write_page_sync(
        conn, path=None, title="需求评审怎么开", page_type="entity", body_md="二",
        citations=[], actor="李四",
    )
    assert a["ok"] and b["ok"], (a, b)
    assert a["slug"] == "kua-yue-hong-gou"
    assert b["slug"] == "xu-qiu-ping-shen-zen-me-kai"
    assert a["path"] == "concepts/kua-yue-hong-gou.md"
    assert b["path"] == "entities/xu-qiu-ping-shen-zen-me-kai.md"
    assert store.get_page(conn, a["path"])["title"] == "跨越鸿沟"
    assert store.get_page(conn, b["path"])["title"] == "需求评审怎么开"
    assert store.get_page(conn, b["path"])["body_md"] == "二"


def test_path_none_same_title_twice_second_is_conflict(conn, tmp_wiki):
    a = store.write_page_sync(
        conn, path=None, title="跨越鸿沟", page_type="concept", body_md="x",
        citations=[], actor="张三",
    )
    assert a["ok"]
    b = store.write_page_sync(
        conn, path=None, title="跨越鸿沟", page_type="concept", body_md="y",
        citations=[], actor="李四",
    )
    assert not b["ok"] and b["failed"][0]["note"] == store.ALREADY_EXISTS
    assert b["path"] == a["path"]  # failed 里回报撞上的是哪一页
    assert store.get_page(conn, a["path"])["body_md"] == "x"
    assert not _file(tmp_wiki, f"concepts/{a['slug']}-2.md").exists()

    # 两端空白不算不同标题
    c = store.write_page_sync(
        conn, path=None, title="  跨越鸿沟  ", page_type="concept", body_md="z",
        citations=[], actor="李四",
    )
    assert not c["ok"] and c["failed"][0]["note"] == store.ALREADY_EXISTS
    assert c["path"] == a["path"]


def test_path_none_bad_type_returns_failed_in_chinese(conn, tmp_wiki):
    res = store.write_page_sync(
        conn, path=None, title="T", page_type="novel", body_md="正文",
        citations=[], actor="张三",
    )
    assert not res["ok"] and res["path"] is None
    note = res["failed"][0]["note"]
    assert _chinese(note) and "type 不在白名单" in note
    assert list(tmp_wiki.glob("*.md")) == []


def test_duplicate_title_conflict_is_independent_of_directory(conn, tmp_wiki):
    """标题冲突闸扫全库：concept 里已有同名标题，entity 建页也要被顶回来。"""
    first = _write(conn, slug="same", page_type="concept", title="同名标题")
    assert first["ok"]
    second = store.write_page_sync(
        conn, path=None, title="同名标题", page_type="entity", body_md="正文",
        citations=[], actor="李四",
    )
    assert not second["ok"] and second["failed"][0]["note"] == store.ALREADY_EXISTS
    assert second["path"] == first["path"]


def _parallel_create_intents(conn, monkeypatch, specs, widen=0.05):
    """把 specs=[(title, page_type, body, actor), ...] 的 path=None 建页同时放出去。"""
    real_head_sha = repo.head_sha

    def slow_head_sha(path):
        time.sleep(widen)
        return real_head_sha(path)

    monkeypatch.setattr(repo, "head_sha", slow_head_sha)
    gate = threading.Barrier(len(specs))

    def run(title, page_type, body, actor):
        gate.wait(timeout=30)
        return store.write_page_sync(
            conn, path=None, title=title, page_type=page_type, body_md=body,
            citations=[], actor=actor,
        )

    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        futures = [pool.submit(run, *spec) for spec in specs]
        return [future.result(timeout=120) for future in futures]


def test_path_none_concurrent_different_titles_neither_page_is_overwritten(
    conn, tmp_wiki, monkeypatch
):
    """裁定 R-S2-4：两个并发 create-intent（不同中文标题）不许落到同一个 slug 上。"""
    specs = [("跨越鸿沟", "concept", "一", "张三"), ("需求评审怎么开", "entity", "二", "李四")]
    a, b = _parallel_create_intents(conn, monkeypatch, specs)
    if len({a["path"], b["path"]}) == 2:
        assert a["ok"] and b["ok"], (a, b)
    else:
        winners = [r for r in (a, b) if r["ok"]]
        assert len(winners) == 1, (a, b)
        loser = b if winners[0] is a else a
        assert loser["failed"][0]["note"] == store.ALREADY_EXISTS, loser

    for (title, _type, body, _actor), res in zip(specs, (a, b)):
        if not res["ok"]:
            continue
        got = store.get_page(conn, res["path"])
        assert got is not None
        assert got["title"] == title, (res["path"], got["title"], title)
        assert got["body_md"] == body
    assert {page["title"] for page in store.list_pages(conn)} == {
        title for (title, _type, _body, _actor), res in zip(specs, (a, b)) if res["ok"]
    }
    # 写成的每一页一个文件：没有"两个 ok 共用一个文件"
    page_files = [rel for rel in store.page_files()]
    assert len(page_files) == sum(1 for res in (a, b) if res["ok"]), page_files


def test_path_none_concurrent_same_title_one_ok_one_conflict(conn, tmp_wiki, monkeypatch):
    """同标题的并发 create-intent：恰好一个 ok、一个「这一页已存在」，不悄悄错开。"""
    specs = [("跨越鸿沟", "concept", "一", "张三"), ("跨越鸿沟", "concept", "二", "李四")]
    a, b = _parallel_create_intents(conn, monkeypatch, specs)
    winners = [res for res in (a, b) if res["ok"]]
    clashes = [res for res in (a, b) if not res["ok"]]
    assert len(winners) == 1 and len(clashes) == 1, (a, b)
    assert clashes[0]["failed"][0]["note"] == store.ALREADY_EXISTS
    assert clashes[0]["path"] == winners[0]["path"]
    assert len(list((tmp_wiki / "concepts").glob("*.md"))) == 1
    winner_spec = specs[0] if winners[0] is a else specs[1]
    page = store.get_page(conn, winners[0]["path"])
    assert page["title"] == winner_spec[0]
    assert page["body_md"] == winner_spec[2]


def test_unreadable_md_entries_are_skipped_on_write_and_list(conn, tmp_wiki):
    """名叫 *.md 的目录 / 断链 symlink 不许把裸 OSError 甩出写路径或目录。"""
    (tmp_wiki / "entities").mkdir()
    (tmp_wiki / "entities" / "evil.md").mkdir()
    (tmp_wiki / "entities" / "dangling.md").symlink_to(
        tmp_wiki / "entities" / "根本没有这个文件.md"
    )

    res = store.write_page_sync(
        conn, path=None, title="跨越鸿沟", page_type="concept", body_md="一",
        citations=[], actor="张三",
    )
    assert res["ok"] is True, res
    assert [page["slug"] for page in store.list_pages(conn)] == [res["slug"]]
    assert store.get_page(conn, "entities/evil.md") is None
    assert store.get_page(conn, "entities/dangling.md") is None


# --- 引注：逐字校验不过 → 整页退回 ------------------------------------------


# --- R43：落盘排在校验之后 —— 磁盘上不存在"未校验的正文"这个中间态 ---


# --- R15：落盘前的 dump→parse→dump 恒等闸 ---


def test_title_with_newline_is_rejected(conn, tmp_wiki):
    res = _write(conn, slug="nl", title="标题\n第二行", body_md="正文")
    assert not res["ok"] and _chinese(res["failed"][0]["note"])
    assert not _file(tmp_wiki, res["path"]).exists()


def test_unknown_page_type_is_rejected(conn, tmp_wiki):
    res = store.write_page_sync(
        conn, path="entities/novel.md", title="T", page_type="novel", body_md="正文",
        citations=[], actor="张三",
    )
    assert not res["ok"]
    note = res["failed"][0]["note"]
    assert _chinese(note)
    assert "type 不在白名单" in note, note
    assert "正文形状" not in note, note
    assert not _file(tmp_wiki, "entities/novel.md").exists()


def test_legit_input_is_not_rejected_by_the_gate(conn, tmp_wiki, member):
    res = _write(
        conn, slug="ok", title="  报名流程  ", body_md="\n\n漏单降到 1 单[^c1]。\n\n",
        citations=[_cite(conn, member.message_id, member.quote)],
    )
    assert res["ok"], res["failed"]
    page = store.get_page(conn, res["path"])
    assert page["title"] == "报名流程"
    assert page["body_md"] == "漏单降到 1 单[^c1]。"


@pytest.mark.parametrize("name,sep", [
    ("crlf", "\r\n"),
    ("line-sep", "\u2028"),
    ("cr", "\r"),
    ("para-sep", "\u2029"),
    ("form-feed", "\x0c"),
    ("nel", "\x85"),
    ("vtab", "\x0b"),
])
def test_body_with_any_line_boundary_writes_and_reads_back_as_lf(
    conn, tmp_wiki, member, name, sep
):
    """R44：正文里的行边界一律归一成 \n（dump 用 \\n 拼、parse 用 splitlines 拆）。"""
    res = _write(
        conn, slug=name,
        body_md=f"第一段[^c1]。{sep}{sep}第二段{sep}",
        citations=[_cite(conn, member.message_id, member.quote)],
    )
    assert res["ok"], res["failed"]
    raw = _file(tmp_wiki, res["path"]).read_bytes()
    assert b"\r" not in raw
    assert sep.encode("utf-8") not in raw
    assert serialize.parse_md(name, raw.decode("utf-8")).body_md == (
        "第一段[^c1]。\n\n第二段"
    )
    page = store.get_page(conn, res["path"])
    assert page["body_md"] == "第一段[^c1]。\n\n第二段"
    again = store.write_page_sync(
        conn, path=res["path"], title="报名流程线上化", page_type="entity",
        body_md="第一段[^c1]。\n\n第二段",
        citations=[_cite(conn, member.message_id, member.quote)],
        actor="张三", action="edit", base_commit=res["sha"],
    )
    assert again["ok"] and again["noop"] is True


def test_title_with_carriage_return_is_still_rejected(conn, tmp_wiki):
    for bad_title in ("标题\r\n第二行", "标题\u2028第二行", "标题\r第二行"):
        res = _write(conn, slug="nl-title", title=bad_title, body_md="正文")
        assert not res["ok"], bad_title
        assert _chinese(res["failed"][0]["note"]), bad_title
        assert not _file(tmp_wiki, res["path"]).exists(), bad_title


# --- R17 / R84：create 撞车与 create/edit 推断 ---------------------------------


def test_create_on_existing_page_is_refused(conn, tmp_wiki, member):
    good = _cite(conn, member.message_id, member.quote)
    first = _write(conn, slug="e", body_md="v1[^c1]", citations=[good],
                   action="create")
    assert first["ok"]
    second = _write(conn, slug="e", title="别的标题", body_md="v2[^c1]",
                    citations=[good], action="create")
    assert not second["ok"] and second["sha"] is None
    assert second["failed"][0]["note"] == "这一页已存在"
    text = _file(tmp_wiki, first["path"]).read_text(encoding="utf-8")
    assert "v1" in text and "v2" not in text and "别的标题" not in text
    assert [entry["action"] for entry in store.versions(first["path"])] == ["create"]

    third = _write(conn, slug="e", body_md="v3[^c1]", citations=[good],
                   action="edit", base_commit=first["sha"])
    assert third["ok"] and not third["noop"]
    assert "v3" in _file(tmp_wiki, first["path"]).read_text(encoding="utf-8")


def test_action_none_decides_by_path_existence(conn, tmp_wiki, member):
    good = _cite(conn, member.message_id, member.quote)
    first = _write(conn, slug="auto", body_md="v1[^c1]", citations=[good],
                   action=None)
    assert first["ok"] and store.versions(first["path"])[0]["action"] == "create"
    second = _write(conn, slug="auto", body_md="v2[^c1]", citations=[good],
                    action=None)
    assert second["ok"] and store.versions(second["path"])[0]["action"] == "edit"
    clash = _write(conn, slug="auto", body_md="v3[^c1]", citations=[good],
                   action="create")
    assert not clash["ok"] and clash["failed"][0]["note"] == store.ALREADY_EXISTS


def test_type_cannot_be_changed_after_creation(conn, tmp_wiki):
    """计划 §1：type 决定目录、创建后不可改（与 slug 同口径）。"""
    first = _write(conn, slug="keep", page_type="entity", body_md="正文")
    assert first["ok"]
    res = store.write_page_sync(
        conn, path=first["path"], title="报名流程线上化", page_type="concept",
        body_md="新正文", citations=[], actor="李四",
    )
    assert not res["ok"]
    assert "类型不能改" in res["failed"][0]["note"]
    assert store.get_page(conn, first["path"])["page_type"] == "entity"
    assert [entry["action"] for entry in store.versions(first["path"])] == ["create"]


def test_created_is_fixed_and_updated_moves_on_edit(conn, tmp_wiki, monkeypatch):
    """计划 §1：created 首写定、updated 每写更新（跨天用 _today 模拟）。"""
    first = _write(conn, slug="dates", page_type="concept", body_md="v1")
    assert first["ok"]
    created = date.today().isoformat()
    page = store.get_page(conn, first["path"])
    assert (page["created_at"], page["updated_at"]) == (created, created)

    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    monkeypatch.setattr(store, "_today", lambda: tomorrow)
    second = store.write_page_sync(
        conn, path=first["path"], title="报名流程线上化", page_type="concept",
        body_md="v2", citations=[], actor="李四", action="edit",
        base_commit=first["sha"],
    )
    assert second["ok"] and not second["noop"]
    page = store.get_page(conn, second["path"])
    assert page["created_at"] == created
    assert page["updated_at"] == tomorrow


# --- 只读兼容：path 带/不带 .md、旧裸 slug ------------------------------------


def test_get_page_accepts_path_with_and_without_md_and_legacy_slug(conn, tmp_wiki):
    res = write_res = store.write_page_sync(
        conn, path=None, title="报名流程线上化", page_type="concept", body_md="正文",
        citations=[], actor="张三",
    )
    assert write_res["ok"]
    for ref in (res["path"], res["path"][:-3], res["slug"]):
        page = store.get_page(conn, ref)
        assert page is not None, ref
        assert page["path"] == res["path"]
        assert page["slug"] == res["slug"]
    assert store.versions(res["slug"]) == store.versions(res["path"])


def test_get_page_shape(conn, tmp_wiki):
    res = _write(conn, slug="shape", tags=["增长"], sources=["raw/uploads/1-x.md"],
                 confidence="high", contested=True, contradictions=["other"])
    assert res["ok"], res["failed"]
    page = store.get_page(conn, res["path"])
    assert set(page) == {
        "path", "slug", "title", "page_type", "body_md", "head", "tags", "sources",
        "created_at", "updated_at", "confidence", "contested", "contradictions",
        "citations", "wikilinks", "backlinks", "metadata",
    }
    assert page["tags"] == ["增长"]
    assert page["sources"] == ["raw/uploads/1-x.md"]
    assert (page["confidence"], page["contested"], page["contradictions"]) == (
        "medium", True, ["other"]
    )


def test_wikilinks_and_backlinks(conn, tmp_wiki):
    a = store.write_page_sync(
        conn, path=None, title="源页", page_type="entity", body_md="指向 [[目标页]] 与 [[目标页|别名]]",
        citations=[], actor="张三",
    )
    b = store.write_page_sync(
        conn, path=None, title="目标页", page_type="concept",
        body_md="被指向的页\n\n断链 [[没有这一页]]", citations=[], actor="张三",
    )
    assert a["ok"] and b["ok"], (a, b)
    page_a = store.get_page(conn, a["path"])
    assert [(link["target"], link["label"], link["path"]) for link in page_a["wikilinks"]] == [
        ("目标页", "目标页", b["path"]),
        ("目标页", "别名", b["path"]),
    ]
    page_b = store.get_page(conn, b["path"])
    assert page_b["wikilinks"] == [
        {"target": "没有这一页", "label": "没有这一页", "path": None}
    ]
    assert page_b["backlinks"] == [{"path": a["path"], "title": "源页"}]
    # 引注角标不算页面链接
    assert store.get_page(conn, a["path"])["wikilinks"] != []


def test_title_links_tolerate_spacing_and_punctuation(conn, tmp_wiki):
    """标题链接的宽松层：服务端发 slug 之前，正文只能按标题链；少一个冒号/空格不该断。

    精确 slug/标题仍然优先；只有精确匹配不到时才走归一化，且归一化撞车就整组丢弃
    （宁可 lint 报断链，也不把链接指到错页）。
    """
    target = store.write_page_sync(
        conn, path=None, title="应届生产品求职：offer 判断框架与作品集结构",
        page_type="concept", body_md="目标页", citations=[], actor="张三",
    )
    source = store.write_page_sync(
        conn, path=None, title="宽松链接源页", page_type="concept",
        body_md="见 [[应届生产品求职 offer 判断框架与作品集结构]] 与 [[应届生产品求职：offer 判断框架与作品集结构]]。",
        citations=[], actor="张三",
    )
    assert target["ok"] and source["ok"], (target, source)
    links = store.get_page(conn, source["path"])["wikilinks"]
    assert [link["path"] for link in links] == [target["path"], target["path"]]


def test_ambiguous_title_keys_are_dropped_not_guessed(conn, tmp_wiki):
    """两个标题归一化后撞车（只差标点）→ 宽松层不解析，精确匹配照旧。"""
    a = store.write_page_sync(
        conn, path=None, title="增长:漏斗", page_type="concept",
        body_md="A", citations=[], actor="张三",
    )
    b = store.write_page_sync(
        conn, path=None, title="增长 漏斗", page_type="concept",
        body_md="B", citations=[], actor="张三",
    )
    assert a["ok"] and b["ok"], (a, b)
    source = store.write_page_sync(
        conn, path=None, title="撞车源页", page_type="concept",
        body_md="见 [[增长－漏斗]]。", citations=[], actor="张三",
    )
    assert source["ok"], source
    assert store.get_page(conn, source["path"])["wikilinks"] == [
        {"target": "增长－漏斗", "label": "增长－漏斗", "path": None}
    ]



# --- 乐观锁 ------------------------------------------------------------------


def test_conflict_on_stale_base(conn, tmp_wiki, member):
    good = _cite(conn, member.message_id, member.quote)
    first = _write(conn, slug="c", body_md="v1[^c1]", citations=[good],
                   action="create")
    with pytest.raises(store.ConflictError) as exc:
        _write(conn, slug="c", body_md="v2[^c1]", citations=[good],
               actor="李四", action="edit", base_commit="0" * 40)
    assert exc.value.diff_text != ""
    assert first["ok"]
    assert "v1" in _file(tmp_wiki, first["path"]).read_text(encoding="utf-8")
    assert [entry["action"] for entry in store.versions(first["path"])] == ["create"]


def test_conflict_on_new_page_never_diffs_head(conn, tmp_wiki, monkeypatch):
    calls = []

    def recording_diff(a, b, path):
        calls.append((a, b, path))
        return "不该被调到"

    monkeypatch.setattr(repo, "diff", recording_diff)
    with pytest.raises(store.ConflictError) as exc:
        _write(conn, slug="brand-new", body_md="x",
               base_commit="0" * 40, action="create")
    assert calls == []
    assert exc.value.diff_text == ""
    assert _chinese(exc.value.reason)
    assert not _file(tmp_wiki, _p("brand-new")).exists()


def test_conflict_diff_falls_back_when_base_is_not_an_object(conn, tmp_wiki, member):
    good = _cite(conn, member.message_id, member.quote)
    first = _write(conn, slug="f", body_md="v1[^c1]", citations=[good],
                   action="create")
    assert first["ok"]
    with pytest.raises(store.ConflictError) as exc:
        _write(conn, slug="f", body_md="v2[^c1]", citations=[good],
               actor="李四", action="edit", base_commit="ab" * 20)
    assert "v1" in exc.value.diff_text
    assert "v2" not in _file(tmp_wiki, first["path"]).read_text(encoding="utf-8")


def test_conflict_with_real_base_shows_diff(conn, tmp_wiki, member):
    good = _cite(conn, member.message_id, member.quote)
    first = _write(conn, slug="g", body_md="v1[^c1]", citations=[good],
                   action="create")
    second = _write(conn, slug="g", body_md="v2[^c1]", citations=[good],
                    action="edit", base_commit=first["sha"])
    assert second["ok"]
    with pytest.raises(store.ConflictError) as exc:
        _write(conn, slug="g", body_md="v3[^c1]", citations=[good],
               actor="李四", action="edit", base_commit=first["sha"])
    assert "-v1" in exc.value.diff_text and "+v2" in exc.value.diff_text


def test_base_commit_accepts_prefix_full_and_uppercase(conn, tmp_wiki, member):
    """裁定 R-S2-29：7..40 位十六进制前缀（大小写不敏感）都算对得上。"""
    good = _cite(conn, member.message_id, member.quote)
    first = _write(conn, slug="px", body_md="v1[^c1]", citations=[good],
                   action="create")
    second = _write(conn, slug="px", body_md="v2[^c1]", citations=[good],
                    action="edit", base_commit=first["sha"][:12])
    assert second["ok"] and not second["noop"]
    third = _write(conn, slug="px", body_md="v3[^c1]", citations=[good],
                   action="edit", base_commit=second["sha"])
    assert third["ok"]
    fourth = _write(conn, slug="px", body_md="v4[^c1]", citations=[good],
                    action="edit", base_commit=third["sha"].upper())
    assert fourth["ok"]
    assert "v4" in _file(tmp_wiki, first["path"]).read_text(encoding="utf-8")


def test_base_commit_rejects_too_short_and_non_prefix(conn, tmp_wiki, member):
    good = _cite(conn, member.message_id, member.quote)
    first = _write(conn, slug="py", body_md="v1[^c1]", citations=[good],
                   action="create")
    head = first["sha"]
    with pytest.raises(store.ConflictError):
        _write(conn, slug="py", body_md="v2[^c1]", citations=[good],
               action="edit", base_commit=head[:6])
    other = ("1" if head[0] != "1" else "2") + head[1:12]
    with pytest.raises(store.ConflictError):
        _write(conn, slug="py", body_md="v2[^c1]", citations=[good],
               action="edit", base_commit=other)
    assert "v1" in _file(tmp_wiki, first["path"]).read_text(encoding="utf-8")
    assert [entry["action"] for entry in store.versions(first["path"])] == ["create"]


def test_base_commit_rejects_non_hex_with_a_chinese_note(conn, tmp_wiki, member):
    good = _cite(conn, member.message_id, member.quote)
    first = _write(conn, slug="pz", body_md="v1[^c1]", citations=[good],
                   action="create")
    with pytest.raises(store.ConflictError) as exc:
        _write(conn, slug="pz", body_md="v2[^c1]", citations=[good],
               action="edit", base_commit="zzzzzzz")
    assert _chinese(exc.value.reason)
    assert exc.value.reason == "有人先改了这一页"
    assert _chinese(exc.value.diff_text)
    assert "拿不到" in exc.value.diff_text and "v1" in exc.value.diff_text
    text = _file(tmp_wiki, first["path"]).read_text(encoding="utf-8")
    assert "v1" in text and "v2" not in text


# --- R21：坏文件读侧跳过，不出 500 ---


def test_corrupt_files_are_skipped_by_readers(conn, tmp_wiki, member):
    good = _write(
        conn, slug="good", title="好页", body_md="正文[^c1]",
        citations=[_cite(conn, member.message_id, member.quote)],
    )
    assert good["ok"]
    (tmp_wiki / "entities" / "corrupt.md").write_text("连 front matter 都没有\n", encoding="utf-8")
    (tmp_wiki / "entities" / "binary.md").write_bytes(b"\xff\xfe\x00\x01 not utf-8")
    (tmp_wiki / "entities" / "badcite.md").write_text(
        "---\ntitle: T\ncreated: 2026-10-23\nupdated: 2026-10-23\ntype: entity\n---\n\n"
        "正文\n\n<!--citations\nc1 | message | 不是数字 | q\n-->\n",
        encoding="utf-8",
    )

    listed = store.list_pages(conn)
    assert [page["slug"] for page in listed] == ["good", "badcite"]
    assert listed[0]["title"] == "好页" and listed[0]["actor"] == "张三"
    for ref in ("entities/corrupt.md", "entities/binary.md",
                "entities/根本没有这一页.md", "corrupt"):
        assert store.get_page(conn, ref) is None, ref


def test_list_pages_and_get_page_survive_a_broken_repo(conn, tmp_wiki, monkeypatch):
    """R23：读侧碰上 GitError 抛 store 层的中文错，不出裸 traceback。"""
    def boom(*args, **kwargs):
        raise repo.GitError("fatal: not a git repository")

    _write(conn, slug="x")  # 先造一页（boom 之前，写路径也要 head_sha/log）
    monkeypatch.setattr(repo, "head_sha", boom)
    monkeypatch.setattr(repo, "log", boom)
    monkeypatch.setattr(repo, "latest_touches", boom)
    with pytest.raises(store.StoreError) as exc:
        store.list_pages(conn)
    assert _chinese(str(exc.value))
    with pytest.raises(store.StoreError):
        store.get_page(conn, _p("x"))
    with pytest.raises(store.StoreError):
        store.versions(_p("x"))


# --- R23 / R45：写侧 GitError、commit 段崩溃 → 三份回滚 + 撤暂存 ---------------


def test_git_error_on_write_path_becomes_failed(conn, tmp_wiki, member, monkeypatch):
    good = _cite(conn, member.message_id, member.quote)
    rel = _p("h")

    def boom_commit(paths, subject):
        for path in ([paths] if isinstance(paths, str) else paths):
            _raw_git(tmp_wiki, "add", "--", path)
        raise repo.GitError("git commit 失败 (rc=1)")

    monkeypatch.setattr(repo, "commit", boom_commit)
    res = _write(conn, slug="h", body_md="v1[^c1]", citations=[good],
                 action="create")
    assert not res["ok"] and res["sha"] is None
    assert _chinese(res["failed"][0]["note"])
    assert not _file(tmp_wiki, rel).exists()
    assert not _file(tmp_wiki, "index.md").exists()  # index 也回滚成"本来没有"
    assert _index_and_worktree(tmp_wiki) == ([], "")


@pytest.mark.parametrize("boom_exc", [
    subprocess.TimeoutExpired(cmd=("git", "commit"), timeout=60),
    OSError(28, "No space left on device"),
    FileNotFoundError(2, "No such file or directory: 'git'"),
])
def test_commit_crash_restores_page_index_log_and_leaves_index_clean(
    conn, tmp_wiki, member, monkeypatch, boom_exc
):
    """R45：commit 段挂掉（git 非零 / 60s 超时 / OSError）→ 三份文件逐字恢复 + 撤下暂存。"""
    good = _cite(conn, member.message_id, member.quote)
    first = _write(conn, slug="to", body_md="v1[^c1]", citations=[good],
                   action="create")
    assert first["ok"]
    rel = first["path"]
    before_page = _file(tmp_wiki, rel).read_bytes()
    before_index = _file(tmp_wiki, "index.md").read_bytes()
    before_log = _file(tmp_wiki, "log.md").read_bytes()

    def boom_commit(paths, subject):
        for path in ([paths] if isinstance(paths, str) else paths):
            _raw_git(tmp_wiki, "add", "--", path)
        assert sorted(_raw_git(tmp_wiki, "diff", "--cached", "--name-only").split()) == sorted(
            [rel, "index.md", "log.md"]
        )
        raise boom_exc

    monkeypatch.setattr(repo, "commit", boom_commit)
    res = _write(conn, slug="to", body_md="v2[^c1]", citations=[good],
                 actor="李四", action="edit", base_commit=first["sha"])
    assert not res["ok"] and res["sha"] is None and res["noop"] is False
    assert _chinese(res["failed"][0]["note"])
    assert _file(tmp_wiki, rel).read_bytes() == before_page
    assert _file(tmp_wiki, "index.md").read_bytes() == before_index
    assert _file(tmp_wiki, "log.md").read_bytes() == before_log
    assert _index_and_worktree(tmp_wiki) == ([], "")
    assert [entry["sha"] for entry in store.versions(rel)] == [first["sha"]]

    monkeypatch.undo()
    again = _write(conn, slug="to", body_md="v3[^c1]", citations=[good],
                   actor="李四", action="edit", base_commit=first["sha"])
    assert again["ok"] and not again["noop"], again["failed"]
    assert b"v3" in _file(tmp_wiki, rel).read_bytes()
    assert _index_and_worktree(tmp_wiki) == ([], "")


def test_git_error_head_lookup_becomes_failed(conn, tmp_wiki, monkeypatch):
    def boom(*args, **kwargs):
        raise repo.GitError("fatal: 坏了")

    monkeypatch.setattr(repo, "head_sha", boom)
    res = _write(conn, slug="i", body_md="正文")
    assert not res["ok"]
    assert _chinese(res["failed"][0]["note"])
    assert not _file(tmp_wiki, res["path"]).exists()


def test_missing_wiki_repo_does_not_traceback(conn, tmp_wiki):
    """data/wiki 被删/配错（.git 不在）：不出裸 traceback，翻成人话的 failed。"""
    import shutil

    shutil.rmtree(tmp_wiki)
    res = _write(conn, slug="j", body_md="正文")
    assert not res["ok"]
    assert _chinese(res["failed"][0]["note"])
    assert store.list_pages(conn) == []
    assert store.get_page(conn, _p("j")) is None
    with pytest.raises(store.StoreError):
        store.versions(_p("j"))


# --- R16 / R46 / R51：no-op 语义 ---------------------------------------------


def test_noop_does_not_rewrite_index_or_log(conn, tmp_wiki, member):
    """计划 §2：no-op 写不重写 index/log、不提交。"""
    good = _cite(conn, member.message_id, member.quote)
    kwargs = dict(title="报名流程", page_type="entity", body_md="v1[^c1]",
                  citations=[good], actor="张三")
    first = store.write_page_sync(conn, path=_p("noop-quiet"), action="create", **kwargs)
    before_index = _file(tmp_wiki, "index.md").read_bytes()
    before_log = _file(tmp_wiki, "log.md").read_bytes()
    commits = _count_commits(tmp_wiki)

    again = store.write_page_sync(
        conn, path=_p("noop-quiet"), action="edit", base_commit=first["sha"], **kwargs
    )
    assert again["ok"] and again["noop"] is True
    assert _file(tmp_wiki, "index.md").read_bytes() == before_index
    assert _file(tmp_wiki, "log.md").read_bytes() == before_log
    assert _count_commits(tmp_wiki) == commits


def test_noop_preserves_created_and_updated_on_a_later_day(conn, tmp_wiki, monkeypatch):
    """隔天重写同样内容也是 no-op：updated 不因"今天"变了而变，不造空提交。"""
    first = _write(conn, slug="later", body_md="同一份正文")
    assert first["ok"]
    before = _file(tmp_wiki, first["path"]).read_bytes()
    before_index = _file(tmp_wiki, "index.md").read_bytes()
    commits = _count_commits(tmp_wiki)
    monkeypatch.setattr(
        store, "_today", lambda: (date.today() + timedelta(days=30)).isoformat()
    )
    again = _write(conn, slug="later", body_md="同一份正文", action="edit",
                   base_commit=first["sha"])
    assert again["ok"] and again["noop"] is True
    assert _file(tmp_wiki, first["path"]).read_bytes() == before
    assert _file(tmp_wiki, "index.md").read_bytes() == before_index
    assert _count_commits(tmp_wiki) == commits


def test_noop_still_reindexes_fts_for_head(conn, tmp_wiki, member):
    """R51：no-op 也要把 FTS 重建一遍——与 R46 补 citation_checks 对称。"""
    good = _cite(conn, member.message_id, member.quote)
    kwargs = dict(title="报名流程", page_type="entity", body_md="v1 现行正文[^c1]",
                  citations=[good], actor="张三")
    first = store.write_page_sync(conn, path=_p("ftsorphan"), action="create", **kwargs)
    assert first["ok"]
    current = store.get_page(conn, _p("ftsorphan"))["body_md"]
    conn.execute(
        "UPDATE fts_shared SET body='陈旧正文' WHERE kind='page' AND ref_id=?",
        (_p("ftsorphan"),),
    )
    again = store.write_page_sync(
        conn, path=_p("ftsorphan"), action="edit", base_commit=first["sha"], **kwargs
    )
    assert again["ok"] and again["noop"] is True
    rows = conn.execute(
        "SELECT heading, body FROM fts_shared WHERE kind='page' AND ref_id=?",
        (_p("ftsorphan"),),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["heading"] == "报名流程"
    assert rows[0]["body"] == current and "陈旧正文" not in rows[0]["body"]


# --- 回滚与版本 --------------------------------------------------------------


def test_revert_restores_old_version(conn, tmp_wiki, member):
    good = _cite(conn, member.message_id, member.quote)
    first = _write(conn, slug="m", body_md="v1[^c1]", citations=[good],
                   action="create")
    second = _write(conn, slug="m", body_md="v2[^c1]", citations=[good],
                    action="edit", base_commit=first["sha"])
    assert second["ok"]

    res = asyncio.run(store.revert(conn, first["path"], first["sha"], "王五"))
    assert res["ok"] and res["sha"] not in (first["sha"], second["sha"])
    page = store.get_page(conn, first["path"])
    assert "v1" in page["body_md"] and "v2" not in page["body_md"]
    assert page["head"] == res["sha"]
    # 回滚保留原来的 created，updated 走写路径
    assert page["created_at"] == store.get_page(conn, first["path"])["created_at"]

    entries = store.versions(first["path"])
    assert [entry["action"] for entry in entries] == ["revert", "edit", "create"]
    assert entries[0]["actor"] == "王五"
    assert entries[0]["note"] == f"回滚到 {first['sha'][:8]}"
    assert store.page_diff(first["path"], first["sha"], res["sha"]) == ""
    assert "+v2" in store.page_diff(first["path"], first["sha"], second["sha"])


def test_revert_of_unknown_sha_fails_in_chinese(conn, tmp_wiki):
    res = asyncio.run(store.revert(conn, "entities/n.md", "ab" * 20, "王五"))
    assert not res["ok"]
    assert _chinese(res["failed"][0]["note"])
    assert not _file(tmp_wiki, _p("n")).exists()
    # 裸 slug（磁盘上没有这一页）也走 failed，不是裸异常
    res = asyncio.run(store.revert(conn, "n", "ab" * 20, "王五"))
    assert not res["ok"] and _chinese(res["failed"][0]["note"])


# --- 并发与 async ------------------------------------------------------------


def test_async_write_page_runs_off_the_loop(conn, tmp_wiki, member):
    good = _cite(conn, member.message_id, member.quote)
    res = asyncio.run(
        store.write_page(
            conn, path=_p("async"), title="T", page_type="entity", body_md="v1[^c1]",
            citations=[good], actor="张三", action="create",
        )
    )
    assert res["ok"] and _file(tmp_wiki, _p("async")).exists()


def test_concurrent_writes_to_two_paths_both_land(conn, tmp_wiki, member):
    """跨 path 并发经 to_thread 进线程池，git 的索引只有一份（repo 的锁兜住）。"""
    good = _cite(conn, member.message_id, member.quote)

    async def run():
        return await asyncio.gather(
            store.write_page(
                conn, path=_p("p1"), title="T1", page_type="entity", body_md="一[^c1]",
                citations=[good], actor="张三", action="create",
            ),
            store.write_page(
                conn, path=_p("p2"), title="T2", page_type="concept", body_md="二[^c1]",
                citations=[good], actor="李四", action="create",
            ),
        )

    a, b = asyncio.run(run())
    assert a["ok"] and b["ok"], (a, b)
    assert a["sha"] != b["sha"]
    assert _file(tmp_wiki, _p("p1")).exists() and _file(tmp_wiki, _p("p2")).exists()
    assert store.versions(_p("p1"))[0]["actor"] == "张三"
    assert store.versions(_p("p2"))[0]["actor"] == "李四"


def test_concurrent_first_writes_both_land_one_create_one_edit(conn, tmp_wiki, member):
    """裁定 R84：create/edit 判定在 per-path 锁内（action=None），并发首写的输家变 edit。"""
    good = _cite(conn, member.message_id, member.quote)

    async def run():
        return await asyncio.gather(
            store.write_page(
                conn, path=_p("race"), title="T1", page_type="entity",
                body_md="一[^c1]", citations=[good], actor="张三", action=None,
            ),
            store.write_page(
                conn, path=_p("race"), title="T2", page_type="entity",
                body_md="二[^c1]", citations=[good], actor="李四", action=None,
            ),
        )

    a, b = asyncio.run(run())
    assert a["ok"] and b["ok"], (a, b)
    assert a["sha"] != b["sha"]
    assert sorted(entry["action"] for entry in store.versions(_p("race"))) == [
        "create", "edit"
    ]


# --- 入参形状 ----------------------------------------------------------------


def test_bad_path_is_refused(conn, tmp_wiki):
    # 2026-10-30 起中文文件名是合法 slug（本地 Hermes vault 就用中文命名），
    # 这里只留真正非法的形状：路径穿越、空格、保留字符、前导 dash、错扩展名、空串。
    bad_paths = (
        "../etc/passwd", "entities/../x.md", "raw/uploads/1-x.md", "index.md",
        "/etc/passwd", "entities/有 空格.md", "entities/方括号[1].md",
        "entities/冒号:名.md", "entities/-负号.md", "entities/a b.md", "entities/x.txt", "",
    )
    for bad in bad_paths:
        with pytest.raises(ValueError):
            store.write_page_sync(
                conn, path=bad, title="T", page_type="entity", body_md="正文",
                citations=[], actor="张三", action="create",
            )
    assert list(tmp_wiki.glob("*.md")) == []


def test_unknown_action_is_refused_before_anything_lands(conn, tmp_wiki, member):
    """R47：action 是原样拼进提交主题的，白名单外的值在落盘之前抛 ValueError。

    S9 起白名单多了 delete（删页也是一次提交），所以它不再算"外面来的坏值"。
    """
    good = _cite(conn, member.message_id, member.quote)
    assert "delete" in store.ACTIONS
    for bad in ("CREATE", "propose", "edit|hack", "改\n一行", ""):
        with pytest.raises(ValueError) as exc:
            _write(conn, slug="act", body_md="正文[^c1]", citations=[good],
                   action=bad)
        assert _chinese(str(exc.value)), bad
        assert "create" in str(exc.value)
        assert not _file(tmp_wiki, _p("act")).exists(), bad
        assert store.versions(_p("act")) == [], bad
        assert _index_and_worktree(tmp_wiki) == ([], ""), bad
    ok = _write(conn, slug="act", body_md="正文[^c1]", citations=[good],
                action="create")
    assert ok["ok"], ok["failed"]
    assert store.versions(_p("act"))[0]["action"] == "create"


def test_read_side_refuses_paths_that_walk_out_of_the_wiki_dir(conn, tmp_wiki):
    """读侧也认 path 形状：HTTP 路径参数 unquote 后可能变成 `../../x`。"""
    from app.config import settings

    outside = settings.data_dir / "secret.md"
    outside.write_text(
        "---\ntitle: 库外的文件\ncreated: 2026-10-23\nupdated: 2026-10-23\n"
        "type: entity\n---\n\n不该被读到\n",
        encoding="utf-8",
    )
    for bad in ("../secret", "../../secret", "entities/../../secret.md", "/etc/passwd",
                "raw/uploads/1-x.md", "index.md", "有中文", "a b", "", None, 1):
        assert store.get_page(conn, bad) is None, bad
        assert store.versions(bad) == [], bad
    for bad in ("../secret", "", None):
        with pytest.raises(ValueError):
            store.page_diff(bad, "0" * 40, "1" * 40)
        with pytest.raises(ValueError):
            store.revert_sync(conn, bad, "0" * 40, "王五")
    # 2026-10-30 起中文裸 slug 是合法形状：没有这一页时按 failed 处理，不是裸异常。
    no_such = store.revert_sync(conn, "有中文", "0" * 40, "王五")
    assert not no_such["ok"] and "没有这一页" in no_such["failed"][0]["note"]
    # 目录里也不许出现点不开的页
    (tmp_wiki / "entities").mkdir()
    (tmp_wiki / "entities" / "a.b.md").write_text(
        "---\ntitle: 怪名字\ncreated: 2026-10-23\nupdated: 2026-10-23\ntype: entity\n---\n\n正文\n",
        encoding="utf-8",
    )
    assert [page["slug"] for page in store.list_pages(conn)] == []
    outside.unlink(missing_ok=True)


def test_chinese_filename_page_roundtrip(conn, tmp_wiki, member):
    """2026-10-30：本地 Hermes 的 vault 用中文文件名，线上要原样接收（不强制拼音）。

    覆盖：显式中文 path 建页、带/不带 .md 与裸 slug 都能读、list/versions 的 slug 是原文、
    全角括号文件名、带 base_commit 改页、删除。
    """
    good = _cite(conn, member.message_id, member.quote)
    rel = "entities/中国新闻史.md"
    first = store.write_page_sync(
        conn, path=rel, title="中国新闻史", page_type="entity",
        body_md="正文[^c1]", citations=[good], actor="Hermes", action="create",
    )
    assert first["ok"], first["failed"]
    assert first["path"] == rel and first["slug"] == "中国新闻史"

    page = store.get_page(conn, rel)
    assert page["path"] == rel and page["slug"] == "中国新闻史"
    assert page["title"] == "中国新闻史"
    # 三种写法都归一到同一个 canonical path（HTTP/聊天里三种都出现过）
    assert store.get_page(conn, "entities/中国新闻史")["path"] == rel
    assert store.get_page(conn, "中国新闻史")["path"] == rel
    assert [item["path"] for item in store.list_pages(conn)] == [rel]
    assert store.versions(rel)[0]["slug"] == "中国新闻史"

    # 全角括号（本地 vault 的 `国际传播（英）.md` 就是这种）
    rel2 = "concepts/国际传播（英）.md"
    second = store.write_page_sync(
        conn, path=rel2, title="国际传播（英）", page_type="concept",
        body_md="正文[^c1]", citations=[good], actor="Hermes", action="create",
    )
    assert second["ok"], second["failed"]
    assert store.get_page(conn, rel2)["slug"] == "国际传播（英）"

    # 中文 path 的乐观锁改页：base_commit 是上一版 head
    edited = store.write_page_sync(
        conn, path=rel, title="中国新闻史", page_type="entity",
        body_md="改后正文[^c1]", citations=[good], actor="Hermes",
        base_commit=first["sha"],
    )
    assert edited["ok"], edited["failed"]
    assert "改后正文" in store.get_page(conn, rel)["body_md"]

    deleted = store.delete_page_sync(conn, rel, actor="Hermes", base_commit=edited["sha"])
    assert deleted["ok"], deleted["failed"]
    assert store.get_page(conn, rel) is None


def test_write_refuses_when_rebase_in_progress(conn, tmp_wiki, member):
    """2026-10-30：版本库卡在 rebase/merge 半途时拒写（旧双向同步的故障模式兜底）。

    那种状态下 commit 会落到 detached HEAD，人工 abort 之后页就没了。旧 timer 已退役，
    这里守住"万一状态还在，写页直接报错并给处理办法"。
    """
    git_dir = tmp_wiki / ".git"
    (git_dir / "rebase-merge").mkdir()
    try:
        with pytest.raises(store.StoreError) as exc:
            store.write_page_sync(
                conn, path="entities/rebase.md", title="T", page_type="entity",
                body_md="正文", citations=[], actor="张三", action="create",
            )
        assert "rebase" in str(exc.value) and "git status" in str(exc.value)
        assert not (tmp_wiki / "entities" / "rebase.md").exists()
    finally:
        (git_dir / "rebase-merge").rmdir()


def test_leading_dash_slug_is_refused(conn, tmp_wiki):
    """裁定 R83：SLUG_RE 禁前导 dash —— 写侧 ValueError、读侧当没有这一页。"""
    with pytest.raises(ValueError):
        store.write_page_sync(
            conn, path="entities/-x.md", title="T", page_type="entity", body_md="正文",
            citations=[], actor="张三", action="create",
        )
    assert not _file(tmp_wiki, "entities/-x.md").exists()
    assert store.get_page(conn, "entities/-x.md") is None
    assert store.versions("entities/-x.md") == []
    assert not store.valid_slug("-x")
    assert store.valid_slug("page-2026-10-22")
    assert not store.valid_page_path("entities/-x.md")


def test_actor_and_note_cannot_break_the_commit_subject(conn, tmp_wiki, member):
    res = _write(
        conn, slug="subj", body_md="正文[^c1]",
        citations=[_cite(conn, member.message_id, member.quote)],
        actor="张|三\n四", action="create", note="合并|两段\n换行",
    )
    assert res["ok"], res["failed"]
    entry = store.versions(res["path"])[0]
    assert (entry["kind"], entry["action"], entry["slug"]) == ("wiki", "create", "subj")
    assert entry["actor"] == "张｜三 四"
    assert entry["note"] == "合并|两段 换行"


def test_new_slug_and_new_path_dedupe_across_directories(conn, tmp_wiki):
    """裁定 R88 + 计划 §1：slug 全库唯一，撞了就 -2、-3……"""
    base = "xu-qiu-ping-shen"
    assert store.new_slug("需求评审") == base
    assert store.new_path("需求评审", "entity") == f"entities/{base}.md"
    (tmp_wiki / "entities").mkdir()
    (tmp_wiki / "entities" / f"{base}.md").write_text("x", encoding="utf-8")
    assert store.new_slug("需求评审") == f"{base}-2"
    # 换个目录也算撞（slug 全库唯一）
    (tmp_wiki / "concepts").mkdir()
    (tmp_wiki / "concepts" / f"{base}-2.md").write_text("x", encoding="utf-8")
    assert store.new_slug("需求评审", "concept") == f"{base}-3"
    assert store.new_slug("Onboarding 流程") == "onboarding-liu-cheng"


# --- 目录摘要 ----------------------------------------------------------------


def test_list_pages_shape_and_summary(conn, tmp_wiki, member):
    first = _write(
        conn, slug="has-summary", title="页", body_md="# 这一页讲报名\n\n正文[^c1]",
        citations=[_cite(conn, member.message_id, member.quote)],
    )
    second = _write(conn, slug="no-summary", title="空页", page_type="concept", body_md="")
    assert first["ok"] and second["ok"], (first, second)

    listed = {page["slug"]: page for page in store.list_pages(conn)}
    assert set(listed) == {"has-summary", "no-summary"}
    assert set(listed["has-summary"]) == {
        "path", "slug", "title", "page_type", "tags", "summary", "created_at",
        "updated_at", "actor",
    }
    assert listed["has-summary"]["summary"] == "这一页讲报名"
    assert listed["has-summary"]["path"] == _p("has-summary")
    assert listed["has-summary"]["tags"] == []
    assert listed["has-summary"]["created_at"] == date.today().isoformat()
    assert listed["no-summary"]["summary"] == ""


# --- 引注来源元数据（S4 读侧纯新增键） ----------------------------------------


# --- 检索重建（语料进 FTS 的 ref_id 是 path） ---------------------------------


def test_reindex_all_rebuilds_pages_from_files(conn, tmp_wiki, member):
    res = _write(
        conn, slug="baoming", title="报名流程线上化", body_md="漏单降到 1 单[^c1]。",
        citations=[_cite(conn, member.message_id, member.quote)],
    )
    assert res["ok"]
    conn.execute("DELETE FROM fts_shared")
    counts = search.reindex_all(conn)
    assert counts["page"] == 1 and counts["message"] == 1
    rows = conn.execute(
        "SELECT ref_id, heading, body FROM fts_shared WHERE kind='page'"
    ).fetchall()
    assert rows[0]["ref_id"] == res["path"]
    assert rows[0]["heading"] == "报名流程线上化"
    assert "漏单" in rows[0]["body"]
    hits = search.search(conn, "漏单降到")
    assert {(hit["kind"], hit["ref_id"]) for hit in hits} >= {("page", res["path"])}


def test_reindex_all_skips_unreadable_and_non_page_files(conn, tmp_wiki, member):
    good = _write(conn, slug="baoming", title="报名流程线上化",
                  body_md="漏单降到 1 单[^c1]。",
                  citations=[_cite(conn, member.message_id, member.quote)])
    assert good["ok"]
    (tmp_wiki / "entities" / "binary.md").write_bytes(b"\xff\xfe\x00\x01 not utf-8")
    (tmp_wiki / "entities" / "nohead.md").write_text("连 front matter 都没有\n", encoding="utf-8")
    (tmp_wiki / "entities" / "a.b.md").write_text(
        "---\ntitle: 怪名字\ncreated: 2026-10-23\nupdated: 2026-10-23\ntype: entity\n---\n\n正文\n",
        encoding="utf-8",
    )
    (tmp_wiki / "index.md").write_text("索引不是页面\n", encoding="utf-8")
    (tmp_wiki / "SCHEMA.md").write_text("schema 不是页面\n", encoding="utf-8")
    (tmp_wiki / "raw" / "uploads").mkdir(parents=True, exist_ok=True)
    (tmp_wiki / "raw" / "uploads" / "1-x.md").write_text("raw 不是页面\n", encoding="utf-8")

    counts = search.reindex_all(conn)
    assert counts == {"message": 1, "upload": 0, "page": 1}, counts
    assert [row["ref_id"] for row in conn.execute(
        "SELECT ref_id FROM fts_shared WHERE kind='page'"
    ).fetchall()] == [good["path"]]
    assert search.search(conn, "索引不是页面") == []
    assert search.search(conn, "raw 不是页面") == []
    # 目录与检索同一个口径
    assert [page["slug"] for page in store.list_pages(conn)] == ["baoming"]


def test_index_page_path_drops_rows_for_deleted_files(conn, tmp_wiki):
    res = _write(conn, slug="gone", body_md="正文")
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM fts_shared WHERE kind='page' AND ref_id=?", (res["path"],)
    ).fetchone()["n"] == 1
    _file(tmp_wiki, res["path"]).unlink()
    search.index_page_path(conn, res["path"])
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM fts_shared WHERE kind='page' AND ref_id=?", (res["path"],)
    ).fetchone()["n"] == 0


# --- S5：ensure_vault / 特殊文件 / index / log --------------------------------


def test_ensure_vault_creates_layout_and_special_files_once(vault, tmp_wiki):
    """ensure_vault 建目录 + SCHEMA/index/log，**一次** commit；再调不产生提交。"""
    for directory in ("entities", "concepts", "comparisons", "queries", "summaries",
                      "raw/uploads"):
        assert (tmp_wiki / directory).is_dir(), directory
    for name in ("SCHEMA.md", "index.md", "log.md"):
        assert (tmp_wiki / name).is_file(), name
    from app.wiki_config import get_config

    # 2026-11-13 修正：SCHEMA 是 llm-wiki 默认骨架，不再由 wiki.toml 生成。
    assert (tmp_wiki / "SCHEMA.md").read_text(encoding="utf-8").startswith("# Wiki Schema\n")
    assert "## tags" in (tmp_wiki / "SCHEMA.md").read_text(encoding="utf-8")
    assert "总页数：0" in (tmp_wiki / "index.md").read_text(encoding="utf-8")
    commits = _count_commits(tmp_wiki)
    assert commits == 1
    tracked = _raw_git(tmp_wiki, "ls-files").split()
    # 2026-11-13 修正：SCHEMA/index 跟页面一起跟踪，不再忽略。
    assert sorted(tracked) == [".gitattributes", ".gitignore", "SCHEMA.md", "index.md", "log.md"]

    result = store.ensure_vault()
    assert result["changed"] == [] and result["changed_any"] is False
    assert _count_commits(tmp_wiki) == commits
    assert _index_and_worktree(tmp_wiki) == ([], "")


def test_ensure_vault_migrates_legacy_layout(vault, tmp_wiki, conn):
    """旧布局一次 ensure_vault：页面按 type 进目录、补 frontmatter，迁移 + 特殊文件合成一次 commit。"""
    from app.config import settings

    for name in ("SCHEMA.md", "index.md", "log.md"):
        (tmp_wiki / name).unlink()
    import shutil
    for directory in serialize.DIRS.values():
        shutil.rmtree(tmp_wiki / directory)
    legacy = (
        "---\ntitle: 报名流程线上化\ntype: topic\n---\n\n"
        "上线首月漏单降到 1 单。\n"
    )
    (tmp_wiki / "baoming.md").write_text(legacy, encoding="utf-8")
    (tmp_wiki / "_outline_.md").write_text(
        "---\ntitle: 大纲\ntype: outline\n---\n\n第一章\n", encoding="utf-8"
    )
    commits_before = _count_commits(tmp_wiki)

    result = store.ensure_vault(conn)
    assert result["migrated"]["changed_any"] is True
    assert (tmp_wiki / "baoming.md").exists() is False
    new_file = tmp_wiki / "entities" / "baoming.md"
    assert new_file.is_file()
    text = new_file.read_text(encoding="utf-8")
    assert "type: entity" in text and "created: " in text and "tags: []" in text
    # 旧 outline 类型已移除：旧 outline 页原样留在根上，不搬去别处。
    assert (tmp_wiki / "_outline_.md").is_file()
    assert (tmp_wiki / "SCHEMA.md").is_file()
    assert "baoming" in (tmp_wiki / "index.md").read_text(encoding="utf-8")
    # 迁移 + SCHEMA/index/log 一次提交
    assert _count_commits(tmp_wiki) == commits_before + 1
    assert store.get_page(conn, "entities/baoming.md")["page_type"] == "entity"
    # 再跑一遍：幂等
    assert store.ensure_vault(conn)["changed_any"] is False
    assert _count_commits(tmp_wiki) == commits_before + 1


def test_read_special_aliases(vault):
    for name in ("index", "index.md", "INDEX", "log", "log.md", "schema", "schema.md",
                 "SCHEMA.md"):
        got = store.read_special(name)
        assert got is not None, name
        assert got["path"] in ("index.md", "log.md", "SCHEMA.md")
        assert isinstance(got["body_md"], str)
        assert got["updated_at"]
    assert store.read_special("raw") is None
    assert store.read_special("entities/x.md") is None
    assert store.read_special(None) is None


def test_read_special_missing_file_returns_none(tmp_wiki):
    assert store.read_special("index.md") is None


def test_rebuild_index_groups_by_type(vault, tmp_wiki, conn):
    from app.config import settings

    out = []
    for slug, page_type, title, body, tags in (
        ("zhu", "entity", "主题页", "主题的摘要", ["增长"]),
        ("gai", "concept", "概念页", "概念的摘要", []),
        ("dui", "comparison", "对比页", "对比的摘要", []),
        ("cha", "query", "查询页", "查询的摘要", []),
        ("zhai", "summary", "摘要页", "摘要的摘要", []),
    ):
        res = store.write_page_sync(
            conn, path=serialize.path_for(slug, page_type), title=title,
            page_type=page_type, body_md=body, citations=[], actor="张三", tags=tags,
        )
        assert res["ok"], res["failed"]
        out.append(res["path"])

    text = store.rebuild_index(conn)
    assert "总页数：5" in text
    labels = {"entity": "实体", "concept": "概念", "comparison": "对比",
              "query": "查询", "summary": "摘要"}
    for page_type in serialize.TYPES:
        assert f"## {labels[page_type]}（1）" in text
    assert "- [[zhu|主题页]] — 主题的摘要（增长）" in text
    assert (settings.wiki_dir / "index.md").read_text(encoding="utf-8") == text
    # 特殊文件不是页面
    assert store.get_page(conn, "index.md") is None
    for rel in out:
        assert rel in [page["path"] for page in store.list_pages(conn)]


def test_special_files_are_not_pages(conn, vault):
    assert store.get_page(conn, "index.md") is None
    assert store.get_page(conn, "SCHEMA.md") is None
    assert store.get_page(conn, "log.md") is None
    assert store.list_pages(conn) == []


def test_append_log_is_append_only(vault, tmp_wiki):
    store.append_log("create", "第一页", "entities/a.md", "张三")
    store.append_log("edit", "第二页", "entities/b.md", "李四")
    text = (tmp_wiki / "log.md").read_text(encoding="utf-8")
    assert text.index("第一页") < text.index("第二页")
    assert text.count("## [") == 2


# --- S5：raw 层 --------------------------------------------------------------


def test_ingest_raw_upload_writes_raw_md_log_and_commit(vault, tmp_wiki, conn, member):
    up_id = int(conn.execute(
        "INSERT INTO upload_files (user_id, orig_filename, stored_path, size, sha256,"
        " parse_status, created_at) VALUES (?, '手册.pdf', 'uploads/1/x.pdf', 10, 'abc123',"
        " 'done', '2026-10-21T00:00:00')",
        (member.user_id,),
    ).lastrowid)
    res = store.ingest_raw_upload(conn, up_id, "手册.pdf", "第三章 报名流程\n漏单降到 1 单\n", "张三")
    assert res["ok"] and res["created"] is True
    rel = f"raw/uploads/{up_id}-pdf.md"
    assert res["path"] == rel
    text = (tmp_wiki / rel).read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "source_file: 手册.pdf" in text
    assert f"upload_id: {up_id}" in text
    assert "sha256: abc123" in text
    assert "ingested: " in text
    assert "漏单降到 1 单" in text
    assert store.read_raw(rel) == text
    # log + commit
    log_text = (tmp_wiki / "log.md").read_text(encoding="utf-8")
    assert f"## [{date.today().isoformat()}] ingest | 手册.pdf" in log_text
    assert f"- path: {rel}" in log_text
    assert res["sha"]
    assert repo.log(rel)[-1].action == "ingest"
    changed = _raw_git(tmp_wiki, "show", "--name-only", "--format=", res["sha"]).split()
    assert sorted(changed) == sorted([rel, "log.md"])
    # raw 不进页面目录/FTS 页面语料
    assert store.get_page(conn, rel) is None
    assert store.list_pages(conn) == []

    # 幂等：再摄一次不重复写 log、不产生第二个 commit
    commits = _count_commits(tmp_wiki)
    again = store.ingest_raw_upload(conn, up_id, "手册.pdf", "第三章\n", "张三")
    assert again["ok"] and again["noop"] is True and again["created"] is False
    assert _count_commits(tmp_wiki) == commits
    assert (tmp_wiki / "log.md").read_text(encoding="utf-8") == log_text


def test_ingest_raw_upload_refuses_empty_text(vault, tmp_wiki, conn, member):
    up_id = int(conn.execute(
        "INSERT INTO upload_files (user_id, orig_filename, stored_path, size, sha256,"
        " parse_status, created_at) VALUES (?, '空.pdf', 'uploads/1/y.pdf', 0, 'x',"
        " 'done', '2026-10-21T00:00:00')",
        (member.user_id,),
    ).lastrowid)
    res = store.ingest_raw_upload(conn, up_id, "空.pdf", "  \n\t ", "张三")
    assert not res["ok"]
    assert _chinese(res["failed"][0]["note"])
    assert not list((tmp_wiki / "raw" / "uploads").glob("*.md"))


def test_ingest_raw_upload_rolls_back_on_commit_failure(
    vault, tmp_wiki, conn, member, monkeypatch
):
    up_id = int(conn.execute(
        "INSERT INTO upload_files (user_id, orig_filename, stored_path, size, sha256,"
        " parse_status, created_at) VALUES (?, '手册.pdf', 'uploads/1/x.pdf', 10, 'abc',"
        " 'done', '2026-10-21T00:00:00')",
        (member.user_id,),
    ).lastrowid)
    before_log = (tmp_wiki / "log.md").read_bytes()

    def boom(paths, subject):
        raise repo.GitError("git commit 失败 (rc=1)")

    monkeypatch.setattr(repo, "commit", boom)
    res = store.ingest_raw_upload(conn, up_id, "手册.pdf", "正文", "张三")
    assert not res["ok"] and _chinese(res["failed"][0]["note"])
    assert not list((tmp_wiki / "raw" / "uploads").glob("*.md"))
    assert (tmp_wiki / "log.md").read_bytes() == before_log
    assert _index_and_worktree(tmp_wiki) == ([], "")


def test_raw_path_for_sanitizes_names():
    assert store.raw_path_for(3, "手册.pdf") == "raw/uploads/3-pdf.md"
    assert store.raw_path_for(3, "../../etc/passwd") == "raw/uploads/3-passwd.md"
    assert store.raw_path_for(3, "") == "raw/uploads/3-upload.md"


def test_read_raw_refuses_escapes(vault, tmp_wiki):
    rel = "raw/uploads/1-x.md"
    (tmp_wiki / "raw" / "uploads").mkdir(parents=True, exist_ok=True)
    (tmp_wiki / rel).write_text("raw 正文\n", encoding="utf-8")
    assert store.read_raw(rel) == "raw 正文\n"
    for bad in ("../secret.md", "raw/../secret.md", "/etc/passwd", "entities/x.md", None):
        assert store.read_raw(bad) is None, bad


# --- S5：lint ----------------------------------------------------------------


def _write_raw(rel: str, text: str, tmp_wiki) -> None:
    path = tmp_wiki / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_lint_clean_vault_has_no_error(findings_fixture_error_free):
    assert findings_fixture_error_free["summary"]["error"] == 0
    assert [f for f in findings_fixture_error_free["findings"] if f["severity"] == "error"] == []


@pytest.fixture
def findings_fixture_error_free(vault, conn, member):
    """两页互相链接、都在 index 里、tags 在 taxonomy 里、来源真实存在 —— 干净 vault。"""
    up_id = int(conn.execute(
        "INSERT INTO upload_files (user_id, orig_filename, stored_path, size, sha256,"
        " parse_status, created_at) VALUES (?, '材料.pdf', 'uploads/1/s.pdf', 10, 'sha9',"
        " 'done', '2026-10-21T00:00:00')",
        (member.user_id,),
    ).lastrowid)
    raw = store.ingest_raw_upload(conn, up_id, "材料.pdf", "来源正文\n", "张三")
    assert raw["ok"], raw
    # 三页各链到另外两页（≥2 出链），且用 slug 链接（不用标题链接），干净 vault 才干净
    a = store.write_page_sync(
        conn, path="entities/shi-ti-ye.md", title="实体页", page_type="entity",
        body_md="见 [[gai-nian-ye]] 与 [[ling-yi-ye]]", citations=[], actor="张三",
        tags=["方法论"], sources=[raw["path"]],
    )
    b = store.write_page_sync(
        conn, path="concepts/gai-nian-ye.md", title="概念页", page_type="concept",
        body_md="见 [[shi-ti-ye]] 与 [[ling-yi-ye]]", citations=[], actor="张三",
        tags=["方法论"],
    )
    c = store.write_page_sync(
        conn, path="concepts/ling-yi-ye.md", title="另一页", page_type="concept",
        body_md="见 [[shi-ti-ye]] 与 [[gai-nian-ye]]", citations=[], actor="张三",
        tags=["方法论"],
    )
    assert a["ok"] and b["ok"] and c["ok"], (a, b, c)
    return store.lint(conn)


def test_lint_reports_every_code(vault, tmp_wiki, conn, member):
    """每条 lint 规则都能被触发；severity 与计划 §2 一致。"""
    long_body = "\n".join(f"第 {index} 行" for index in range(201))
    pages = {
        "broken": store.write_page_sync(
            conn, path=None, title="断链页", page_type="entity",
            body_md="链到 [[根本没有这一页]]", citations=[], actor="张三",
            tags=["不存在的标签"],
        ),
        "orphan": store.write_page_sync(
            conn, path=None, title="孤儿页", page_type="concept",
            body_md="没人链到我", citations=[], actor="张三",
            sources=["raw/uploads/没有这份材料.md"],
        ),
        "contested": store.write_page_sync(
            conn, path=None, title="争议页", page_type="comparison",
            body_md="两说有冲突", citations=[], actor="张三",
            contested=True, contradictions=["orphan"],
        ),
        "long": store.write_page_sync(
            conn, path=None, title="长页", page_type="query",
            body_md=long_body, citations=[], actor="张三",
        ),
    }
    for res in pages.values():
        assert res["ok"], res["failed"]
    # 跨过 90 天且没有 sources → stale
    stale_rel = pages["orphan"]["path"]
    old_day = (date.today() - timedelta(days=200)).isoformat()
    # stale 要求"超过 90 天且没有 sources"：挑那两页没有 sources 的（长页/断链页）
    for rel in (pages["long"]["path"], pages["broken"]["path"]):
        text = (tmp_wiki / rel).read_text(encoding="utf-8").replace(
            f"updated: {date.today().isoformat()}", f"updated: {old_day}"
        )
        (tmp_wiki / rel).write_text(text, encoding="utf-8")
    # 手改 index，让一页 index_missing；再手造一个坏 frontmatter 文件
    broken_rel = pages["broken"]["path"]
    index_text = (tmp_wiki / "index.md").read_text(encoding="utf-8")
    broken_slug = pages["broken"]["slug"]
    lines = [
        line for line in index_text.splitlines()
        if f"[[{broken_slug}|" not in line and f"[[{broken_slug}]]" not in line
    ]
    (tmp_wiki / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _write_raw("entities/no-frontmatter.md", "连 front matter 都没有\n", tmp_wiki)
    # log 轮转：501 条
    (tmp_wiki / "log.md").write_text(
        "".join(f"## [2026-10-23] edit | 第{i}条\n- path: x\n- actor: a\n" for i in range(501)),
        encoding="utf-8",
    )
    # raw 漂移：raw 的 sha256 与上传记录对不上
    up_id = int(conn.execute(
        "INSERT INTO upload_files (user_id, orig_filename, stored_path, size, sha256,"
        " parse_status, created_at) VALUES (?, '手册.pdf', 'uploads/1/x.pdf', 10, 'real',"
        " 'done', '2026-10-21T00:00:00')",
        (member.user_id,),
    ).lastrowid)
    _write_raw(
        f"raw/uploads/{up_id}-pdf.md",
        f"---\nsource_file: 手册.pdf\nupload_id: {up_id}\nsha256: 假的\n"
        f"ingested: 2026-10-23\n---\n\n正文\n",
        tmp_wiki,
    )

    report = store.lint(conn)
    codes = {(finding["code"], finding["severity"]) for finding in report["findings"]}
    by_code = {}
    for finding in report["findings"]:
        by_code.setdefault(finding["code"], []).append(finding)
    expected = {
        "broken_link": "error", "orphan": "warn", "index_missing": "error",
        "frontmatter": "error", "tag_unknown": "warn", "source_missing": "warn",
        "stale": "info", "contested": "info", "page_too_long": "warn",
        "log_rotation": "info", "raw_drift": "error",
    }
    for code, severity in expected.items():
        assert code in by_code, (code, sorted(by_code))
        assert all(item["severity"] == severity for item in by_code[code]), code
        assert all(item["message"] and not item["message"].isascii()
                   for item in by_code[code]), code
    assert any(item["path"] == broken_rel for item in by_code["broken_link"])
    assert report["summary"]["total"] == len(report["findings"])
    assert report["summary"]["error"] >= 2


def test_lint_is_read_only(vault, tmp_wiki, conn):
    store.write_page_sync(
        conn, path=None, title="一页", page_type="entity", body_md="正文",
        citations=[], actor="张三",
    )
    before = {
        rel: (tmp_wiki / rel).read_bytes()
        for rel in ("index.md", "log.md", "SCHEMA.md")
    }
    store.lint(conn)
    for rel, old in before.items():
        assert (tmp_wiki / rel).read_bytes() == old, rel


# --- post-commit DB 失败：git 是事实源，写路径照常 ok=True --------------------


def test_post_commit_fts_failure_twice_then_succeeds(conn, tmp_wiki, member, monkeypatch):
    """FTS 连续失败两次、第三次成功：走满两次重试（0.05/0.1s）后 ok，索引也补上。"""
    import sqlite3

    real = search.index_page_path
    calls = {"n": 0}

    def flaky(conn_arg, rel):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise sqlite3.OperationalError("fts5: database disk image is malformed")
        return real(conn_arg, rel)

    monkeypatch.setattr(store.search, "index_page_path", flaky)
    good = _cite(conn, member.message_id, member.quote)
    res = store.write_page_sync(
        conn, path=_p("pc-fts"), title="T", page_type="entity",
        body_md="v1[^c1]", citations=[good], actor="张三",
    )
    assert res["ok"] is True and res["sha"], res
    assert calls["n"] == 3, calls
    hit = conn.execute(
        "SELECT ref_id FROM fts_shared WHERE kind='page' AND ref_id=?", (res["path"],)
    ).fetchone()
    assert hit is not None


def test_post_commit_always_failing_still_ok_and_only_logs(
    conn, tmp_wiki, member, monkeypatch, caplog
):
    """post-commit DB 永远失败：错误只进日志，页面已经写进 git 就照常报 ok。"""
    import logging
    import sqlite3

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store.search, "index_page_path", boom)
    good = _cite(conn, member.message_id, member.quote)
    with caplog.at_level(logging.ERROR, logger="openllmwiki.wiki"):
        res = store.write_page_sync(
            conn, path=_p("pc-dead"), title="T", page_type="entity",
            body_md="v1[^c1]", citations=[good], actor="张三",
        )
    assert res["ok"] is True and res["sha"], res
    assert _file(tmp_wiki, res["path"]).exists()
    assert len(store.versions(res["path"])) == 1
    assert any(r.levelno == logging.ERROR for r in caplog.records), caplog.records


def test_post_commit_failure_does_not_swallow_git_errors(conn, tmp_wiki, monkeypatch):
    """只接 sqlite3.Error：commit 段之前的 GitError 仍按 failed 处理（不能被兜底吞掉）。"""
    def commit_boom(*args, **kwargs):
        raise repo.GitError("git 非零退出（假装磁盘满了）")

    monkeypatch.setattr(repo, "commit", commit_boom)
    res = store.write_page_sync(
        conn, path=_p("pc-git"), title="T", page_type="entity", body_md="正文",
        citations=[], actor="张三",
    )
    assert res["ok"] is False
    assert res["failed"][0]["note"].startswith("版本库没跑通")
    assert not _file(tmp_wiki, res["path"]).exists()


# --- S9 裁定：CRUD 的“删”（delete_page_sync） --------------------------------


def test_delete_page_removes_file_index_log_and_fts(conn, tmp_wiki, member):
    """建页→删：文件没了、目录不含、GET 式读取 None、index/log 同一次提交更新、FTS 清掉。"""
    res = _write(conn, slug="doomed", body_md="这一页马上要被删掉。")
    assert res["ok"]

    deleted = store.delete_page_sync(conn, res["path"], actor="张三")
    assert deleted["ok"] is True and deleted["action"] == "delete"
    assert deleted["path"] == res["path"] and deleted["sha"]
    assert not _file(tmp_wiki, res["path"]).exists()
    assert store.get_page(conn, res["path"]) is None
    assert [page["slug"] for page in store.list_pages(conn)] == []

    index_text = _file(tmp_wiki, "index.md").read_text(encoding="utf-8")
    assert "doomed" not in index_text
    log_text = _file(tmp_wiki, "log.md").read_text(encoding="utf-8")
    assert "delete" in log_text and res["path"] in log_text

    # 历史还在 git 里：versions 第一条就是 delete 提交
    entries = store.versions(res["path"])
    assert entries and entries[0]["action"] == "delete"
    assert entries[-1]["action"] == "create"

    # FTS 里该页的行被清掉（搜索不会再给出一个点不开的命中）
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM fts_shared WHERE kind = 'page' AND ref_id = ?",
        (res["path"],),
    ).fetchone()["n"] == 0


def test_delete_page_with_stale_base_commit_is_conflict(conn, tmp_wiki):
    """有人先改过：拿着旧 base_commit 删 → ConflictError（与写路径同一套乐观锁），文件还在。"""
    first = _write(conn, slug="contested", body_md="v1")
    _write(conn, slug="contested", body_md="v2", action="edit", base_commit=first["sha"])

    with pytest.raises(store.ConflictError) as caught:
        store.delete_page_sync(
            conn, first["path"], actor="张三", base_commit=first["sha"]
        )
    assert caught.value.diff_text
    assert _file(tmp_wiki, first["path"]).exists(), "冲突时一个字都不许删"


def test_delete_page_missing_or_second_delete_is_failed_in_chinese(conn, tmp_wiki):
    missing = store.delete_page_sync(conn, "entities/nope.md", actor="张三")
    assert missing["ok"] is False
    assert missing["failed"][0]["note"] == "没有这一页"

    bad_shape = store.delete_page_sync(conn, "../x", actor="张三")
    assert bad_shape["ok"] is False and not bad_shape["failed"][0]["note"].isascii()

    res = _write(conn, slug="only-once", body_md="删一次就没了。")
    assert store.delete_page_sync(conn, res["path"], actor="张三")["ok"] is True
    second = store.delete_page_sync(conn, res["path"], actor="张三")
    assert second["ok"] is False
    assert second["failed"][0]["note"] == "没有这一页"


def test_delete_page_commit_failure_restores_the_file(conn, tmp_wiki, monkeypatch):
    """commit 段炸掉：文件逐字退回、不留 failed 之外的残局（历史里也没有 delete 提交）。"""
    res = _write(conn, slug="keep-me", body_md="删不掉就得原样留着。")
    before = _file(tmp_wiki, res["path"]).read_bytes()

    def commit_boom(*args, **kwargs):
        raise repo.GitError("git 非零退出（假装磁盘满了）")

    monkeypatch.setattr(repo, "commit", commit_boom)
    failed = store.delete_page_sync(conn, res["path"], actor="张三")
    assert failed["ok"] is False
    assert failed["failed"][0]["note"].startswith("版本库没跑通")
    assert _file(tmp_wiki, res["path"]).read_bytes() == before
    assert store.get_page(conn, res["path"]) is not None
    assert [entry["action"] for entry in store.versions(res["path"])] == ["create"]


def test_refresh_raw_upload_replaces_body_keeps_frontmatter(conn, tmp_wiki, member):
    """补录图片后刷 raw 正文：frontmatter/sha256 不动，正文换成新解析文本，并提交。"""
    from app.services import uploads as uploads_svc

    # 先造一份上传 + 解析文本 + raw（与真实摄入同一条路）
    parsed = tmp_wiki.parent / "extracted_text" / "raw-refresh.txt"
    parsed.parent.mkdir(parents=True, exist_ok=True)
    parsed.write_text("旧正文：漏单降到 1 单", encoding="utf-8")
    cur = conn.execute(
        "INSERT INTO upload_files (user_id, orig_filename, stored_path, size, sha256,"
        " parse_status, parsed_text_path, created_at)"
        " VALUES (?, '资料.pdf', 'uploads/1/x.pdf', 10, 'abc123', 'done', ?,"
        " '2026-10-30T00:00:00+00:00')",
        (member.user_id, str(parsed.relative_to(tmp_wiki.parent))),
    )
    upload_id = int(cur.lastrowid)
    first = store.ingest_raw_upload(conn, upload_id, "资料.pdf", "旧正文：漏单降到 1 单", "张三")
    assert first["ok"] and first["created"]

    refreshed = store.refresh_raw_upload(
        conn, upload_id, "资料.pdf", "旧正文：漏单降到 1 单\n\n【第 1 页图片】\n图中是漏斗", "reparse"
    )
    assert refreshed["ok"] and refreshed["created"]
    rel = refreshed["path"]
    body = (tmp_wiki / rel).read_text(encoding="utf-8")
    assert body.startswith("---\n")
    assert "sha256: abc123" in body
    assert "漏单降到 1 单" in body and "【第 1 页图片】" in body
    # 再刷一次同样的正文：幂等
    again = store.refresh_raw_upload(
        conn, upload_id, "资料.pdf", "旧正文：漏单降到 1 单\n\n【第 1 页图片】\n图中是漏斗", "reparse"
    )
    assert again["ok"] and again["noop"]


# --- 2026-11-13 修正：SCHEMA/index 属于 wiki，首启不得覆盖本地已有文件 -------


def test_ensure_vault_preserves_existing_schema_and_index(tmp_wiki):
    """首次从本地已有 wiki 启动：SCHEMA/index 原样保留，并跟页面一起跟踪。"""
    (tmp_wiki / "SCHEMA.md").write_text(
        "# Custom Schema\n\n## tags\n\n- 自定义标签\n", encoding="utf-8")
    (tmp_wiki / "index.md").write_text(
        "# Custom Index\n\n> 本地手写目录\n", encoding="utf-8")

    store.ensure_vault()

    assert (tmp_wiki / "SCHEMA.md").read_text(encoding="utf-8") == (
        "# Custom Schema\n\n## tags\n\n- 自定义标签\n")
    assert (tmp_wiki / "index.md").read_text(encoding="utf-8").startswith("# Custom Index")
    tracked = _raw_git(tmp_wiki, "ls-files").split()
    assert "SCHEMA.md" in tracked and "index.md" in tracked


def test_index_upsert_preserves_other_manual_lines(conn, tmp_wiki):
    """服务端写一页只 upsert 自己那一行，不重写别人手写的目录行。"""
    b = store.write_page_sync(
        conn, path="concepts/b.md", title="B 页", page_type="concept",
        body_md="B 正文", actor="张三", note="建 B",
    )
    assert b["ok"]
    index_path = tmp_wiki / "index.md"
    text = index_path.read_text(encoding="utf-8")
    text = text.replace(
        [line for line in text.splitlines() if line.startswith("- [[b|")][0],
        "- [[b|B 页]] — 手工摘要，保留",
    )
    index_path.write_text(text, encoding="utf-8")

    a = store.write_page_sync(
        conn, path="concepts/a.md", title="A 页", page_type="concept",
        body_md="A 正文", actor="张三", note="建 A",
    )
    assert a["ok"]
    after = index_path.read_text(encoding="utf-8")
    assert "- [[b|B 页]] — 手工摘要，保留" in after
    assert "- [[a|A 页]]" in after
