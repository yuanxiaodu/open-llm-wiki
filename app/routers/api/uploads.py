"""上传件：暂存（两段式）与材料列表。

S11 起是两段式：`POST /api/uploads` 只**暂存**（落盘 + 插行，不解析、不建消息、
不触发智能体），前端在输入框里显示胶囊；用户点发送时由 POST /api/chat/{sid}/messages
带 upload_ids 一次性入库、解析并触发一轮。`DELETE` 撤掉还没发送的暂存件。

**不提供原件下载**（2026-10-30 裁定）：原件只作为解析/引注的来源留在服务器上，
人类面能读的是解析后的正文与 wiki 页；没有 `/api/uploads/{id}/download` 这个入口，
材料列表也只给元数据。语料全员共享的口径不变（spec §5），变的只是"能不能把原件
抠下来"——不做。

找不到行、或盘上的原件已经没了，列表里照旧没有它；接口一律用 /api 的
{"error": 人话} 信封（信封由 app/main.py 的 handler 统一出）。
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, File, HTTPException, UploadFile

from ...config import settings
from ...deps import Conn, RequiredUser, RequiredWikiBuilder
from ...services import uploads as uploads_service

router = APIRouter()

# 列表一次最多给多少份材料：不是资料库页面，100 份足够挑；更早的等以后加分页。
LIST_MAX = 100


def _chars(parsed_text_path: str | None) -> int:
    """解析正文的字符数（前端显示"约 N 字"）。读不到（文件被删/没有）给 0。"""
    if not parsed_text_path:
        return 0
    try:
        return len((settings.data_dir / parsed_text_path).read_text(
            encoding="utf-8", errors="replace"
        ))
    except OSError:
        return 0


@router.get("/api/uploads")
def list_uploads(conn: sqlite3.Connection = Conn, user=RequiredUser):
    """材料列表（新到旧，最多 100）：给智能体/内部任务挑要摄入的材料。

    每项：id / orig_filename / parse_status / source_kind / source_url / chars / created_at。
    chars 是解析正文的字符数（读不到给 0）；source_kind 是 'file' 或 'web'（0007）。
    只给元数据，**不给原件下载地址**（2026-10-30 裁定）。
    """
    rows = conn.execute(
        "SELECT id, orig_filename, parse_status, source_kind, source_url,"
        " parsed_text_path, created_at FROM upload_files ORDER BY id DESC LIMIT ?",
        (LIST_MAX,),
    ).fetchall()
    return {
        "uploads": [
            {
                "id": row["id"],
                "orig_filename": row["orig_filename"],
                "parse_status": row["parse_status"],
                "source_kind": row["source_kind"] or "file",
                "source_url": row["source_url"],
                "chars": _chars(row["parsed_text_path"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    }


@router.post("/api/uploads")
def create_upload(
    conn: sqlite3.Connection = Conn,
    user=RequiredWikiBuilder,
    file: UploadFile = File(...),
):
    """**暂存**一份附件：只落盘+插行（parse_status='none'），不解析、不建消息、不起轮。

    返回 {upload_id, orig_filename, size, mime, parse_status:"none"}；前端拿 upload_id
    显示胶囊，点发送时把它放进 POST /api/chat/{sid}/messages 的 upload_ids。

    2026-10-30：只有「可建设 wiki」的成员能摄入材料；只有使用权限的成员在第一步就被
    RequiredWikiBuilder 挡成 403，不会先落盘再等发送时才拒。
    """
    try:
        return uploads_service.stage_upload(conn, user["id"], file)
    except uploads_service.FileTooLargeError:
        raise HTTPException(413, "文件超过 50 MB")


@router.delete("/api/uploads/{upload_id}")
def delete_upload(upload_id: int, conn: sqlite3.Connection = Conn, user=RequiredUser):
    """撤掉一份没发送的暂存附件（只能撤自己的、未挂消息的）；已发送 400。"""
    ok, note = uploads_service.discard_staged_upload(conn, upload_id, user["id"])
    if not ok:
        status = 404 if note == "没有这份材料" else 400
        raise HTTPException(status, note)
    return {"ok": True, "upload_id": upload_id}
