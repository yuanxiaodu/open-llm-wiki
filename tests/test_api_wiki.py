"""wiki 接口（/api/wiki*、/api/log*）：S5 物理 vault 的路由口径测试。

写路径本身（校验、乐观锁、恒等闸、index/log 自动维护、多 path 提交、回滚）在
tests/test_store.py 里钉过，这里只钉路由层：状态码与信封形状（509 带 diff、400 引注
失败带 failed、建页撞车的 400 多带一个 "path"）、成功路径的返回字段、特殊文件路由
（index/schema/log/lint）与 `{path:path}` 的注册顺序、以及三条人改写路由的只读闸。

日志相关（2026-10-30 裁定：日志只给管理员）也在这里：/api/log、/api/log/outcomes、
/api/wiki/log 三条的可见性与「会话成果」的形状。

对话可见范围（2026-10-30）落在这两处：单页接口的引注来源脱敏（own 模式下来源会话
不可见就抹掉 who/session_id），以及三条人改路由的建设权限闸（can_build_wiki=0 的成员
403，管理员恒可写）。

S5 起页面 path = `{dir}/{slug}.md`（entities/concepts/comparisons/queries/summaries），
API 同时接受带/不带 .md 与旧裸 slug，返回 canonical path。
"""

from __future__ import annotations

import contextlib
from urllib.parse import quote

from app.config import settings
from app.wiki import repo, store


def _create(client, **over):
    body = {
        "title": "Onboarding Flow", "page_type": "entity", "body_md": "第一版正文",
        "citations": [], "tags": [], "sources": [],
    }
    body.update(over)
    return client.post("/api/wiki", json=body)


def _edit(client, path, **over):
    body = {"body_md": "第二版正文", "base_commit": None, "note": ""}
    body.update(over)
    return client.post(f"/api/wiki/{path}/edit", json=body)


def _citation(key, member, quote=None):
    return {
        "key": key, "source_kind": "message", "source_id": member.message_id,
        "quote": member.quote if quote is None else quote,
    }


def test_anonymous_wiki_routes_are_401(client):
    for path in (
        "/api/wiki", "/api/wiki/entities/whatever", "/api/wiki/index",
        "/api/wiki/schema", "/api/wiki/log", "/api/wiki/lint", "/api/log",
        "/api/log/outcomes",
    ):
        resp = client.get(path)
        assert resp.status_code == 401, path
        assert resp.json()["error"], path


def test_create_read_list_page(client, admin, conn, tmp_wiki):
    """建页 → 目录 → 单页：字段与 S5 契约逐项对齐，path 由服务端发。"""
    resp = _create(client, title="Onboarding 流程")
    assert resp.status_code == 200, resp.text
    created = resp.json()
    assert set(created) == {"ok", "sha", "noop", "failed", "path", "slug"}
    assert created["ok"] is True and created["noop"] is False and created["sha"]
    assert created["path"] == "entities/onboarding-liu-cheng.md"   # 目录由 page_type 决定
    assert created["slug"] == "onboarding-liu-cheng"

    listing = client.get("/api/wiki").json()
    assert set(listing) == {"pages", "count"} and listing["count"] == 1
    item = listing["pages"][0]
    assert set(item) == {
        "path", "slug", "title", "page_type", "tags", "summary",
        "created_at", "updated_at", "actor",
    }
    assert item["path"] == "entities/onboarding-liu-cheng.md"
    assert item["title"] == "Onboarding 流程"
    assert item["page_type"] == "entity"
    assert item["actor"] == "管理员"

    page = client.get("/api/wiki/entities/onboarding-liu-cheng.md").json()
    assert set(page) == {
        "path", "slug", "title", "page_type", "body_md", "head", "tags", "sources",
        "created_at", "updated_at", "confidence", "contested", "contradictions",
        "citations", "wikilinks", "backlinks", "metadata",
    }
    assert page["body_md"] == "第一版正文"
    assert page["head"] == created["sha"]
    assert page["citations"] == [] and page["wikilinks"] == [] and page["backlinks"] == []


def test_unknown_page_is_404(client, admin, tmp_wiki):
    resp = client.get("/api/wiki/entities/nope")
    assert resp.status_code == 404
    assert resp.json() == {"error": "没有这一页"}


def test_path_accepts_dot_md_and_legacy_bare_slug(client, admin, tmp_wiki):
    """计划 §2：URL 里的 path 不带 .md 也认，旧裸 slug 也认，返回 canonical path。"""
    unique = "path-forms-page"
    created = _create(client, title=unique).json()
    canonical = created["path"]
    assert canonical == f"entities/{unique}.md"
    for url in (f"/api/wiki/{canonical}", f"/api/wiki/entities/{unique}",
                f"/api/wiki/{unique}"):
        got = client.get(url)
        assert got.status_code == 200, url
        assert got.json()["path"] == canonical, url


def test_chinese_page_path_over_http(client, admin, conn, tmp_wiki):
    """2026-10-30：中文文件名合法（本地 Hermes vault 就用中文命名），HTTP 三种写法都要能读。"""
    rel = "entities/中国新闻史.md"
    res = store.write_page_sync(
        conn, path=rel, title="中国新闻史", page_type="entity", body_md="正文",
        citations=[], actor="Hermes", action="create",
    )
    assert res["ok"], res["failed"]

    for url in (
        "/api/wiki/entities/中国新闻史",
        "/api/wiki/entities/中国新闻史.md",
        "/api/wiki/中国新闻史",
    ):
        resp = client.get(quote(url, safe="/"))
        assert resp.status_code == 200, (url, resp.text)
        assert resp.json()["path"] == rel
        assert resp.json()["slug"] == "中国新闻史"
    versions = client.get(quote(f"/api/wiki/{rel}/versions", safe="/"))
    assert versions.status_code == 200 and versions.json()[0]["slug"] == "中国新闻史"
    listing = client.get("/api/wiki").json()
    assert any(item["path"] == rel for item in listing["pages"])


def test_create_conflict_is_400_with_clashed_path(client, admin, tmp_wiki):
    """建页撞车：400 {"error","failed","path"}，path 是撞上的那一页；其他 400 不带它。"""
    first = _create(client, title="需求评审怎么开").json()
    second = _create(client, title="需求评审怎么开", body_md="二")
    assert second.status_code == 400
    body = second.json()
    assert set(body) == {"error", "failed", "path"}
    assert body["error"] == "这一页已存在"
    assert body["path"] == first["path"]
    # 第二次一个字都没落盘：页数还是 1，正文还是第一版
    assert client.get("/api/wiki").json()["count"] == 1
    assert client.get(f"/api/wiki/{first['path']}").json()["body_md"] == "第一版正文"


def test_create_bad_type_is_400_without_path_key(client, admin, tmp_wiki):
    """只有撞车那条 400 带 path；type 不在白名单的失败仍是 {"error","failed"} 两键。"""
    resp = _create(client, title="跨越鸿沟", page_type="novel")
    assert resp.status_code == 400
    assert set(resp.json()) == {"error", "failed"}
    assert "type 不在白名单" in resp.json()["error"]
    assert client.get("/api/wiki").json() == {"pages": [], "count": 0}


def test_title_over_cap_is_400(client, admin, tmp_wiki):
    resp = _create(client, title="长" * 121)
    assert resp.status_code == 400
    assert "标题" in resp.json()["error"]
    assert client.get("/api/wiki").json()["count"] == 0


def test_note_over_cap_is_400(client, admin, tmp_wiki):
    created = _create(client).json()
    resp = _edit(client, created["path"], note="记" * 201)
    assert resp.status_code == 400
    assert "改动说明" in resp.json()["error"]


def test_conflict_is_409_with_diff(client, admin, tmp_wiki):
    """备注 N5：409 = {"error": "有人先改了这一页", "diff": <text>}。"""
    created = _create(client).json()
    _edit(client, created["path"], body_md="第二版正文", base_commit=created["sha"])

    resp = _edit(client, created["path"], body_md="迟到的稿子", base_commit=created["sha"])
    assert resp.status_code == 409
    body = resp.json()
    assert set(body) == {"error", "diff"}
    assert body["error"] == "有人先改了这一页"
    assert "第二版正文" in body["diff"]


def test_edit_unknown_page_is_404(client, admin, tmp_wiki):
    resp = _edit(client, "entities/nope.md", body_md="x")
    assert resp.status_code == 404
    assert "没有这一页" in resp.json()["error"]
    assert "path" in resp.json()["error"] or "POST /api/wiki" in resp.json()["error"]


def test_versions_diff_revert(client, admin, tmp_wiki):
    created = _create(client, body_md="第一版正文")
    v1 = created.json()
    v2 = _edit(client, v1["path"], body_md="第二版正文", base_commit=v1["sha"]).json()

    entries = client.get(f"/api/wiki/{v1['path']}/versions").json()
    assert [e["sha"] for e in entries] == [v2["sha"], v1["sha"]]
    assert entries[0]["action"] == "edit" and entries[1]["action"] == "create"
    assert set(entries[0]) == {"sha", "kind", "action", "slug", "actor", "note", "at"}

    diff_body = client.get(
        f"/api/wiki/{v1['path']}/diff?a={v1['sha']}&b={v2['sha']}"
    ).json()["diff"]
    assert "-第一版正文" in diff_body and "+第二版正文" in diff_body

    resp = client.post(f"/api/wiki/{v1['path']}/revert?sha={v1['sha']}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert client.get(f"/api/wiki/{v1['path']}").json()["body_md"] == "第一版正文"
    assert client.get(f"/api/wiki/{v1['path']}/versions").json()[0]["action"] == "revert"


def test_revert_unknown_sha_is_400_with_failed_list(client, admin, tmp_wiki):
    """裁定 R79：回滚没写成与 edit 同形——400 {"error": 人话, "failed": [...]}。"""
    created = _create(client).json()
    resp = client.post(f"/api/wiki/{created['path']}/revert?sha={'ab' * 20}")
    assert resp.status_code == 400
    body = resp.json()
    assert set(body) == {"error", "failed"}
    assert body["error"] and not body["error"].isascii()
    assert len(body["failed"]) == 1
    assert body["failed"][0]["key"] == "" and body["failed"][0]["quote"] == ""
    assert client.get(f"/api/wiki/{created['path']}").json()["body_md"] == "第一版正文"


def test_revert_store_error_is_500_chinese_envelope(client, admin, tmp_wiki, monkeypatch):
    """裁定 R75：revert 的 StoreError → 500 {"error": 人话}。

    走真实路径把 StoreError 逼出来：commit 段炸掉之后 _restore 收拾残局，恢复本身又失败
    （只读挂载、磁盘满的形状）就是 store.StoreError，路由必须接住翻 500 中文信封。
    """
    created = _create(client).json()
    _edit(client, created["path"], body_md="第二版正文", base_commit=created["sha"])

    def commit_boom(*args, **kwargs):
        raise repo.GitError("git 非零退出（假装磁盘满了）")

    def restore_boom(*args, **kwargs):
        raise store.StoreError("回滚没做成，页面可能留着没提交的正文，请手工检查：磁盘满了")

    monkeypatch.setattr(repo, "commit", commit_boom)
    monkeypatch.setattr(store, "_restore", restore_boom)

    resp = client.post(f"/api/wiki/{created['path']}/revert?sha={created['sha']}")
    assert resp.status_code == 500
    body = resp.json()
    assert set(body) == {"error"}, body
    assert body["error"].startswith("回滚没做成"), body["error"]
    assert not body["error"].isascii()


def test_diff_with_bad_rev_is_400(client, admin, tmp_wiki):
    created = _create(client).json()
    resp = client.get(f"/api/wiki/{created['path']}/diff?a=HEAD&b=HEAD")
    assert resp.status_code == 400
    assert resp.json()["error"]


def test_log_lists_commits_newest_first(client, admin, conn, tmp_wiki):
    first = _create(client, title="First Page").json()
    _create(client, title="Second Page")

    entries = client.get("/api/log").json()
    assert [e["slug"] for e in entries] == ["second-page", "first-page"]
    assert set(entries[0]) == {"sha", "kind", "action", "slug", "actor", "note", "at"}
    assert entries[-1]["sha"] == first["sha"]
    assert entries[-1]["note"] == "人建"  # note 空时的兜底


def test_log_outcomes_lists_sessions_with_an_outcome(client, admin, conn, vault):
    """管理员日志页的「会话成果」：只列有过 outcome 的会话，带归属者与最近动静时间。

    计数原来挂在 GET /api/chat/sessions 的 last_outcome 上（对话列表的侧栏小字）；
    2026-10-30 起对话列表不再暴露后台计数，这份数据只走管理员专属的 /api/log/outcomes。
    """
    from app.agent.hooks import append_event

    sid = client.post("/api/chat/sessions").json()["id"]
    append_event(
        conn, sid, "system",
        {"kind": "outcome",
         "pages": [{"path": f"concepts/p{i}.md", "action": "create"} for i in range(3)],
         "citations": 18, "lint_errors": 0},
        message_id=0,
    )
    # 同一时段再开一场没有 outcome 的会话：它不该出现在成果列表里。
    empty = client.post("/api/chat/sessions").json()["id"]

    body = client.get("/api/log/outcomes").json()
    assert set(body) == {"outcomes"}, body
    ids = {row["id"] for row in body["outcomes"]}
    assert sid in ids and empty not in ids, body
    (item,) = [row for row in body["outcomes"] if row["id"] == sid]
    assert set(item) == {"id", "title", "owner_name", "status", "at", "outcome"}
    assert item["owner_name"] == "管理员"
    assert item["status"] == "active"
    # at = COALESCE(last_message_at, started_at)：还没人说话的会话也该有时间。
    assert item["at"]
    assert item["outcome"] == {"pages": 3, "citations": 18, "lint_errors": 0}

    # 同一可见性口径下，log.md 原文仍是管理员可读的 200（vault fixture 已建出 log.md）。
    assert client.get("/api/wiki/log").status_code == 200


def test_log_routes_are_admin_only(client, member_login):
    """日志只给管理员（2026-10-30 裁定）：普通成员三条都是 403 + 人话。

    /api/log（提交摊平）、/api/log/outcomes（会话成果）、/api/wiki/log（log.md 原文）
    是同一份信息的三种形状，可见性必须一条线；信封与其它 /api/* 一样只有 error。
    匿名 401 见 test_anonymous_wiki_routes_are_401。
    """
    for path in ("/api/log", "/api/log/outcomes", "/api/wiki/log"):
        resp = client.get(path)
        assert resp.status_code == 403, (path, resp.status_code, resp.text)
        body = resp.json()
        assert list(body) == ["error"], (path, body)
        assert body["error"] and not body["error"].isascii(), (path, body)


def test_write_path_maintains_index_and_log_in_the_same_commit(client, admin, tmp_wiki):
    """2026-11-13：页面 + log 同一次 commit；index 是生成物，同一次刷新但不进 git。"""
    created = _create(client, title="Maintained Page").json()
    assert len(client.get("/api/log").json()) == 1

    index = client.get("/api/wiki/index").json()
    assert index["path"] == "index.md"
    assert "[[maintained-page|Maintained Page]]" in index["body_md"]
    assert "索引" in index["body_md"]
    assert index["updated_at"]

    log = client.get("/api/wiki/log").json()
    assert log["path"] == "log.md"
    assert "## [" in log["body_md"] and "maintained-page" in log["body_md"]
    assert log["body_md"].count("## [") == 1
    assert created["path"] in log["body_md"]


def test_special_files_and_lint_routes(client, admin, vault):
    """index/schema/log/lint 四个特殊路由：形状固定，且注册在 `{path:path}` 之前。"""
    schema = client.get("/api/wiki/schema").json()
    assert set(schema) == {"path", "body_md", "updated_at"}
    assert schema["path"] == "SCHEMA.md" and "## tags" in schema["body_md"]

    lint = client.get("/api/wiki/lint").json()
    assert set(lint) == {"findings", "summary", "checks", "review_candidates"}
    assert lint["findings"] == []

    # 建一页带断链 + 不带入链：lint 必须报出来
    _create(
        client, title="Broken Link Page",
        body_md="见 [[根本没有这一页]]。",
    )
    lint = client.get("/api/wiki/lint").json()
    codes = {finding["code"] for finding in lint["findings"]}
    assert "broken_link" in codes
    finding = next(f for f in lint["findings"] if f["code"] == "broken_link")
    assert set(finding) == {"code", "severity", "path", "message"}
    assert finding["severity"] == "error"
    assert finding["path"] == "entities/broken-link-page.md"


def test_wikilinks_and_backlinks_resolve_by_slug_then_title(client, admin, tmp_wiki):
    """计划 §2：wikilinks 先按 slug 再按 title 解析；backlinks 扫全库入链。"""
    first = _create(client, title="Refund Flow", body_md="见 [[Support Desk]]。").json()
    page = client.get(f"/api/wiki/{first['path']}").json()
    assert page["wikilinks"] == [
        {"target": "Support Desk", "label": "Support Desk", "path": None}
    ]

    second = _create(client, title="Support Desk", body_md="回到 [[Refund Flow]]。").json()
    page = client.get(f"/api/wiki/{first['path']}").json()
    assert page["wikilinks"] == [
        {"target": "Support Desk", "label": "Support Desk", "path": second["path"]}
    ]
    assert page["backlinks"] == [
        {"path": second["path"], "title": "Support Desk"}
    ]


def test_create_round_trips_frontmatter_extras(client, admin, tmp_wiki):
    """tags/sources/confidence/contested/contradictions 是 S5 新增的 frontmatter 字段。"""
    resp = _create(
        client, title="Tagged Page", tags=["需求洞察", "案例"],
        sources=["raw/uploads/3-notes.md"], confidence="high",
        contested=True, contradictions=["other-slug"],
    )
    assert resp.status_code == 200, resp.text
    page = client.get(f"/api/wiki/{resp.json()['path']}").json()
    assert page["tags"] == sorted(["需求洞察", "案例"])      # store 去重排序
    assert page["sources"] == ["raw/uploads/3-notes.md"]
    assert page["confidence"] == "medium"
    assert page["contested"] is True
    assert page["contradictions"] == ["other-slug"]


def test_list_pages_items_carry_summary(client, admin, tmp_wiki):
    """目录每项有 summary（去 # / 角标、压空白）；没正文给 ""。"""
    _create(client, title="Summary Page", body_md="# 开场白\n\n第二段正文")
    _create(client, title="Empty Page", body_md="")

    listing = {item["path"]: item for item in client.get("/api/wiki").json()["pages"]}
    assert listing["entities/summary-page.md"]["summary"] == "开场白"
    assert listing["entities/empty-page.md"]["summary"] == ""


# ------------------------------------------ S4-R-S4-20：只读闸（S5 继续成立）


READONLY_TEXT = "wiki 现在只能查看，内容由智能体维护"


@contextlib.contextmanager
def _wiki_readonly(on: bool = True):
    """把 frozen settings 的 wiki_readonly 临时改成 on，退出时还原。

    测试进程 conftest 里设的是 WIKI_READONLY=0（既有写接口用例靠它造数据），只读这条
    裁定单独在本文件里开关。frozen dataclass 挡 setattr，用 object.__setattr__ 绕过；
    不还原的话，同进程后面的用例会全 403。
    """
    prev = settings.wiki_readonly
    object.__setattr__(settings, "wiki_readonly", on)
    try:
        yield
    finally:
        object.__setattr__(settings, "wiki_readonly", prev)


def test_readonly_gate_blocks_three_human_write_routes(client, admin, browser, conn, tmp_wiki):
    """裁定 R-S4-20：只读期间三条人改路由一律 403 中文，且闸在收参/校验之前。

    这个测试同时钉四件事：
    1. 三条写路由（建页/编辑/回滚）——合法请求体、超长标题（本该 400）、过期
       base_commit（本该 409）、缺 sha（本该 422）、字段类型不对（本该 422）——全都先
       得到同一个 403，说明闸确实跑在 Pydantic 字段校验与 query 校验之前；
    2. GET 目录/单页/特殊文件照常 200（只读 = 能看）；
    3. "只读不影响 agent 的 store 写路径"：store.write_page_sync 不经过 HTTP，照常写成功；
    4. 匿名者仍是 401（只读状态不泄给没登录的人）。
    """
    assert settings.wiki_readonly is False  # conftest 给测试进程显式关掉了
    with _wiki_readonly():
        # 只读闸只拦人的这一面：智能体工具直接调 store，照常写成
        written = store.write_page_sync(
            conn, path="entities/ro.md", title="只读页", page_type="entity",
            body_md="人只能看的一版", citations=[], actor="管理员", action="create",
        )
        assert written["ok"] is True, written
        head = written["sha"]

        # 1) 建页：合法体也 403
        resp = _create(client, body_md="写不进去")
        assert resp.status_code == 403, resp.text
        assert resp.json() == {"error": READONLY_TEXT}

        # 2) 编辑：超长改动说明（本该 400）先撞只读闸
        resp = _edit(client, "entities/ro.md", note="记" * 201)
        assert resp.status_code == 403 and resp.json() == {"error": READONLY_TEXT}

        # 3) 编辑：过期 base_commit（本该 409）同样先 403
        resp = _edit(client, "entities/ro.md", body_md="迟到的一版", base_commit="0" * 40)
        assert resp.status_code == 403 and resp.json() == {"error": READONLY_TEXT}

        # 4) 回滚：连 sha 都不给（本该 422）也先 403 —— 闸确实在 query 校验之前
        resp = client.post("/api/wiki/entities/ro.md/revert")
        assert resp.status_code == 403 and resp.json() == {"error": READONLY_TEXT}

        # 5) 字段类型不对（本该 422）：body_params 的校验也排在依赖之后，仍是 403
        resp = client.post("/api/wiki", json={
            "title": "新页", "page_type": "entity", "body_md": "x", "citations": "不是数组",
        })
        assert resp.status_code == 403 and resp.json() == {"error": READONLY_TEXT}

        # 6) 没登录：401「请先登录」，不把只读状态泄给匿名者
        resp = browser().post("/api/wiki", json={
            "title": "新页", "page_type": "entity", "body_md": "x", "citations": [],
        })
        assert resp.status_code == 401
        assert resp.json() == {"error": "请先登录"}

        # 只看的路由照常 200
        listing = client.get("/api/wiki")
        assert listing.status_code == 200
        assert [item["path"] for item in listing.json()["pages"]] == ["entities/ro.md"]
        page = client.get("/api/wiki/entities/ro.md")
        assert page.status_code == 200
        assert page.json()["body_md"] == "人只能看的一版"
        assert page.json()["head"] == head
        assert client.get("/api/wiki/index").status_code == 200
        assert client.get("/api/wiki/lint").status_code == 200
        # 被挡下的三次写一个字都没落：现行版本还是 store 写的那一版
        assert client.get("/api/wiki/entities/ro.md").json()["body_md"] == "人只能看的一版"
        assert client.get("/api/log").json()[0]["sha"] == head

    # 闸还原之后人又能写了（不靠后续 fixture 的隐式状态）
    assert settings.wiki_readonly is False
    assert _edit(client, "entities/ro.md", body_md="人又改回来了").status_code == 200


def test_versions_and_diff_404_for_an_unknown_page(client, admin, tmp_wiki):
    """P2-10③：页面不存在时版本/差异接口要 404，不是 200 + 空列表。"""
    missing = "/api/wiki/entities/nope"
    resp = client.get(f"{missing}/versions")
    assert resp.status_code == 404
    assert resp.json() == {"error": "没有这一页"}

    resp = client.get(f"{missing}/diff?a=abcde&b=bcdef")
    assert resp.status_code == 404
    assert resp.json() == {"error": "没有这一页"}

    # 修订号形状闸仍在 404 之前：坏 rev 是 400（客户端错误优先说清楚）
    resp = client.get(f"{missing}/diff?a=HEAD&b=HEAD")
    assert resp.status_code == 400
    assert resp.json()["error"]

    # 存在的页照旧 200
    created = _create(client).json()
    assert client.get(f"/api/wiki/{created['path']}/versions").status_code == 200
    diff = client.get(
        f"/api/wiki/{created['path']}/diff?a={created['sha']}&b={created['sha']}"
    )
    assert diff.status_code == 200


# ------------------ 2026-10-30：引注来源脱敏 + 人写 wiki 要建设权限 -------------------
# site_settings 不在 conftest._wipe 的清表清单里，翻开关的用例必须自己还原，否则同进程
# 后面的用例全变成 own 模式（报错还会离真正的原因很远）。


@contextlib.contextmanager
def _own_only(conn):
    """把对话可见范围临时切成 own；退出时还原成进入前的值（不是写死 all）。"""
    from app.services import site_settings

    previous = site_settings.get_chat_visibility(conn)
    site_settings.set_chat_visibility(conn, "own", None)
    try:
        yield
    finally:
        site_settings.set_chat_visibility(conn, previous, None)


def _admin_client(browser):
    """另开一个管理员浏览器（不动 client 的身份，和 _li_client 同一个手法）。"""
    from tests.conftest import ADMIN_PASSWORD, ADMIN_PHONE, api_login, make_admin

    make_admin()
    who = browser()
    who.cookies.update(api_login(who, ADMIN_PHONE, ADMIN_PASSWORD))
    return who


def test_human_wiki_writes_require_build_permission(
    client, conn, member_login, browser, tmp_wiki
):
    """没有建设权限的成员：建/改/回滚一律 403 人话，且在收参之前就被挡下。

    与只读闸共用一条依赖：先判账号能力（can_build_wiki），再判全站只读开关。
    管理员恒有建设权限，照常能写。
    """
    from app.routers.api import wiki as wiki_route

    created = _create(client, title="有权限时建的页").json()
    conn.execute(
        "UPDATE users SET can_build_wiki = 0 WHERE id = ?", (member_login.user_id,)
    )
    denied = {"error": wiki_route.NO_BUILD_TEXT}

    responses = (
        _create(client, title="关了权限还想建"),
        _edit(client, created["path"], body_md="关了权限还想改"),
        client.post(f"/api/wiki/{created['path']}/revert?sha={created['sha']}"),
        # 字段类型不对（本该 422）也先撞能力闸：闸在 body 校验之前
        client.post("/api/wiki", json={
            "title": "新页", "page_type": "entity", "body_md": "x", "citations": "不是数组",
        }),
    )
    for resp in responses:
        assert resp.status_code == 403, (resp.status_code, resp.text)
        assert resp.json() == denied

    # 被拒的三次写一个字都没落：正文还是原样，页数还是 1
    assert client.get(f"/api/wiki/{created['path']}").json()["body_md"] == "第一版正文"
    assert client.get("/api/wiki").json()["count"] == 1

    # 管理员恒有建设权限（不受 users.can_build_wiki 影响）
    admin = _admin_client(browser)
    assert _create(admin, title="管理员建的页").status_code == 200
