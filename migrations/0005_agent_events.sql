-- 0005_agent_events.sql：智能体一轮跑里的工具事件（裁定 R-S2-9）。
--
-- 这张表是 SSE 尾读的数据面：GET /api/chat/{id}/stream 按 (session_id, seq) 拼帧，
-- Last-Event-ID 就从 seq 之后续读，所以 seq 必须在一场会话内连续且不撞。
--
-- 建表放在 Task 3（hooks 落不了库就测不了）；表结构照计划 Task 4 那一段逐字抄，
-- Task 4 不要再建一遍。
--
-- UNIQUE(session_id, seq) 自带一条 (session_id, seq) 的索引，尾读那句
-- `WHERE session_id = ? AND seq > ? ORDER BY seq` 正好走它，不再另建索引。
-- seq 由写它的人用单条 INSERT ... SELECT COALESCE(MAX(seq),0)+1 原子分配
-- （与 messages 的 seq 同一个手法，裁定 R68），不靠应用层的计数器。

CREATE TABLE agent_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES chat_sessions(id),
    seq INTEGER NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('tool-call','tool-result','system','error')),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (session_id, seq)
);
