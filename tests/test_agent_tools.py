"""智能体工具层：六个 @function_tool 打在真库与真 vault 上，不 mock store。

S5 起工具的操作对象是 Hermes 物理 vault 的**相对 path**（entities/concepts/...、
raw/uploads/...、index.md/SCHEMA.md/log.md），不再有 slug 定位与 `_outline_` 大纲页。
`_call` 那一层仍然是 S2 探针实证的口径（SDK 0.24.0）：

1. `on_invoke_tool` 是 **async** 的，得 await（它内部把同步工具丢进 asyncio.to_thread）；
2. 它只认 `ToolContext`：裸 `RunContextWrapper(context=conn)` 会在
   `agents/tool.py:_on_invoke_tool_impl` 第一行的 `ctx.tool_name` 上 AttributeError。

工具函数第一参数照旧叫 `ctx`，conn 从 `ctx.context.conn` 取——`ctx.context` 是 runner
交进来的 AgentContext（Task 3 起，与 hooks 共用同一个对象；裁定 R-S2-2 已实证注入成立）。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import re
import sqlite3
import subprocess

import pytest
from agents.tool_context import ToolContext

from app.agent.context import AgentContext
from app.config import settings
from app.services import search
from app.wiki import repo, store
from tests.conftest import make_human_message

_SEQ = itertools.count(1)


def _call(tool, conn, args: dict, session_id: int = 0, actor_name: str = "张三",
          can_build_wiki: bool = True, message_owner_id: int | None = None,
          user_id: int | None = None) -> str:
    """直接打一个工具，拿回它交给模型的那一串。

    context= 交的是 AgentContext，与 runner 同一个形状。session_id 与 actor_name 是给
    hooks 记 agent_events 用的，工具层只读 .conn。

    can_build_wiki / message_owner_id（2026-10-30）默认 True/None，与迁移前完全一致；
    权限用例显式传 False / user id，模拟 runner 从路由 actor dict 里塞进来的那两个快照。

    `tool_arguments` 必须与 `input` 是同一个串：真跑时 SDK 就是这么给的，而工具的
    failure_error_function 要靠它把原始参数再校验一遍，才报得出中文字段名。
    """
    raw = json.dumps(args, ensure_ascii=False)
    ctx = ToolContext(
        context=AgentContext(
            conn=conn, session_id=session_id, actor_name=actor_name,
            can_build_wiki=can_build_wiki, message_owner_id=message_owner_id, user_id=user_id,
        ),
        tool_name=tool.name, tool_call_id="t1", tool_arguments=raw,
    )
    return asyncio.run(tool.on_invoke_tool(ctx, raw))


def _write(conn, *, title="报名流程线上化", page_type="entity", body_md="正文",
           citations=None, slug=None, tags=None, sources=None, actor="张三") -> dict:
    """直接调 store 造一页（不走工具）：slug 给了就写成 {dir}/{slug}.md，增强可预测性。"""
    path = f"entities/{slug}.md" if slug is not None else None
    res = store.write_page_sync(
        conn, path=path, title=title, page_type=page_type, body_md=body_md,
        citations=citations or [], actor=actor,
        action="create" if path is not None else None, note="人建",
        tags=tags, sources=sources,
    )
    assert res["ok"], res
    return res


def _upload(conn, user_id: int, filename: str, text: str) -> int:
    """落一份解析文本 + 一行 upload_files，返回 upload_id（不走 HTTP，省一次解析）。"""
    settings.extracted_text_dir.mkdir(parents=True, exist_ok=True)
    path = settings.extracted_text_dir / f"agent-tool-{next(_SEQ)}.txt"
    path.write_text(text, encoding="utf-8")
    report = json.dumps(
        {"pages_total": 3, "pages_with_text": 2, "pages_image_only": 1}, ensure_ascii=False
    )
    cur = conn.execute(
        "INSERT INTO upload_files (user_id, orig_filename, stored_path, size, sha256,"
        " parse_status, parsed_text_path, parse_report, created_at)"
        " VALUES (?, ?, ?, ?, ?, 'done', ?, ?, '2026-10-22T00:00:00')",
        (
            user_id, filename, f"uploads/{user_id}/x.txt", len(text.encode("utf-8")),
            "deadbeef", str(path.relative_to(settings.data_dir)), report,
        ),
    )
    upload_id = int(cur.lastrowid)
    search.index_upload(conn, upload_id)
    return upload_id


def _ingest_raw(conn, upload_id: int, filename: str, text: str) -> str:
    res = store.ingest_raw_upload(conn, upload_id, filename, text, "张三")
    assert res["ok"], res
    return res["path"]


# --- 简报逐字照抄的三条 -------------------------------------------------------


def test_write_page_tool_round_trip(conn, tmp_wiki, member):
    from app.agent.tools import write_page

    out = _call(write_page, conn, {
        "title": "报名流程线上化", "page_type": "entity",
        "body_md": "漏单降到 1 单。",
        "sources": ["raw/articles/x.md"],
    })
    assert out.startswith("已生效：path="), out
    path = re.search(r"path=([^\s（]+)", out).group(1)
    assert path == "entities/bao-ming-liu-cheng-xian-shang-hua.md"
    page = store.get_page(conn, path)
    assert page and page["citations"] == [] and page["sources"] == ["raw/articles/x.md"]
    # 返回串约定的另外半截：版本号与引注核对结果都要在里面，模型才知道下一步拿什么当 base_commit
    assert " v1" in out
    assert page["title"] == "报名流程线上化"
    assert page["page_type"] == "entity"
    assert f"edit_page(path=\"{path}\"" in out


def test_write_and_edit_commit_under_the_triggering_member(conn, tmp_wiki, member):
    """2026-10-30 裁定：建页/改页的提交 actor 是触发这一轮的人，note 不再提"智能体"。

    _call 默认 actor_name="张三"（runner 交进来的就是这个人的姓名）。以前工具写死
    AGENT_ACTOR，日志里只看得出"智能体改了一页"，看不出是谁引起的这一串动作。
    """
    from app.agent.tools import edit_page, write_page

    out = _call(write_page, conn, {
        "title": "报名流程线上化", "page_type": "entity",
        "body_md": "第一版正文。",
    })
    path = re.search(r"path=([^\s（]+)", out).group(1)
    created = repo.log()[0]
    assert (created.actor, created.note) == ("张三", "建页"), created

    head = store.get_page(conn, path)["head"]
    out = _call(edit_page, conn, {
        "path": path, "body_md": "第二版正文。", "base_commit": head,
    })
    assert out.startswith("已生效："), out
    edited = repo.log()[0]
    assert (edited.actor, edited.note) == ("张三", "改页"), edited

    # 拿不到姓名（脚本/旧测试裸造上下文）才回退 AGENT_ACTOR，actor 字段永不为空。
    _call(write_page, conn, {
        "title": "没有触发人的一页", "page_type": "concept",
        "body_md": "正文。",
    }, actor_name="")
    assert repo.log()[0].actor == "agent"


def test_read_tool_refuses_outside_whitelist(conn, tmp_wiki):
    from app.agent.tools import read

    empty = _call(read, conn, {"path": ""})
    assert empty.startswith("读不到：") and "只许读" in empty
    for path in ("../../.env", "/etc/passwd", "data/wiki/_outline_.md", "upload/1/2",
                 "entities/nope.md"):
        out = _call(read, conn, {"path": path})
        assert out.startswith("读不到："), (path, out)


def test_read_wiki_page_and_upload_window(conn, tmp_wiki, member):
    from app.agent.tools import read

    res = _write(conn, slug="baoming", body_md="漏单降到 1 单。", tags=["需求洞察"])
    path = res["path"]
    for read_path in (path, f"wiki/{path}", "baoming"):
        page = _call(read, conn, {"path": read_path})
        assert "报名流程线上化" in page and "entity" in page, read_path
        assert "漏单降到 1 单" in page
        assert "需求洞察" in page
    # 现行版本号给出来，模型改页时才交得上 base_commit
    head = store.get_page(conn, path)["head"]
    assert head[:12] in page

    small = _upload(conn, member.user_id, "小结.txt", "整份材料就这么长。")
    assert "整份材料就这么长。" in _call(read, conn, {"path": f"upload/{small}"})

    big_text = "开头这一段是重点。\n" + "填充文字" * 9000  # 3.6 万字符，超过 2 万
    big = _upload(conn, member.user_id, "整本书.txt", big_text)
    window = _call(read, conn, {"path": f"upload/{big}"})
    assert "开头这一段是重点。" in window
    assert len(window) < len(big_text), "整文超过 2 万字符时只给一截窗口"
    assert "还有" in window and "search" in window, "要说清还剩多少、怎么拿到剩下的"

    assert _call(read, conn, {"path": "upload/99999"}).startswith("读不到：")


def test_read_special_files_and_raw(conn, vault, member):
    """read 认 index.md / SCHEMA.md / log.md（大小写不敏感）与 raw/uploads/… 。"""
    from app.agent.tools import read

    _write(conn, slug="baoming", body_md="漏单少了。")
    index = _call(read, conn, {"path": "index.md"})
    assert "知识库索引" in index and "[[baoming|报名流程线上化]]" in index
    schema = _call(read, conn, {"path": "SCHEMA.md"})
    from app.wiki_config import get_config

    assert "## tags" in schema and f"- {get_config().tags[0]}" in schema
    log = _call(read, conn, {"path": "log"})
    assert "## [" in log and "baoming" in log

    upload_id = _upload(conn, member.user_id, "报告.pdf", "第三章 报名流程\n漏单降到 1 单")
    raw_path = _ingest_raw(conn, upload_id, "报告.pdf", "第三章 报名流程\n漏单降到 1 单")
    assert raw_path == f"raw/uploads/{upload_id}-pdf.md"
    raw = _call(read, conn, {"path": raw_path})
    assert "source_file: 报告.pdf" in raw and "漏单降到 1 单" in raw
    # 旧 upload 入口顺带告诉模型 raw 归档路径（sources 要写它）
    upload = _call(read, conn, {"path": f"upload/{upload_id}"})
    assert raw_path in upload
    assert _call(read, conn, {"path": "raw/uploads/999-missing.md"}).startswith("读不到：")


def test_list_wiki_lists_pages_and_raw(conn, vault, member):
    from app.agent.tools import list_wiki

    res = _write(
        conn, slug="baoming", body_md="漏单少了。", tags=["需求洞察"],
    )
    upload_id = _upload(conn, member.user_id, "报名流程.pptx", "第一页的字")
    raw_path = _ingest_raw(conn, upload_id, "报名流程.pptx", "第一页的字")

    out = _call(list_wiki, conn, {})
    assert res["path"] in out and "需求洞察" in out
    assert "entity" in out and "需求洞察" in out and "漏单少了。" in out
    assert f"upload/{upload_id}" in out and "报名流程.pptx" in out
    # parse_report 的口径要给到模型：有文字的页数才引得到
    assert "2" in out and "3" in out
    assert raw_path in out


# --- search：wiki|raw|all ------------------------------------------------------


def test_search_scopes(conn, tmp_wiki, member):
    """scope ∈ wiki|raw|all：wiki 只搜页、raw 搜消息与材料、all 三样都搜。"""
    from app.agent.tools import search as search_tool

    res = _write(
        conn, slug="baoming", body_md="报名流程线上化之后漏单少了。",
    )
    mid = make_human_message(conn, member.session_id, member.user_id,
                             "报名流程线上化我最有感触")
    assert search.index_message(conn, mid) is True
    _upload(conn, member.user_id, "报名流程.txt", "报名流程线上化材料")

    everything = _call(search_tool, conn, {"query": "报名流程线上化", "scope": "all"})
    assert "【消息 " in everything and "【材料 " in everything
    assert f"【页面 {res['path']}】" in everything

    wiki_only = _call(search_tool, conn, {"query": "报名流程线上化", "scope": "wiki"})
    assert f"【页面 {res['path']}】" in wiki_only
    assert "【消息 " not in wiki_only and "【材料 " not in wiki_only

    raw_only = _call(search_tool, conn, {"query": "报名流程线上化", "scope": "raw"})
    assert "【消息 " in raw_only and "【材料 " in raw_only
    assert "【页面 " not in raw_only

    # 命中行里必须带定位符：页面是 path，消息/材料是编号
    assert re.search(r"【消息 (\d+)】", everything)
    assert re.search(r"【材料 (\d+)】", everything)
    nothing = _call(search_tool, conn, {"query": "根本没有这句话", "scope": "all"})
    assert nothing.startswith("没搜到")


def test_search_unknown_scope_is_chinese_whitelist(conn, tmp_wiki):
    """outline scope 随大纲页退休：pydantic 把非法值翻成中文白名单，且不按字母拆开。"""
    from app.agent.tools import search as search_tool

    out = _call(search_tool, conn, {"query": "报名", "scope": "outline"})
    assert out.startswith("读不到：") and "scope" in out and "不合法" in out
    assert "wiki、raw、all" in out, out


def test_search_caps_hits_to_search_limit(conn, tmp_wiki):
    from app.agent.tools import SEARCH_LIMIT, search as search_tool

    for i in range(SEARCH_LIMIT + 3):
        _write(conn, slug=f"awiki{i}", title=f"正文第{i}页", body_md="报名流程线上化")

    out = _call(search_tool, conn, {"query": "报名流程线上化", "scope": "wiki"})
    page_lines = [line for line in out.splitlines() if line.startswith("【页面 ")]
    assert len(page_lines) == SEARCH_LIMIT, out


def test_search_lines_carry_exactly_one_bracket_pair(conn, tmp_wiki):
    """snippet() 自己就用 【】 标命中处：不换掉的话一行里两层同一种括号，
    模型分不清哪个是行首的类别定位符。"""
    from app.agent.tools import search as search_tool

    _write(conn, slug="baoming", body_md="报名流程线上化之后漏单少了。")
    out = _call(search_tool, conn, {"query": "报名流程线上化", "scope": "wiki"})
    lines = [line for line in out.splitlines() if line.startswith("【")]
    assert lines, out
    for line in lines:
        assert line.count("【") == 1 and line.count("】") == 1, line
        assert "「报名流程线上化」" in line, line


# --- edit_page ---------------------------------------------------------------


def test_edit_page_keeps_title_type_and_preserves_tags(conn, tmp_wiki, member):
    from app.agent.tools import edit_page

    res = _write(
        conn, slug="baoming", title="报名流程线上化", body_md=member.content,
        tags=["需求洞察"], sources=["raw/articles/x.md"],
    )
    out = _call(edit_page, conn, {
        "path": res["path"], "body_md": f"{member.content}。补一句。",
        "base_commit": res["sha"],
    })
    assert out.startswith(f"已生效：path={res['path']} v2"), out
    page = store.get_page(conn, res["path"])
    assert page["title"] == "报名流程线上化" and page["page_type"] == "entity"
    assert page["tags"] == ["需求洞察"] and page["sources"] == ["raw/articles/x.md"]
    assert page["citations"] == []


def test_edit_and_delete_require_base_commit(conn, tmp_wiki):
    """2026-10-30：工具层强制 base_commit —— 不带版本号的整页重写/删页会被直接拒绝。"""
    from app.agent.tools import delete_page, edit_page

    res = _write(conn, slug="must-base", body_md="第一版")
    out = _call(edit_page, conn, {"path": res["path"], "body_md": "第二版"})
    assert "base_commit" in out and "必须" in out, out
    assert store.get_page(conn, res["path"])["body_md"].strip() == "第一版"

    out = _call(delete_page, conn, {"path": res["path"]})
    assert "base_commit" in out and "必须" in out, out
    assert store.get_page(conn, res["path"]) is not None


def test_edit_page_missing_page_and_stale_base_commit(conn, tmp_wiki):
    from app.agent.tools import edit_page

    missing = _call(edit_page, conn, {"path": "entities/nope.md", "body_md": "x"})
    assert missing.startswith("没写成：") and "没有这一页" in missing and "write_page" in missing

    bad = _call(edit_page, conn, {"path": "../x", "body_md": "x"})
    assert bad.startswith("没写成：") and "没有这一页" in bad

    res = _write(conn, slug="baoming", body_md="旧正文")
    stale = _call(edit_page, conn, {
        "path": res["path"], "body_md": "新正文", "base_commit": "0" * 40,
    })
    assert stale.startswith("没写成：") and "有人先改了这一页" in stale
    assert store.get_page(conn, res["path"])["body_md"] == "旧正文", "冲突时正文一个字都不许动"


# --- 失败路径：一律中文串，绝不抛异常 -----------------------------------------


def test_write_page_commits_so_other_connections_see_it(tmp_wiki, member):
    """工具自己 commit：后台任务没有 get_conn 那道收尾提交，不 commit 的话
    另一条连接（SSE 尾读、GET /api/wiki/{path}）看不到引注核对留痕。"""
    from app.agent.tools import write_page
    from app.db import connect

    writer = connect()          # 默认 isolation_level：不自动提交
    reader = connect()
    try:
        out = _call(write_page, writer, {
            "title": "报名流程线上化", "page_type": "entity",
            "body_md": "漏单降到 1 单。",
        })
        path = re.search(r"path=([^\s（]+)", out).group(1)
        rows = reader.execute(
            "SELECT ref_id FROM fts_shared WHERE kind = 'page' AND ref_id = ?", (path,)
        ).fetchall()
        assert rows and rows[0]["ref_id"] == path
    finally:
        writer.close()
        reader.close()


def test_a_version_lookup_failure_after_commit_degrades_to_ok(conn, tmp_wiki, member,
                                                               monkeypatch):
    """R-S2-34：commit 成功之后 store.versions 取号失败，降级成 ok，不报"没写成"。

    页已经落盘并提交了（_result 里 conn.commit() 在 versions 之前），这时 versions
    再抛 GitError（git 一时不可用/仓库被锁），旧写法会把成功报成「没写成：…」——模型
    看见会重写一遍、页多出一版重复内容，hooks 判 ok 也翻红。降级后仍以「已生效：」
    开头、带「（版本号没取到）」，ok 判据（不以 FAIL_PREFIX 开头）保持 True。
    """
    from app.agent.tools import FAIL_PREFIX, write_page

    def boom(path):
        raise repo.GitError("fatal: bad revision")

    monkeypatch.setattr(store, "versions", boom)
    out = _call(write_page, conn, {
        "title": "报名流程线上化", "page_type": "entity",
        "body_md": "漏单降到 1 单。",
    })
    assert out.startswith("已生效：path="), out
    assert "版本号没取到" in out, out
    assert not out.startswith(FAIL_PREFIX), out          # hooks 判 ok 保持 True
    # 页是真写进去并提交了：降级发生在 conn.commit() 之后，落盘不受影响
    path = re.search(r"path=([^\s（]+)", out).group(1)
    page = store.get_page(conn, path)
    assert page is not None and page["citations"] == []


@pytest.mark.parametrize("exc", [
    # store.versions 走读侧 _git_read：它把 repo.GitError 翻成 store.StoreError 再抛，
    # 所以真跑时 git 故障冒出来的**不是** GitError 而是 StoreError（裁定 R-S2-35）。
    store.StoreError("版本记录没读出来：git 挂了"),
    # 挂住的 git 由子进程超时抛 TimeoutExpired（subprocess.SubprocessError 的子类）。
    subprocess.TimeoutExpired("git log", 60),
])
def test_version_lookup_wider_failures_after_commit_degrade_to_ok(conn, tmp_wiki, member,
                                                                   monkeypatch, exc):
    """R-S2-35：StoreError 与 TimeoutExpired 也得降级成 ok，不许报「没写成」。"""
    from app.agent.tools import FAIL_PREFIX, write_page

    def boom(path):
        raise exc

    monkeypatch.setattr(store, "versions", boom)
    out = _call(write_page, conn, {
        "title": "报名流程线上化", "page_type": "entity",
        "body_md": "漏单降到 1 单。",
    })
    assert out.startswith("已生效：path="), out
    assert "版本号没取到" in out, out
    assert not out.startswith(FAIL_PREFIX), out          # hooks 判 ok 保持 True
    path = re.search(r"path=([^\s（]+)", out).group(1)
    page = store.get_page(conn, path)
    assert page is not None and page["citations"] == []


def test_write_page_title_clash_names_the_other_page(conn, tmp_wiki):
    """失败路径上的 res["path"] 是"分配了但没写"的名字；只有撞车那条是"撞上的那一页"。"""
    from app.agent.tools import write_page

    args = {
        "title": "报名流程线上化", "page_type": "entity", "body_md": "第一版。",
    }
    first = _call(write_page, conn, args)
    path = re.search(r"path=([^\s（]+)", first).group(1)
    clash = _call(write_page, conn, {**args, "body_md": "第二版。"})
    assert clash.startswith("没写成：") and "这一页已存在" in clash
    assert path in clash and "edit_page" in clash
    assert store.get_page(conn, path)["body_md"] == "第一版。", "撞车那次不许覆盖既有页"
    assert len(store.list_pages(conn)) == 1


def test_write_page_tags_and_sources_round_trip(conn, tmp_wiki):
    from app.agent.tools import write_page

    out = _call(write_page, conn, {
        "title": "带标签的一页", "page_type": "concept", "body_md": "正文。", "tags": ["方法论"], "sources": ["raw/uploads/1-notes.md"],
    })
    assert out.startswith("已生效：path=concepts/"), out
    path = re.search(r"path=([^\s（]+)", out).group(1)
    page = store.get_page(conn, path)
    assert page["tags"] == ["方法论"]
    assert page["sources"] == ["raw/uploads/1-notes.md"]


def test_bad_enum_value_is_chinese_whitelist_not_spelled_out_letters(conn, tmp_wiki):
    """pydantic 的 ctx["expected"] 是一个字符串，直接 join 会一个字符一个字符地拆开。"""
    from app.agent.tools import write_page

    out = _call(write_page, conn, {
        "title": "T", "page_type": "novel", "body_md": "x",
    })
    assert out.startswith("没写成：") and "page_type" in out and "不合法" in out
    assert "entity、concept、comparison、query、summary" in out, out
    assert store.list_pages(conn) == []


def test_lint_tool_reports_findings_after_a_broken_link(conn, vault):
    from app.agent.tools import lint

    clean = _call(lint, conn, {})
    assert clean.startswith("lint 通过"), clean

    _write(conn, slug="broken", body_md="见 [[根本没有这一页]]。")
    out = _call(lint, conn, {})
    assert "lint 发现" in out and "broken_link" in out and "entities/broken.md" in out


def test_lint_tool_lists_every_error_even_when_warnings_are_many(conn, vault, monkeypatch):
    """生产故障回归（2026-10-29）：186 条 warn 把 3 条 error 挤出前 60 条，模型只看到 1 条。

    现在报告必须做到两件事：error 全列；error 排在 warn 前面。这里直接打桩 store.lint，
    造 186 条 warn + 3 条 error 的极端形状，比真造 190 页快得多。
    """
    from app.agent import tools

    findings = [
        {"code": "orphan", "severity": "warn", "path": f"concepts/w{i}.md", "message": "孤立页"}
        for i in range(186)
    ] + [
        {"code": "broken_link", "severity": "error", "path": f"concepts/e{i}.md",
         "message": "按 slug 和标题都找不到对应的页"}
        for i in range(3)
    ]
    monkeypatch.setattr(tools.store, "lint", lambda conn=None: {
        "findings": findings, "summary": {"total": 189, "error": 3, "warn": 186, "info": 0},
    })

    out = tools._lint(None)
    lines = out.splitlines()
    bullets = [line for line in lines if line.startswith("- [")]
    # 3 条 error 一条不少，且都在前 3 条里。
    assert sum(1 for line in bullets if "[error]" in line) == 3
    assert all("[error]" in line for line in bullets[:3])
    assert "error 3 条（全部列出，先修这些）" in out
    assert "warn/info" in out and "没列" in out


def test_write_page_warns_about_broken_links_in_the_result(conn, vault):
    """写页当场就把断链告诉模型（不等收尾 lint、也不拒写）。"""
    from app.agent.tools import write_page

    out = _call(write_page, conn, {
        "title": "断链演示页", "page_type": "concept",
        "body_md": "见 [[还没建的页]] 和 [[另一页]]。",
    })
    assert out.startswith("已生效："), out
    assert "断链" in out and "[[页面标题]]" in out

    clean = _call(write_page, conn, {
        "title": "干净演示页", "page_type": "concept", "body_md": "没有链接的正文。",
    })
    assert clean.startswith("已生效：") and "断链" not in clean



# --- 兜底与装配 ---------------------------------------------------------------


def test_tools_translate_uncaught_errors_into_chinese(conn, tmp_wiki, monkeypatch):
    """每个工具的兜底 except：StoreError / GitError / OSError / sqlite3.Error 都翻中文。"""
    from app.agent import tools

    def _store_error(*_a, **_k):
        raise store.StoreError("目录没读出来：git 挂了")

    def _git_error(*_a, **_k):
        raise repo.GitError("fatal: not a git repository")

    def _os_error(*_a, **_k):
        raise OSError("只读文件系统")

    def _db_error(*_a, **_k):
        raise sqlite3.OperationalError("no such table: fts_shared")

    monkeypatch.setattr(tools.store, "list_pages", _store_error)
    out = _call(tools.list_wiki, conn, {})
    assert out.startswith("读不到：") and "git 挂了" in out

    monkeypatch.setattr(tools.store, "get_page", _store_error)
    out = _call(tools.read, conn, {"path": "entities/baoming.md"})
    assert out.startswith("读不到：")

    monkeypatch.setattr(tools.search_service, "search", _db_error)
    out = _call(tools.search, conn, {"query": "随便搜点什么", "scope": "all"})
    assert out.startswith("读不到：")

    monkeypatch.setattr(tools.store, "write_page_sync", _git_error)
    out = _call(tools.write_page, conn, {
        "title": "T", "page_type": "entity", "body_md": "x",
    })
    assert out.startswith("没写成：") and "not a git repository" in out

    monkeypatch.setattr(tools.store, "get_page", _os_error)
    out = _call(tools.edit_page, conn, {"path": "entities/baoming.md", "body_md": "x"})
    assert out.startswith("没写成：")

    monkeypatch.setattr(tools.store, "lint", _store_error)
    out = _call(tools.lint, conn, {})
    assert out.startswith("读不到：")


def test_wiki_tools_is_the_nine_whitelisted_tools():
    from app.agent.tools import WIKI_TOOLS

    assert [t.name for t in WIKI_TOOLS] == [
        "read", "list_wiki", "search", "write_page", "edit_page", "delete_page",
        "lint", "ingest_url", "ingest_document", "web_search", "ingest_text",
    ]
    assert len(WIKI_TOOLS) == 11
    schema = WIKI_TOOLS[3].params_json_schema
    assert "citations" not in schema["properties"]
    # 长文摄入工具只要一个 upload_id
    document_schema = WIKI_TOOLS[8].params_json_schema
    assert "upload_id" in document_schema["properties"]
    # 删页工具：path 必填 + 可选 base_commit
    delete_schema = WIKI_TOOLS[5].params_json_schema
    assert set(delete_schema["properties"]) == {"path", "base_commit"}


def test_ingest_document_tool_starts_a_background_job(conn, tmp_wiki, member, monkeypatch):
    """ingest_document 工具 = 起一个后台 job，返回 job 号与规模；不自己跑模型、不等结果。"""
    from app.agent import tools
    from app.services import document_ingest

    spawned: list[int] = []
    monkeypatch.setattr(document_ingest, "_spawn", lambda job_id: spawned.append(job_id))
    up_id = _upload(conn, member.user_id, "文档.md", "产品经理要用数据说话。" * 10)

    out = _call(tools.ingest_document, conn, {"upload_id": up_id},
                session_id=member.session_id)
    assert out.startswith("已开始长文摄入：job #"), out
    assert f"upload/{up_id}" in out
    assert "块" in out and "后台" in out
    assert spawned, "工具没有起后台任务"
    row = conn.execute(
        "SELECT status, upload_id FROM ingest_jobs WHERE id = ?", (spawned[0],)
    ).fetchone()
    assert (row["status"], row["upload_id"]) == ("pending", up_id)
    conn.execute("DELETE FROM ingest_jobs")  # conftest 的 TABLES 还不含这张新表


def test_ingest_url_tool_stores_material_and_reports_next_step(conn, tmp_wiki, member,
                                                               monkeypatch):
    """ingest_url 工具与 POST /api/ingest/url 共用服务：返回 upload_id / raw 路径 / 下一步。

    抓取被 monkeypatch（不出网、不花额度）；actor_name 从 AgentContext 反查 users.id，
    并原样记进 raw 归档那次提交的 actor（2026-10-30 裁定：连锁动作归属触发人）。
    """
    from app.agent import tools
    from app.services import web_ingest

    fake_page = {
        "title": "招聘要求解读",
        "text": "这是网页正文：要求三年以上产品经验，另外还要会写需求文档。",
        "url": "https://example.com/job",
    }
    monkeypatch.setattr(tools.web_ingest, "fetch_page", lambda url: dict(fake_page))

    url = "https://example.com/job"
    out = _call(tools.ingest_url, conn, {"url": url}, session_id=member.session_id,
                actor_name="张三")
    assert out.startswith("已摄入：招聘要求解读"), out
    assert "upload_id：" in out
    upload_id = int(re.search(r"upload_id：(\d+)", out).group(1))
    assert "raw 归档：" in out and f"raw/uploads/{upload_id}-" in out
    assert "下一步" in out
    # 网页摄入的 raw 提交也要记到触发这一轮的人名下（不是写死的 AGENT_ACTOR）。
    assert repo.log()[0].actor == "张三", repo.log()[0]

    row = conn.execute(
        "SELECT user_id, source_kind, source_url, parse_status"
        " FROM upload_files WHERE id = ?", (upload_id,)
    ).fetchone()
    assert row["user_id"] == member.user_id
    assert (row["source_kind"], row["source_url"], row["parse_status"]) == (
        "web", "https://example.com/job", "done",
    )
    assert web_ingest is tools.web_ingest


def test_web_search_tool_lists_sources_and_points_at_ingest_url(conn, tmp_wiki, member,
                                                                  monkeypatch):
    """web_search 工具：DeepSeek 搜索被 monkeypatch，返回逐条「标题｜URL」并提示先摄入。"""
    from app.agent import tools

    results = [
        {"title": "DeepSeek 官网", "url": "https://www.deepseek.com/",
         "snippet": "官网", "summary": "DeepSeek 官方站点。",
         "date": "2026-10-01", "site_name": "DeepSeek", "authority": 0.98},
        {"title": "新闻稿", "url": "https://news.example/x",
         "snippet": "新闻", "summary": "", "date": "", "site_name": "", "authority": None},
    ]
    captured: list = []

    def fake_search(query, count=5, need_content=False):
        captured.append({"query": query, "count": count})
        return results

    monkeypatch.setattr(tools.web_search_service, "search_web", fake_search)
    out = _call(tools.web_search, conn, {"query": "DeepSeek", "count": 2},
                session_id=member.session_id, actor_name="张三")
    assert captured == [{"query": "DeepSeek", "count": 2}]
    assert "搜到 2 个网页" in out
    assert "DeepSeek 官网" in out and "https://www.deepseek.com/" in out
    assert "DeepSeek 官方站点。" in out and "2026-10-01" in out
    assert "ingest_url" in out
    # 空结果：给一句人话而不是空串
    monkeypatch.setattr(tools.web_search_service, "search_web",
                        lambda query, count=5, need_content=False: [])
    empty = _call(tools.web_search, conn, {"query": "没有这个词"}, session_id=member.session_id)
    assert empty.startswith("没搜到")


def test_web_search_tool_bad_query_is_chinese_failure(conn, tmp_wiki):
    from app.agent import tools

    out = _call(tools.web_search, conn, {"query": "字" * 129})
    assert out.startswith("读不到：") and "搜索词" in out


def test_tool_body_waits_for_the_context_db_lock(conn, tmp_wiki, member):
    """R-S2-17：工具体从头到尾持着 ctx.context.db_lock。

    relay（事件循环线程）与工具体（asyncio.to_thread 的 worker 线程）共用一条 sqlite
    连接，这把锁是它们之间唯一的闸。这里从反面钉：测试线程先把锁占住，工具就必须等
    ——装饰器没生效的话工具会直接跑完（耗时≈0），耗时断言立刻红。
    """
    import threading
    import time

    from app.agent.tools import WIKI_TOOLS

    tool = next(t for t in WIKI_TOOLS if t.name == "list_wiki")
    agent_ctx = AgentContext(conn=conn, session_id=member.session_id, actor_name="张三")
    raw = "{}"
    ctx = ToolContext(context=agent_ctx, tool_name=tool.name, tool_call_id="t1",
                      tool_arguments=raw)

    started = threading.Event()

    def holder():
        with agent_ctx.db_lock:
            started.set()
            time.sleep(0.4)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert started.wait(2), "测试线程没拿到 db_lock"
    begin = time.monotonic()
    out = asyncio.run(tool.on_invoke_tool(ctx, raw))
    elapsed = time.monotonic() - begin
    t.join(2)

    assert elapsed >= 0.3, f"工具体没有等 db_lock（只花了 {elapsed:.3f}s）"
    assert out.startswith("wiki 页")


def test_delete_page_tool_removes_and_reports_in_chinese(conn, tmp_wiki):
    """delete_page 工具：删除成功给人话 + path，页面/目录里都没了；不存在给失败串。

    顺带钉住归属：删页也记到触发这一轮的人（_call 默认张三）名下，note 是「删页」。
    """
    from app.agent import tools

    res = _write(conn, slug="doomed", body_md="这一页要删掉。")
    out = _call(tools.delete_page, conn, {"path": res["path"], "base_commit": res["sha"]})
    assert out.startswith(f"已删除：path={res['path']}"), out
    deleted = repo.log()[0]
    assert (deleted.actor, deleted.note) == ("张三", "删页"), deleted
    assert store.get_page(conn, res["path"]) is None
    assert not (tmp_wiki / res["path"]).exists()
    assert store.list_pages(conn) == []

    missing = _call(tools.delete_page, conn, {"path": "entities/nope.md"})
    assert missing.startswith("没写成：") and "没有这一页" in missing


def test_delete_page_tool_reports_stale_base_commit(conn, tmp_wiki):
    """delete_page 的乐观锁：base_commit 过期时给模型中文冲突说明，不删。"""
    from app.agent import tools

    first = _write(conn, slug="contested", body_md="v1")
    store.write_page_sync(
        conn, path=first["path"], title="报名流程线上化", page_type="entity",
        body_md="v2", citations=[], actor="张三", action="edit",
        base_commit=first["sha"],
    )
    out = _call(tools.delete_page, conn, {
        "path": first["path"], "base_commit": first["sha"],
    })
    assert out.startswith("没写成：") and "有人先改了这一页" in out
    assert store.get_page(conn, first["path"])["body_md"] == "v2"


# --- 2026-10-30：只有使用权限的成员（can_build_wiki=False）-------------------------
# 判据是 AgentContext.can_build_wiki（runner 从路由 actor dict 取）。关闭后：
# read/list_wiki/search/lint 照常；write_page 只许 query / comparison / summary；
# 改/删/摄入/网页搜索一律 FAIL_TEXT 开头的人话拒绝——不是异常，也不是"读不到"。


def test_query_only_member_can_create_synthesis_pages_but_not_entity(conn, tmp_wiki):
    """没有建设权限：entity 被拒，query / comparison / summary 三类沉淀页放行。

    查询页、对照页、摘要页都是"把已有内容整理成一页"，不算修改既有知识库；
    entity / concept 是建设知识本体，不在只读权限内。
    """
    from app.agent.tools import FAIL_TEXT, write_page

    denied = _call(write_page, conn, {
        "title": "实体页", "page_type": "entity", "body_md": "正文。",
    }, can_build_wiki=False)
    assert denied.startswith(FAIL_TEXT), denied
    assert "只有使用权限" in denied and "建这一类页面" in denied
    assert store.list_pages(conn) == [], "被拒时一个字都不许落盘"

    out = _call(write_page, conn, {
        "title": "这个主题怎么查", "page_type": "query", "body_md": "查询记录。",
    }, can_build_wiki=False)
    assert out.startswith("已生效：path=queries/"), out
    page = store.get_page(conn, "queries/zhe-ge-zhu-ti-zen-me-cha.md")
    assert page is not None

    # 对照页也放行（2026-10-30 委托人二次确认：只读成员可以要求“整合成一张对照页”）
    comparison = _call(write_page, conn, {
        "title": "产品经理类型对照", "page_type": "comparison",
        "body_md": "三种分类的对照。",
    }, can_build_wiki=False)
    assert comparison.startswith("已生效：path=comparisons/"), comparison
    assert store.get_page(conn, "comparisons/chan-pin-jing-li-lei-xing-dui-zhao.md") is not None
    # 归属口径不变：actor 仍是触发这一轮的人，note 是「建页」
    latest = repo.log()[0]
    assert (latest.actor, latest.note) == ("张三", "建页")


def test_query_only_member_cannot_edit_or_delete_pages(conn, tmp_wiki):
    """没有建设权限：edit_page / delete_page 都返回人话拒绝，页面一个字都不动。"""
    from app.agent import tools
    from app.agent.tools import FAIL_TEXT

    res = _write(conn, slug="keep", body_md="原文")
    edited = _call(tools.edit_page, conn, {
        "path": res["path"], "body_md": "被篡改的正文",
    }, can_build_wiki=False)
    assert edited.startswith(FAIL_TEXT), edited
    assert "修改已有页面" in edited
    assert store.get_page(conn, res["path"])["body_md"] == "原文"

    deleted = _call(tools.delete_page, conn, {"path": res["path"]}, can_build_wiki=False)
    assert deleted.startswith(FAIL_TEXT), deleted
    assert "删除页面" in deleted
    assert store.get_page(conn, res["path"]) is not None


def test_query_only_member_cannot_ingest_or_web_search(conn, tmp_wiki, member, monkeypatch):
    """没有建设权限：ingest_url / ingest_document / web_search 直接拒绝，不碰上游也不落库。"""
    from app.agent import tools
    from app.agent.tools import FAIL_TEXT

    def _boom(*_a, **_k):
        raise AssertionError("没有建设权限时不该真的去抓/搜/起任务")

    # 这三个入口的拒绝必须排在真正干活之前：monkeypatch 成 boom，调用了就会炸。
    monkeypatch.setattr(tools.web_ingest, "fetch_page", _boom)
    monkeypatch.setattr(tools.web_search_service, "search_web", _boom)
    monkeypatch.setattr(tools.document_ingest, "_spawn", _boom)

    uploads_before = conn.execute("SELECT COUNT(*) AS n FROM upload_files").fetchone()["n"]
    url_out = _call(tools.ingest_url, conn, {"url": "https://example.com/x"},
                    session_id=member.session_id, can_build_wiki=False)
    assert url_out.startswith(FAIL_TEXT), url_out
    assert "摄入网页材料" in url_out
    assert conn.execute("SELECT COUNT(*) AS n FROM upload_files").fetchone()["n"] == uploads_before

    search_out = _call(tools.web_search, conn, {"query": "随便搜点什么"},
                       session_id=member.session_id, can_build_wiki=False)
    assert search_out.startswith(FAIL_TEXT), search_out
    assert "搜索网页" in search_out

    up_id = _upload(conn, member.user_id, "文档.md", "产品经理要用数据说话。" * 10)
    jobs_before = conn.execute("SELECT COUNT(*) AS n FROM ingest_jobs").fetchone()["n"]
    document_out = _call(tools.ingest_document, conn, {"upload_id": up_id},
                     session_id=member.session_id, can_build_wiki=False)
    assert document_out.startswith(FAIL_TEXT), document_out
    assert "摄入长文档材料" in document_out
    assert conn.execute("SELECT COUNT(*) AS n FROM ingest_jobs").fetchone()["n"] == jobs_before
    conn.execute("DELETE FROM ingest_jobs")   # conftest 的 TABLES 还不含这张新表


def test_search_tool_passes_the_message_owner_filter(conn, tmp_wiki, member):
    """search 工具把 AgentContext.message_owner_id 交给服务层：own 模式下只搜自己的消息。

    runner 会把 access.message_owner_filter 的结果塞进上下文；这里直接钉"工具确实
    把它传下去了"，服务层两个分支的过滤由 tests/test_search.py 钉。
    """
    from app.agent.tools import search as search_tool
    from app.services import auth
    from tests.conftest import make_session

    other_uid = auth.create_member_direct(conn, "李婷", "13700000002", "member-local-2")
    other_sid = make_session(conn, other_uid, "李婷的对话")
    mine = make_human_message(conn, member.session_id, member.user_id, "验收口径在张三这里")
    theirs = make_human_message(conn, other_sid, other_uid, "验收口径在李婷那里")
    for mid in (mine, theirs):
        assert search.index_message(conn, mid) is True

    own = _call(search_tool, conn, {"query": "验收口径", "scope": "raw"},
                session_id=member.session_id, message_owner_id=member.user_id)
    assert f"【消息 {mine}】" in own
    assert f"【消息 {theirs}】" not in own

    everything = _call(search_tool, conn, {"query": "验收口径", "scope": "raw"},
                       session_id=member.session_id)
    assert f"【消息 {mine}】" in everything and f"【消息 {theirs}】" in everything


def test_search_tool_falls_back_to_keywords_when_full_phrase_misses(conn, vault, member):
    """整句短语没命中时按空格拆词再搜（2026-10-30 查询轮回答不出来的根因之一）。

    FTS 的 MATCH 是短语匹配；用户/模型把"产品实习 简历 投递"整串发下来时，几个词
    分散在不同句子就一条都命中不了，模型只好反复换词、把查询轮预算烧光。
    """
    from app.agent.tools import search as search_tool

    _write(conn, slug="qiuzhi", title="求职与简历",
           body_md="产品实习要提前准备简历，投递渠道也要提前整理。")
    out = _call(
        search_tool, conn, {"query": "产品实习 简历 投递", "scope": "wiki"},
        session_id=member.session_id,
    )
    assert out.startswith("搜到"), out
    assert "qiuzhi" in out, out


# --- confidence：工具参数 + 来源广度兜底（2026-10-31 体检修复）-------------------


def test_write_page_confidence_explicit_and_default_from_sources(conn, tmp_wiki):
    """confidence 可由模型显式给；省略时按来源广度兜底（口径见 app/wiki/quality.py）。"""
    from app.agent.tools import write_page

    out = _call(write_page, conn, {
        "title": "显式置信度", "page_type": "concept", "body_md": "正文。",
        "sources": ["raw/articles/a.md"], "confidence": "high",
    })
    path = re.search(r"path=([^\s（]+)", out).group(1)
    assert store.get_page(conn, path)["confidence"] == "medium"
    # 建完 read 回来能看到这个信号（否则模型改页时不知道自己之前定的是什么）。
    from app.agent.tools import read as read_tool

    text = _call(read_tool, conn, {"path": path})
    assert "confidence：medium" in text, text

    cases = [
        (["raw/articles/a.md"], "medium"),                       # 单来源
        (["raw/uploads/10-pdf.md", "raw/uploads/11-x.md"], "medium"),  # 来源数量不能代替证据判断
        ([], "low"),                                             # 没有来源
        # 读书笔记（26）不算独立来源：与它摘录的原书（25）合计 1 份。
        (["raw/uploads/25-pdf.md", "raw/uploads/26-upload.md"], "medium"),
    ]
    for i, (sources, want) in enumerate(cases):
        out = _call(write_page, conn, {
            "title": f"默认置信度{i}", "page_type": "concept",
            "body_md": "正文。", "sources": sources,
        })
        assert out.startswith("已生效："), out
        path = re.search(r"path=([^\s（]+)", out).group(1)
        assert store.get_page(conn, path)["confidence"] == want, (sources, out)


def test_write_page_rejects_illegal_confidence(conn, tmp_wiki):
    from app.agent.tools import write_page

    out = _call(write_page, conn, {
        "title": "非法置信度", "page_type": "concept", "body_md": "x",
        "confidence": "certain",
    })
    assert out.startswith("没写成：") and "confidence" in out, out
    assert "high / medium / low" in out, out
    assert store.list_pages(conn) == []


def test_edit_page_confidence_preserve_then_set(conn, tmp_wiki):
    from app.agent.tools import edit_page

    res = _write(conn, slug="conf-page", body_md="第一版",
                 sources=["raw/articles/x.md"])
    path = res["path"]

    # 1) 现行 confidence 是空的：不传时按来源广度补默认值（单来源 → medium）。
    out = _call(edit_page, conn, {
        "path": path, "body_md": "第二版", "base_commit": res["sha"],
    })
    assert out.startswith("已生效："), out
    page = store.get_page(conn, path)
    assert page["confidence"] == "medium"

    # 2) 显式改。
    out = _call(edit_page, conn, {
        "path": path, "body_md": "第三版",
        "base_commit": page["head"], "confidence": "low",
    })
    assert out.startswith("已生效："), out
    page = store.get_page(conn, path)
    assert page["confidence"] == "low"

    # 3) 不传时保留现行值，不许被打回默认。
    _call(edit_page, conn, {
        "path": path, "body_md": "第四版", "base_commit": page["head"],
    })
    assert store.get_page(conn, path)["confidence"] == "low"

    # 4) 非法值拒绝，且一个字都不写。
    head = store.get_page(conn, path)["head"]
    bad = _call(edit_page, conn, {
        "path": path, "body_md": "第五版", "base_commit": head,
        "confidence": "maybe",
    })
    assert bad.startswith("没写成：") and "confidence" in bad, bad
    assert store.get_page(conn, path)["body_md"].strip() == "第四版"
