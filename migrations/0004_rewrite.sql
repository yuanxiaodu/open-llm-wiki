-- 0004_rewrite.sql：wiki 事实源移到 data/wiki 文件 + git；会话改多会话全员可见；去点头流。
--
-- 这三条是为了能在"已经灌了数据的旧库"上跑通（测试库是空的，跑不出这些错）：
-- 1) 全程关掉外键检查。runner（app/db.py connect()）这条连接是 foreign_keys=ON 的，
--    而 SQLite 的 DROP TABLE 在开着外键时会先做一次隐式 DELETE FROM，父表底下还有子行
--    就直接 FOREIGN KEY constraint failed。upload_files 要留着却指着 messages，
--    光靠调 DROP 顺序治不了它，必须关外键。
-- 2) DROP 一律子表在前、父表在后：citations / changelog → wiki_versions → wiki_pages，
--    messages → chat_sessions。
-- 3) 旧的公共房间那行 chat_sessions.user_id IS NULL，新表要求 NOT NULL。公共房间这个概念
--    随 S1 删掉了，所以这些行连同它底下的消息一起不搬——是语义上的删除，不是数据丢失。
--    （只按 kind='reply' 过滤不够：公共房间里也有人话，那些行的 session_id 会变成孤儿。）
PRAGMA foreign_keys=OFF;

CREATE TABLE chat_sessions_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
    started_at TEXT NOT NULL,
    last_message_at TEXT
);
INSERT INTO chat_sessions_new (id, user_id, title, status, started_at, last_message_at)
SELECT id, user_id, COALESCE(title, ''), 'active', started_at, last_message_at
FROM chat_sessions WHERE user_id IS NOT NULL;

CREATE TABLE messages_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES chat_sessions(id),
    seq INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('agent','human')),
    user_id INTEGER REFERENCES users(id),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    truncated INTEGER NOT NULL DEFAULT 0,
    usage_json TEXT,
    UNIQUE (session_id, seq)
);
INSERT INTO messages_new (id, session_id, seq, role, user_id, content, created_at, truncated, usage_json)
SELECT id, session_id, seq, role, user_id, content, created_at, truncated, usage_json
FROM messages
WHERE kind = 'reply'
  AND session_id IN (SELECT id FROM chat_sessions WHERE user_id IS NOT NULL);

ALTER TABLE upload_files ADD COLUMN parse_report TEXT;

CREATE TABLE citation_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    page_slug TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    cite_key TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('message','upload')),
    source_id INTEGER NOT NULL,
    quote TEXT NOT NULL,
    verified INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX idx_checks_page ON citation_checks (page_slug, commit_sha);

CREATE VIRTUAL TABLE fts_shared_new USING fts5(
    kind UNINDEXED, ref_id UNINDEXED, piece UNINDEXED, heading, body, tokenize='trigram'
);

-- 先删旧表（子表在前），再把新表改名顶上。
DROP TABLE citations;
DROP TABLE changelog;
DROP TABLE wiki_versions;
DROP TABLE wiki_pages;
DROP TABLE fts_shared;
DROP TABLE messages;
DROP TABLE chat_sessions;

ALTER TABLE chat_sessions_new RENAME TO chat_sessions;
ALTER TABLE messages_new RENAME TO messages;
ALTER TABLE fts_shared_new RENAME TO fts_shared;

PRAGMA foreign_keys=ON;
