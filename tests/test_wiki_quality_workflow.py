"""Behavioural regressions for provenance, publication, and durable compilation."""
from __future__ import annotations

import json
from io import BytesIO

import pytest
from fastapi import UploadFile

from app.agent import tools
from app.config import settings
from app.services import document_ingest, uploads, web_ingest
from app.wiki import lint, provenance, quality, store
from tests.test_agent_tools import _call
from tests.test_document_ingest import _upload


def raw(conn, member, name="source.txt", body="材料原文"):
    saved = uploads.save_upload(conn, None, member.user_id,
                                UploadFile(filename=name, file=BytesIO(body.encode())))
    archived = store.ingest_raw_upload(conn, saved["upload_id"], name, body, "测试")
    conn.commit()
    assert archived["ok"]
    return archived["path"]


def page(conn, title, sources=None, body="正文", **kwargs):
    result = store.write_page_sync(conn, title=title, page_type="concept", body_md=body,
                                  sources=sources, actor="测试", **kwargs)
    conn.commit()
    assert result["ok"]
    return store.get_page(conn, result["path"])


def test_body_tampering_is_detected_independently_of_original_record(conn, vault, member):
    rel = raw(conn, member)
    path = settings.wiki_dir / rel
    meta, body = provenance.split_raw(path.read_text())
    assert meta["sha256"] == provenance.content_hash(body)
    path.write_text(path.read_text().replace("材料原文", "被篡改的正文"))
    report = lint.lint(conn)
    assert any(f["code"] == "raw_drift" and f["path"] == rel for f in report["findings"])


def test_urls_are_not_treated_as_missing_local_files(conn, vault):
    p = page(conn, "网络资料", ["https://example.com/article"])
    findings = lint.lint(conn)["findings"]
    assert not any(f["code"] == "source_missing" and f["path"] == p["path"] for f in findings)


def test_legacy_citations_are_not_page_links():
    assert list(store.iter_wikilinks("事实[[cite:c1]]，见[[real-page]]")) == [("real-page", "real-page")]


def test_same_source_and_derivative_do_not_upgrade_confidence(conn, vault, member):
    rel = raw(conn, member)
    other = settings.wiki_dir / "raw/articles/derivative.md"
    other.write_text(f"---\nderived_from: {rel}\n---\n\n笔记\n")
    assert len(quality.independent_sources([rel, "raw/articles/derivative.md"])) == 1
    assert quality.confidence_for([rel, "raw/articles/derivative.md"], "high") == "medium"
    assert quality.default_confidence([rel, "https://other.example/"]) == "medium"


def test_tool_can_set_clear_and_read_disputes(conn, vault, member):
    rel = raw(conn, member)
    p = page(conn, "争议对象", [rel])
    out = _call(tools.edit_page, conn, {"path": p["path"], "body_md": "保留双方观点",
                "base_commit": p["head"], "contested": True, "contradictions": ["other-page"]})
    assert out.startswith("已生效")
    p = store.get_page(conn, p["path"])
    assert p["contested"] and p["contradictions"] == ["other-page"]
    assert "contested：True" in _call(tools.read, conn, {"path": p["path"]})
    _call(tools.edit_page, conn, {"path": p["path"], "body_md": "已复核",
          "base_commit": p["head"], "contested": False, "contradictions": []})
    assert store.get_page(conn, p["path"])["contradictions"] == []


def test_read_can_reach_material_after_twenty_thousand_characters(conn, vault, member):
    rel = raw(conn, member, body="甲" * 22000 + "后文独有的证据")
    out = _call(tools.read, conn, {"path": rel, "offset": 21950, "limit": 1000})
    assert "后文独有的证据" in out
    assert _call(tools.read, conn, {"path": rel, "offset": -1}).startswith("读不到")


def test_long_page_tail_is_searchable(conn, vault):
    from app.services.search import search
    page(conn, "长页", body="前文" * 12000 + "后文罕见关键词")
    assert search(conn, "后文罕见关键词", kinds=("page",))


def test_scoped_health_does_not_pull_in_unrelated_historical_errors(conn, vault):
    a = page(conn, "本轮页面")
    b = page(conn, "历史页面", body="[[missing-page]]")
    report = lint.lint(conn, paths={a["path"]})
    assert not any(f["path"] == b["path"] for f in report["findings"])
    assert report["checks"]["semantic"] != "passed"


def test_repeated_web_ingest_is_idempotent_and_changed_content_is_snapshot(conn, vault, member, monkeypatch):
    payload = {"title": "网页", "url": "https://EXAMPLE.com/x#section", "text": "原文"}
    monkeypatch.setattr(web_ingest, "fetch_page", lambda url: dict(payload))
    a = web_ingest.ingest_url(conn, payload["url"], member.user_id, "测试")
    b = web_ingest.ingest_url(conn, payload["url"], member.user_id, "测试")
    assert a["upload_id"] == b["upload_id"] and b["unchanged"]
    previous = (settings.wiki_dir / a["raw_path"]).read_text()
    payload["text"] = "变化的新原文"
    c = web_ingest.ingest_url(conn, payload["url"], member.user_id, "测试")
    assert c["source_changed"] and c["upload_id"] != a["upload_id"]
    assert (settings.wiki_dir / a["raw_path"]).read_text() == previous


def test_only_owner_builder_or_admin_can_cancel(client, conn, vault, member_login, other_login, browser, monkeypatch):
    monkeypatch.setattr(document_ingest, "_spawn", lambda job_id: None)
    uid = _upload(conn, member_login.user_id, "原文")
    job = document_ingest.start_job(conn, uid, member_login.user_id)
    other = browser(); other.cookies.update(other_login.cookies)
    assert other.post(f"/api/ingest/jobs/{job['job_id']}/cancel").status_code == 403
    conn.execute("UPDATE users SET can_build_wiki=0 WHERE id=?", (member_login.user_id,)); conn.commit()
    assert client.post(f"/api/ingest/jobs/{job['job_id']}/cancel").status_code == 403


def test_pending_restart_and_duplicate_submissions_use_durable_queue(conn, vault, member, monkeypatch):
    spawned = []
    monkeypatch.setattr(document_ingest, "_spawn", spawned.append)
    uid = _upload(conn, member.user_id, "原文")
    a = document_ingest.start_job(conn, uid, member.user_id)
    b = document_ingest.start_job(conn, uid, member.user_id)
    assert a["job_id"] == b["job_id"] and b["reused"]
    document_ingest.mark_interrupted()
    assert document_ingest.resume_pending() == 1
    assert spawned == [a["job_id"], a["job_id"]]


def test_topic_ingest_updates_existing_page_and_preserves_sources(conn, vault, member, monkeypatch):
    monkeypatch.setattr(document_ingest, "_spawn", lambda job_id: None)
    original = raw(conn, member, "old.txt", "旧事实")
    left, right = page(conn, "相关主题甲"), page(conn, "相关主题乙")
    links = f"[[{left['slug']}]] [[{right['slug']}]]"
    old = page(conn, "同一个主题", [original], "旧事实" + links)
    upload = _upload(conn, member.user_id, "新事实")
    def model(messages):
        if "合并编辑" in messages[0]["content"]:
            return {"retained_existing": True, "pages": [{"title": old["title"], "page_type": "concept",
                    "body_md": "旧事实\n新事实" + links, "tags": []}]}
        return {"pages": [{"title": old["title"], "target_path": old["path"], "page_type": "concept", "body_md": "新事实", "tags": []}]}
    monkeypatch.setattr(document_ingest, "_call_model", model)
    job = document_ingest.start_job(conn, upload, member.user_id)
    document_ingest._run_job(job["job_id"])
    changed = store.get_page(conn, old["path"])
    assert "旧事实" in changed["body_md"] and "新事实" in changed["body_md"]
    assert original in changed["sources"] and len(changed["sources"]) == 2
    job = document_ingest.get_job(conn, job["job_id"])
    assert job["pages_updated"] == 1
    assert conn.execute("SELECT status FROM ingest_candidates WHERE job_id=?", (job["id"],)).fetchone()[0] == "applied"


def test_unacknowledged_merge_retains_original_and_candidate(conn, vault, member, monkeypatch):
    monkeypatch.setattr(document_ingest, "_spawn", lambda job_id: None)
    old = page(conn, "既有主题", body="不能丢的旧事实")
    uid = _upload(conn, member.user_id, "新事实")
    monkeypatch.setattr(document_ingest, "_call_model", lambda messages: {"pages": [{"title": "既有主题", "page_type": "concept", "body_md": "新事实", "tags": []}]})
    job = document_ingest.start_job(conn, uid, member.user_id)
    document_ingest._run_job(job["job_id"])
    assert store.get_page(conn, old["path"])["body_md"] == old["body_md"]
    assert document_ingest.get_job(conn, job["job_id"])["quality_status"] == "partial"
    assert conn.execute("SELECT status FROM ingest_candidates WHERE job_id=?", (job["job_id"],)).fetchone()[0] == "needs_review"


def test_external_file_edit_invalidates_parsed_page_cache(conn, vault):
    p = page(conn, "缓存页面", body="第一版")
    assert store.all_pages()[p["path"]].body_md == "第一版"
    path = settings.wiki_dir / p["path"]
    path.write_text(path.read_text().replace("第一版", "第二版"))
    assert store.all_pages()[p["path"]].body_md == "第二版"


def test_log_rotation_preserves_page_audit_coverage(conn, vault):
    from app.wiki.maintenance import maintain
    p = page(conn, "日志页")
    log = settings.wiki_dir / "log.md"
    original = log.read_text()
    log.write_text(original + "\n".join(f"## [2026-01-01] query | {i}" for i in range(501)))
    maintain(actor="测试")
    assert list(settings.wiki_dir.glob("log-*.md"))
    assert log.read_text().count("## [") < 500
    assert not any(f["code"] == "log_coverage" and f["path"] == p["path"] for f in lint.lint(conn)["findings"])


def test_cancelled_identity_does_not_poison_reused_task_id(conn, vault, member):
    uid = _upload(conn, member.user_id, "材料")
    a = document_ingest._create_job(conn, uid, member.user_id, "", "材料")
    document_ingest.cancel_job(conn, a["job_id"])
    conn.execute("DELETE FROM ingest_jobs WHERE id=?", (a["job_id"],)); conn.commit()
    b = document_ingest._create_job(conn, uid, member.user_id, "", "材料")
    assert not document_ingest._cancel_event(b["job_id"]).is_set()


def test_revoked_permission_is_rechecked_at_write_boundary(conn, vault, member):
    conn.execute("UPDATE users SET can_build_wiki=0 WHERE id=?", (member.user_id,)); conn.commit()
    out = _call(tools.write_page, conn, {"title": "不能越权建核心页", "page_type": "concept", "body_md": "内容"},
                can_build_wiki=True, user_id=member.user_id)
    assert out.startswith("没写成")


def test_pasted_text_archives_actual_message_and_enforces_visibility(conn, vault, member):
    from app.services.text_ingest import ingest_message
    from tests.conftest import make_human_message
    mid = make_human_message(conn, member.session_id, member.user_id, "用户提供的真实原文")
    saved = ingest_message(conn, member.session_id, member.user_id, "测试", mid, member.user_id)
    assert "用户提供的真实原文" in store.read_raw(saved["raw_path"])
    assert ingest_message(conn, member.session_id, member.user_id, "测试", mid, member.user_id)["unchanged"]
    with pytest.raises(ValueError, match="没有可访问"):
        ingest_message(conn, member.session_id, member.user_id, "测试", mid, member.user_id + 1)


def test_identical_text_at_different_urls_is_one_evidence_identity(conn, vault):
    directory = settings.wiki_dir / "raw/articles"
    directory.mkdir(parents=True, exist_ok=True)
    for filename, url in (("a", "https://a.example/post"), ("b", "https://b.example/copy")):
        (directory / f"{filename}.md").write_text(f"---\nsource_url: {url}\n---\n\n完全相同的转载正文\n")
    refs = ["raw/articles/a.md", "raw/articles/b.md"]
    assert len(quality.independent_sources(refs)) == 1
    assert quality.confidence_for(refs, "high") == "medium"


def test_contradiction_list_prevents_high_even_when_flag_is_omitted(conn, vault, member):
    a, b = raw(conn, member, "a.txt", "来源甲"), raw(conn, member, "b.txt", "来源乙")
    p = page(conn, "有矛盾的论断", [a, b], confidence="high", contradictions=["other-page"])
    assert p["contested"] and p["confidence"] == "medium"


def test_automatic_repair_cannot_delete_content(conn, vault):
    import asyncio
    from agents.tool_context import ToolContext
    from app.agent.context import AgentContext
    p = page(conn, "不能自动删除的页面")
    args = json.dumps({"path": p["path"], "base_commit": p["head"]})
    ctx = ToolContext(context=AgentContext(conn=conn, session_id=0, actor_name="测试",
        progress={"repair_paths": {p["path"]}}), tool_name="delete_page", tool_call_id="repair", tool_arguments=args)
    out = asyncio.run(tools.delete_page.on_invoke_tool(ctx, args))
    assert "自动修复不删除" in out
    assert store.get_page(conn, p["path"]) is not None
