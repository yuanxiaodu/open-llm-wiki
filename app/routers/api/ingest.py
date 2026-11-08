"""摄入接口：网页来源（S6b）+ 整本书后台任务（S6c）。

网页（已有）：
- `POST /api/ingest/url {url}` → `app.services.web_ingest.ingest_url`（DeepSeek Fetch）；
- `POST /api/ingest/search {query, count?, need_content?}` → 规范化候选网页清单
  （DeepSeek Search，可选——给前端"先搜再挑一个摄入"的入口）。

整本书（S6c）：
- `POST /api/ingest/document {upload_id, title?}` → 起一个后台 job（分块调模型抽页 + 逐字
  引注校验 + 写 vault），返回 `{job_id,status,total_chars,chunks_total}`；
- `GET /api/ingest/jobs?limit=20` / `GET /api/ingest/jobs/{id}` → 任务状态（表列 + progress）；
- `POST /api/ingest/jobs/{id}/cancel` → 取消（pending 立即、running 块间生效）。

材料没正文/没有这份上传 → 400 中文人话；没这个任务 → 404。网页抓取失败由服务层抛
`WebIngestError`（中文），这里翻 400；不自动起智能体跑。
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...deps import Conn, RequiredUser, RequiredWikiBuilder
from ...services import access, document_ingest, web_ingest, web_search

router = APIRouter()


class IngestUrlBody(BaseModel):
    url: str


class IngestSearchBody(BaseModel):
    query: str
    count: int = 5
    need_content: bool = False


class IngestDocumentBody(BaseModel):
    """长文摄入的入参：哪份材料 + 可选的显示标题（空则任务里留空，前端用文件名顶）。"""

    upload_id: int
    title: str = ""


@router.post("/api/ingest/url")
def ingest_url(body: IngestUrlBody, conn: sqlite3.Connection = Conn,
               user=RequiredWikiBuilder):
    """抓一个网页并落成一份材料；返回 upload_id / raw_path / title / url / chars。

    400 是"这个链接抓不了"（形状不对、上游拒绝、抓取失败、没有正文），消息是中文人话。
    """
    try:
        return web_ingest.ingest_url(conn, body.url, user["id"], user["name"])
    except web_ingest.WebIngestError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(500, f"摄入没做成：{exc}") from exc


@router.post("/api/ingest/search")
def ingest_search(body: IngestSearchBody, user=RequiredWikiBuilder):
    """用 DeepSeek Search 搜候选网页：返回 {results:[{title,url,snippet,summary,date,site_name,authority}]}。

    只是线索，不落库；要引用就先 `POST /api/ingest/url` 把正文抓进 raw。
    """
    try:
        return {"results": web_search.search_web(
            body.query, count=body.count, need_content=body.need_content,
        )}
    except web_ingest.WebIngestError as exc:
        raise HTTPException(400, str(exc)) from exc


# --- S6c：整本书后台摄入 ------------------------------------------------------


@router.post("/api/ingest/document")
def ingest_document(body: IngestDocumentBody, conn: sqlite3.Connection = Conn,
                user=RequiredWikiBuilder):
    """起一个长文摄入任务：材料必须已解析（parse_status='done'）且有正文。

    返回 `{job_id,status,total_chars,chunks_total}`；进度看 /api/ingest/jobs/{id}。
    """
    try:
        return document_ingest.start_job(
            conn, body.upload_id, user["id"], title=body.title,
        )
    except document_ingest.DocumentIngestError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/api/ingest/jobs")
def ingest_jobs(limit: int = 20, conn: sqlite3.Connection = Conn, user=RequiredUser):
    """任务列表（新到旧，limit 夹在 1..100）：每条是表列 + progress（0..1）。"""
    limit = max(1, min(int(limit or 20), 100))
    return {"jobs": document_ingest.list_jobs(conn, limit=limit)}


@router.get("/api/ingest/jobs/{job_id}")
def ingest_job(job_id: int, conn: sqlite3.Connection = Conn, user=RequiredUser):
    job = document_ingest.get_job(conn, job_id)
    if job is None:
        raise HTTPException(404, "没有这个摄入任务")
    return job


@router.post("/api/ingest/jobs/{job_id}/cancel")
def cancel_ingest_job(job_id: int, conn: sqlite3.Connection = Conn,
                      user=RequiredUser):
    """取消任务：pending 直接 cancelled；running 置取消标记，worker 在块间生效。

    已经结束的任务再取消不算错（幂等）。
    """
    _require_job_owner(conn, job_id, user)
    result = document_ingest.cancel_job(conn, job_id)
    if result is None:
        raise HTTPException(404, "没有这个摄入任务")
    return result


def _require_job_owner(conn, job_id: int, user):
    job = document_ingest.get_job(conn, job_id)
    if job is None:
        raise HTTPException(404, "没有这个摄入任务")
    if not access.is_admin(user) and (not access.can_build_wiki(user) or job["actor_id"] != user["id"]):
        raise HTTPException(403, "只有任务发起人或管理员可以操作摄入任务")
    return job


@router.post("/api/ingest/jobs/{job_id}/retry")
def retry_ingest_job(job_id: int, conn: sqlite3.Connection = Conn, user=RequiredWikiBuilder):
    _require_job_owner(conn, job_id, user)
    return document_ingest.retry_job(conn, job_id)


@router.get("/api/ingest/jobs/{job_id}/candidates")
def ingest_candidates(job_id: int, offset: int = 0, limit: int = 50,
                      conn: sqlite3.Connection = Conn, user=RequiredWikiBuilder):
    _require_job_owner(conn, job_id, user)
    import json
    rows = conn.execute("SELECT * FROM ingest_candidates WHERE job_id=? ORDER BY id LIMIT ? OFFSET ?",
                        (job_id, max(1, min(limit, 100)), max(0, offset))).fetchall()
    return {"candidates": [{**dict(row), "candidate": json.loads(row["candidate_json"])} for row in rows]}
