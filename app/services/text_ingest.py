"""Archive actual human messages as sources; never accept generated source text."""
from __future__ import annotations

from io import BytesIO
from fastapi import UploadFile

from . import uploads


def ingest_message(conn, session_id: int, actor_id: int, actor: str,
                   message_id: int | None = None, owner_id: int | None = None) -> dict:
    sql = "SELECT m.* FROM messages m JOIN chat_sessions s ON s.id=m.session_id WHERE m.role='human'"
    params = []
    if message_id is None:
        sql += " AND m.session_id=?"
        params.append(session_id)
    else:
        sql += " AND m.id=?"
        params.append(message_id)
    if owner_id is not None:
        sql += " AND s.user_id=?"
        params.append(owner_id)
    row = conn.execute(sql + " ORDER BY m.seq DESC LIMIT 1", params).fetchone()
    if row is None or not row["content"].strip():
        raise ValueError("没有可访问的人类原文消息")
    previous = conn.execute("SELECT id,orig_filename FROM upload_files WHERE message_id=? AND source_kind='message' ORDER BY id LIMIT 1", (row["id"],)).fetchone()
    if previous:
        archived = uploads.ingest_raw(conn, previous["id"], previous["orig_filename"], actor)
        return {"upload_id": previous["id"], "raw_path": archived, "message_id": row["id"], "unchanged": True}
    filename = f"message-{row['id']}.txt"
    saved = uploads.save_upload(conn, row["id"], actor_id,
        UploadFile(filename=filename, file=BytesIO(row["content"].encode("utf-8"))))
    conn.execute("UPDATE upload_files SET source_kind='message' WHERE id=?", (saved["upload_id"],))
    archived = uploads.ingest_raw(conn, saved["upload_id"], filename, actor)
    if archived is None:
        raise ValueError("消息原文已保存，但 raw 归档失败，请重试")
    conn.commit()
    return {"upload_id": saved["upload_id"], "raw_path": archived, "message_id": row["id"]}
