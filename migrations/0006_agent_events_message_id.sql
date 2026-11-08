-- 0006_agent_events_message_id.sql：给 agent_events 加 message_id（裁定 R-S2-20）。
--
-- 为什么：SSE 尾读原来靠"created_at >= 那条 agent 行的 created_at"划一轮的边界，
-- 而 created_at 是秒级（db.utcnow）——上一轮与这一轮挤在同一秒里时，上一轮的事件尾巴
-- 会被再发一遍。加一列 message_id（这条事件属于哪条 agent 消息）之后，尾读按
-- `message_id = 当前 agent 行的 id` 过滤，边界不再靠时间猜。
--
-- 可空是有意的：迁移之前落的老行没有归属可补（NULL），尾读对 NULL 行退回原来的
-- created_at 口径（legacy 兼容，见 chat._pending_events 的注释）。
--
-- 用 ADD COLUMN 而不是重建表：SQLite 的 ADD COLUMN 原样保留 UNIQUE(session_id, seq)
-- 与 type 的 CHECK，可空、无默认值的列也不需要回填——这是最省事且不动老数据的做法。
--
-- 索引建在 (message_id) 上：尾读那句仍走 UNIQUE(session_id, seq) 自带的索引，
-- 这条索引是给 S3 准备的（按消息取事件、渲染历史里的工具卡片）。

ALTER TABLE agent_events ADD COLUMN message_id INTEGER;

CREATE INDEX idx_agent_events_message_id ON agent_events (message_id);
