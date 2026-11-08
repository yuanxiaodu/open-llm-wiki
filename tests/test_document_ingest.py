"""整本书后台摄入（S6c）：分块、模型抽取、逐字引注校验、写 vault、任务状态/取消/中断。

模型调用一律 mock（不出网、不花额度）：worker 的注入口子是
`app.services.document_ingest._call_model`，测试 monkeypatch 它；`start_job` 会起线程，
API 形状类测试把 `_spawn` 换成空转，需要跑完整流程的测试直接调 `_run_job(job_id)`。

口径（与任务契约一致）：
- 分块 8000 字符、重叠 600，边界落在换行/句号上；块数由真实分块结果决定；
- 模型输出里的 quote 必须在这一块正文里逐字找得到（忽略空白），否则整页跳过；
- 页面走 `store.write_page_sync` 这一条共享写路径 → citation_checks/FTS/index/log 全自动；
- 模型输出坏 JSON/没有 pages 只跳过这一块（job 照样 done，error 里记几块没解析出来）；
- 模型调用抛异常才把 job 置 failed，error 是中文短句。
"""

from __future__ import annotations

import itertools
import json

import pytest

from app.config import settings
from app.services import document_ingest
from app.wiki import store

_SEQ = itertools.count(1)


# --- 造数据 ------------------------------------------------------------------


def _upload(conn, user_id: int, text: str, *, filename: str = "文档.md",
            status: str = "done", with_text: bool = True) -> int:
    """落一份解析文本 + 一行 upload_files，返回 upload_id（不走 HTTP，省一次解析）。"""
    settings.extracted_text_dir.mkdir(parents=True, exist_ok=True)
    path = settings.extracted_text_dir / f"document-ingest-{next(_SEQ)}.txt"
    if with_text:
        path.write_text(text, encoding="utf-8")
    rel = str(path.relative_to(settings.data_dir))
    return int(conn.execute(
        "INSERT INTO upload_files (user_id, orig_filename, stored_path, size, sha256,"
        " parse_status, parsed_text_path, created_at)"
        " VALUES (?, ?, 'uploads/1/document.pdf', ?, 'sha-document', ?, ?, '2026-10-23T00:00:00')",
        (user_id, filename, len(text), status, rel),
    ).lastrowid)


@pytest.fixture(autouse=True)
def _clean_jobs(conn):
    """每条用例从空的 ingest_jobs 开始（conftest 的 TABLES 不含这张新表）。"""
    conn.execute("DELETE FROM ingest_jobs")
    yield


@pytest.fixture
def no_spawn(monkeypatch):
    """把 start_job 的后台线程换成空转，测试自己决定什么时候调 _run_job。"""
    spawned: list[int] = []
    monkeypatch.setattr(document_ingest, "_spawn", lambda job_id: spawned.append(job_id))
    return spawned


def _pages_payload(pages):
    return {"pages": pages}


def _model_stub(monkeypatch, payload_or_fn):
    calls: list[list[dict]] = []

    def fake(messages):
        calls.append(messages)
        if callable(payload_or_fn):
            return payload_or_fn(messages)
        return payload_or_fn

    monkeypatch.setattr(document_ingest, "_call_model", fake)
    return calls


def _start(conn, upload_id, **kwargs) -> int:
    return int(document_ingest.start_job(conn, upload_id, **kwargs)["job_id"])


# --- 分块 --------------------------------------------------------------------


def test_split_chunks_empty_and_short():
    assert document_ingest.split_chunks("") == []
    assert document_ingest.split_chunks("   \n\t ") == []
    assert document_ingest.split_chunks("只有一句话。") == ["只有一句话。"]


def test_split_chunks_respects_size_and_overlap():
    text = "第一句话内容。\n" * 2000
    chunks = document_ingest.split_chunks(text)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert 0 < len(chunk) <= document_ingest.CHUNK_SIZE
    # 相邻块确实重叠：后一块的开头能在前一块的尾巴里找到（重叠量至少几十字）
    for previous, nxt in zip(chunks, chunks[1:]):
        tail = previous[-document_ingest.CHUNK_OVERLAP:]
        assert tail and tail[:20] in nxt
    # 拼回去覆盖全文（重叠不丢字）
    assert chunks[0].startswith("第一句话内容。")
    assert chunks[-1].endswith("\n")


def test_split_chunks_cuts_on_boundary_not_mid_sentence():
    prefix = "甲" * 7900
    text = prefix + "这一句的结尾在这里。下一句从这里开始" + "乙" * 500
    first = document_ingest.split_chunks(text)[0]
    # 第一块在句号处断开（不是硬切在 8000 的中间）
    assert first.endswith("。")
    assert len(first) <= document_ingest.CHUNK_SIZE


def test_split_chunks_rejects_bad_parameters():
    with pytest.raises(ValueError):
        document_ingest.split_chunks("x" * 100, size=0)
    with pytest.raises(ValueError):
        document_ingest.split_chunks("x" * 100, size=100, overlap=100)


def test_chunks_offsets_are_monotonic():
    text = "句子内容。" * 3000
    chunks = document_ingest._chunks(text)
    assert len(chunks) >= 2
    last_end = 0
    for start, end, chunk in chunks:
        assert start >= 0 and end > start
        assert text[start:end] == chunk
        assert start <= last_end or last_end == 0
        last_end = end


# --- 模型输出解析 --------------------------------------------------------------


def test_parse_json_accepts_plain_and_fenced():
    assert document_ingest._parse_json('{"pages": []}') == {"pages": []}
    assert document_ingest._parse_json('```json\n{"pages": []}\n```') == {"pages": []}
    # 前后带解释文字时，退到第一个 { 到最后一个 }
    assert document_ingest._parse_json('好的：\n{"pages": []}\n以上。') == {"pages": []}


def test_parse_json_rejects_garbage():
    with pytest.raises(document_ingest.ModelOutputError):
        document_ingest._parse_json("完全不是 JSON")
    with pytest.raises(document_ingest.ModelOutputError):
        document_ingest._parse_json('["不是对象"]')


# --- worker：正常跑完 ---------------------------------------------------------


def test_run_job_second_time_skips_existing_titles(conn, vault, member, monkeypatch,
                                                   no_spawn):
    text = "产品经理要用数据说话。"
    up_id = _upload(conn, member.user_id, text)
    payload = _pages_payload([{
        "title": "数据说话", "page_type": "concept", "tags": [],
        "body_md": "产品经理要用数据说话。[[cite:c1]]",
        "citations": [{"key": "c1", "quote": "产品经理要用数据说话"}],
    }])
    _model_stub(monkeypatch, payload)

    first = _start(conn, up_id)
    document_ingest._run_job(first)
    assert document_ingest.get_job(conn, first)["pages_created"] == 1

    second = _start(conn, up_id)
    document_ingest._run_job(second)
    job = document_ingest.get_job(conn, second)
    assert job["status"] == "done"
    assert job["pages_created"] == 0 and job["pages_skipped"] == 1
    assert len(store.list_pages(conn)) == 1  # 没有新增第二页


def test_run_job_bad_model_output_counts_failed_chunk_but_stays_done(
    conn, vault, member, monkeypatch, no_spawn
):
    text = "产品经理要用数据说话。"
    up_id = _upload(conn, member.user_id, text)
    _model_stub(monkeypatch, {"没有": "pages"})
    job_id = _start(conn, up_id)
    document_ingest._run_job(job_id)

    job = document_ingest.get_job(conn, job_id)
    assert job["status"] == "done"
    assert job["chunks_done"] == 1
    assert job["pages_created"] == 0
    assert job["chunks_failed"] == 1
    assert job["quality_status"] == "partial"
    assert not job["knowledge_complete"]
    assert store.list_pages(conn) == []


def test_run_job_model_exception_marks_job_failed(conn, vault, member, monkeypatch,
                                                  no_spawn):
    up_id = _upload(conn, member.user_id, "产品经理要用数据说话。")

    def boom(messages):
        raise RuntimeError("上游 500 了，里面还带点英文 traceback")

    monkeypatch.setattr(document_ingest, "_call_model", boom)
    job_id = _start(conn, up_id)
    document_ingest._run_job(job_id)

    job = document_ingest.get_job(conn, job_id)
    assert job["status"] == "failed"
    assert job["error"] and not job["error"].isascii()  # 中文短句，不是 traceback
    assert "RuntimeError" not in job["error"]
    assert job["finished_at"]


def test_run_job_without_parsed_text_fails_with_chinese_error(conn, vault, member,
                                                              monkeypatch, no_spawn):
    up_id = _upload(conn, member.user_id, "正文")
    job_id = _start(conn, up_id)  # start_job 时文件还在，worker 跑之前删掉
    for path in settings.extracted_text_dir.glob("document-ingest-*.txt"):
        path.unlink()
    document_ingest._run_job(job_id)
    job = document_ingest.get_job(conn, job_id)
    assert job["status"] == "failed"
    assert job["error"] == "这份材料没有可摄入的正文"


# --- 取消 / 中断 -------------------------------------------------------------


def test_cancel_between_chunks(conn, vault, member, monkeypatch, no_spawn):
    # 足够长 → 至少 2 块；模型第一次返回空页并在第一块后置取消标记。
    text = "这一句是文档内容。" * 2000
    up_id = _upload(conn, member.user_id, text)
    job_id = _start(conn, up_id)
    assert document_ingest.get_job(conn, job_id)["chunks_total"] >= 2

    calls = []

    def fake(messages):
        calls.append(1)
        document_ingest._cancel_event(job_id).set()
        return {"pages": []}

    monkeypatch.setattr(document_ingest, "_call_model", fake)
    document_ingest._run_job(job_id)

    job = document_ingest.get_job(conn, job_id)
    assert job["status"] == "cancelled"
    assert job["chunks_done"] == 1
    assert len(calls) == 1  # 第二块没有再去调模型
    assert job["finished_at"]


def test_cancel_pending_job_is_immediate(conn, vault, member, no_spawn):
    up_id = _upload(conn, member.user_id, "产品经理要用数据说话。")
    job_id = _start(conn, up_id)
    result = document_ingest.cancel_job(conn, job_id)
    assert result == {"ok": True, "status": "cancelled"}
    job = document_ingest.get_job(conn, job_id)
    assert job["status"] == "cancelled" and job["finished_at"]
    # worker 之后才拿到锁：看到不是 pending，直接退出，不改状态
    document_ingest._run_job(job_id)
    assert document_ingest.get_job(conn, job_id)["status"] == "cancelled"


def test_cancel_unknown_job_returns_none(conn):
    assert document_ingest.cancel_job(conn, 999999) is None


def test_mark_interrupted_only_touches_running(conn, member):
    now = "2026-10-23T00:00:00+00:00"
    cur = conn.execute(
        "INSERT INTO ingest_jobs (upload_id, actor_id, title, status, created_at, updated_at)"
        " VALUES (?, ?, '跑着的', 'running', ?, ?)",
        ((_upload(conn, member.user_id, "正文"),
          member.user_id, now, now)),
    )
    running_id = int(cur.lastrowid)
    cur = conn.execute(
        "INSERT INTO ingest_jobs (upload_id, actor_id, title, status, created_at, updated_at)"
        " VALUES (?, ?, '排队的', 'pending', ?, ?)",
        ((_upload(conn, member.user_id, "正文"),
          member.user_id, now, now)),
    )
    pending_id = int(cur.lastrowid)

    assert document_ingest.mark_interrupted() >= 1

    running = conn.execute(
        "SELECT status, error FROM ingest_jobs WHERE id=?", (running_id,)
    ).fetchone()
    assert running["status"] == "interrupted"
    assert running["error"] == document_ingest.INTERRUPTED_TEXT
    assert conn.execute(
        "SELECT status FROM ingest_jobs WHERE id=?", (pending_id,)
    ).fetchone()["status"] == "pending"


# --- start_job 校验 ---------------------------------------------------------


def test_start_job_rejects_materials_without_text(conn, vault, member, no_spawn):
    failed = _upload(conn, member.user_id, "解析失败", status="failed")
    with pytest.raises(document_ingest.DocumentIngestError) as exc:
        document_ingest.start_job(conn, failed, member.user_id)
    assert "没有可摄入的正文" in str(exc.value)

    no_text = _upload(conn, member.user_id, "", status="done")
    for path in settings.extracted_text_dir.glob("document-ingest-*.txt"):
        path.unlink()
    with pytest.raises(document_ingest.DocumentIngestError):
        document_ingest.start_job(conn, no_text, member.user_id)

    with pytest.raises(document_ingest.DocumentIngestError):
        document_ingest.start_job(conn, 999999, member.user_id)
    assert no_spawn == []


def test_start_job_does_not_need_a_real_api_key(conn, vault, member, no_spawn):
    """key 只在 worker 真调模型时检查；建任务本身不该因为"没配 key"失败。"""
    up_id = _upload(conn, member.user_id, "产品经理要用数据说话。")
    job = document_ingest.start_job(conn, up_id, member.user_id)
    assert job["job_id"] and job["status"] == "pending"


# --- API ---------------------------------------------------------------------


def test_ingest_document_api_start_list_get_cancel(client, conn, member, member_login,
                                               vault, monkeypatch):
    spawned = []
    monkeypatch.setattr(document_ingest, "_spawn", lambda job_id: spawned.append(job_id))
    up_id = _upload(conn, member.user_id, "产品经理要用数据说话。" * 10)

    resp = client.post("/api/ingest/document", json={"upload_id": up_id, "title": "我的书"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"job_id", "status", "total_chars", "chunks_total"}
    assert body["status"] == "pending" and body["job_id"] in spawned
    job_id = body["job_id"]

    listed = client.get("/api/ingest/jobs?limit=20")
    assert listed.status_code == 200
    jobs = listed.json()["jobs"]
    assert any(job["id"] == job_id for job in jobs)
    found = next(job for job in jobs if job["id"] == job_id)
    assert set(found) >= {
        "id", "upload_id", "actor_id", "title", "status", "total_chars", "chunk_size",
        "cursor", "chunks_total", "chunks_done", "pages_created", "pages_skipped",
        "error", "created_at", "updated_at", "finished_at", "progress",
    }
    assert found["progress"] == 0.0 and found["title"] == "我的书"
    assert found["actor_id"] == member.user_id

    detail = client.get(f"/api/ingest/jobs/{job_id}")
    assert detail.status_code == 200 and detail.json() == found

    cancelled = client.post(f"/api/ingest/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["ok"] is True
    assert client.get(f"/api/ingest/jobs/{job_id}").json()["status"] == "cancelled"


def test_ingest_document_api_400_for_unparsed_material(client, conn, member, member_login,
                                                   vault, monkeypatch):
    monkeypatch.setattr(document_ingest, "_spawn", lambda job_id: None)
    up_id = _upload(conn, member.user_id, "解析失败", status="failed")
    resp = client.post("/api/ingest/document", json={"upload_id": up_id})
    assert resp.status_code == 400
    assert "没有可摄入的正文" in resp.json()["error"]


def test_ingest_document_api_unknown_job_is_404(client, conn, member, member_login, vault):
    assert client.get("/api/ingest/jobs/999999").status_code == 404
    assert client.post("/api/ingest/jobs/999999/cancel").status_code == 404


def test_ingest_document_api_requires_login(client, conn, member):
    up_id = _upload(conn, member.user_id, "正文")
    assert client.post("/api/ingest/document", json={"upload_id": up_id}).status_code == 401
    assert client.get("/api/ingest/jobs").status_code == 401


def test_uploads_api_lists_materials_with_chars(client, conn, member, member_login):
    first = _upload(conn, member.user_id, "一段正文" * 10, filename="先传的.txt")
    second = _upload(conn, member.user_id, "另一段", filename="后传的.txt")
    resp = client.get("/api/uploads")
    assert resp.status_code == 200
    uploads = resp.json()["uploads"]
    by_id = {item["id"]: item for item in uploads}
    assert first in by_id and second in by_id
    assert set(by_id[first]) == {
        "id", "orig_filename", "parse_status", "source_kind", "source_url", "chars",
        "created_at",
    }
    assert by_id[first]["orig_filename"] == "先传的.txt"
    assert by_id[first]["parse_status"] == "done"
    assert by_id[first]["source_kind"] == "file"
    assert by_id[first]["source_url"] is None
    assert by_id[first]["chars"] == len("一段正文" * 10)
    # 新到旧
    assert uploads.index(by_id[second]) < uploads.index(by_id[first])


def test_uploads_api_requires_login(client):
    assert client.get("/api/uploads").status_code == 401
