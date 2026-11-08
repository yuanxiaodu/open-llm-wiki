-- 众筹共写版 schema（2026-10-20 改版重写；旧采集版迁移链作废，库从未上机）

CREATE TABLE users (
    id            INTEGER PRIMARY KEY,
    invite_id     INTEGER REFERENCES invites(id),
    name          TEXT    NOT NULL,
    phone         TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    role          TEXT    NOT NULL CHECK (role IN ('admin', 'member')),
    created_at    TEXT    NOT NULL,
    last_login_at TEXT,
    deactivated_at TEXT
);

CREATE TABLE invites (
    id              INTEGER PRIMARY KEY,
    token           TEXT    NOT NULL UNIQUE,
    name            TEXT    NOT NULL,
    purpose         TEXT    NOT NULL DEFAULT 'register'
                        CHECK (purpose IN ('register', 'reset')),
    created_by      INTEGER REFERENCES users(id),
    used_count      INTEGER NOT NULL DEFAULT 0,
    revoked_at      TEXT,
    first_opened_at TEXT,
    first_used_at   TEXT,
    created_at      TEXT    NOT NULL
);

CREATE TABLE chat_sessions (
    id              INTEGER PRIMARY KEY,
    room_type       TEXT    NOT NULL CHECK (room_type IN ('private', 'public')),
    user_id         INTEGER REFERENCES users(id),
    title           TEXT,
    status          TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'closed')),
    started_at      TEXT    NOT NULL,
    last_message_at TEXT
);
CREATE UNIQUE INDEX idx_sessions_private_user ON chat_sessions(user_id)
    WHERE room_type = 'private';
CREATE UNIQUE INDEX idx_sessions_public_singleton ON chat_sessions(room_type)
    WHERE room_type = 'public';

CREATE TABLE messages (
    id         INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES chat_sessions(id),
    seq        INTEGER NOT NULL,
    role       TEXT    NOT NULL CHECK (role IN ('agent', 'human')),
    kind       TEXT    NOT NULL DEFAULT 'reply' CHECK (kind IN ('reply', 'broadcast')),
    user_id    INTEGER REFERENCES users(id),
    content    TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL,
    truncated  INTEGER NOT NULL DEFAULT 0,
    usage_json TEXT
);
CREATE UNIQUE INDEX idx_messages_session_seq ON messages(session_id, seq);

CREATE TABLE upload_files (
    id               INTEGER PRIMARY KEY,
    message_id       INTEGER REFERENCES messages(id),
    user_id          INTEGER NOT NULL REFERENCES users(id),
    orig_filename    TEXT    NOT NULL,
    stored_path      TEXT    NOT NULL,
    size             INTEGER NOT NULL,
    mime             TEXT,
    sha256           TEXT    NOT NULL,
    parse_status     TEXT    NOT NULL DEFAULT 'none'
                        CHECK (parse_status IN ('none', 'done', 'failed')),
    parsed_text_path TEXT,
    created_at       TEXT    NOT NULL
);

CREATE TABLE wiki_pages (
    id                 INTEGER PRIMARY KEY,
    slug               TEXT    NOT NULL UNIQUE,
    title              TEXT    NOT NULL,
    page_type          TEXT    NOT NULL
                           CHECK (page_type IN ('topic', 'concept', 'comparison', 'outline')),
    current_version_id INTEGER,
    created_at         TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL
);

CREATE TABLE wiki_versions (
    id                INTEGER PRIMARY KEY,
    page_id           INTEGER NOT NULL REFERENCES wiki_pages(id),
    version_no        INTEGER NOT NULL,
    body_md           TEXT    NOT NULL,
    author_user_id    INTEGER REFERENCES users(id),
    generated_by      TEXT    NOT NULL CHECK (generated_by IN ('human', 'agent')),
    status            TEXT    NOT NULL DEFAULT 'live'
                          CHECK (status IN ('pending', 'live', 'rejected')),
    confirmed_by      INTEGER REFERENCES users(id),
    confirmed_at      TEXT,
    source_message_ids TEXT   NOT NULL DEFAULT '[]',
    verify_summary    TEXT    NOT NULL DEFAULT '{}',
    created_at        TEXT    NOT NULL,
    note              TEXT
);
CREATE UNIQUE INDEX idx_versions_page_no ON wiki_versions(page_id, version_no);

CREATE TABLE citations (
    id           INTEGER PRIMARY KEY,
    version_id   INTEGER NOT NULL REFERENCES wiki_versions(id),
    cite_key     TEXT    NOT NULL,
    message_id   INTEGER NOT NULL REFERENCES messages(id),
    char_start   INTEGER NOT NULL,
    char_end     INTEGER NOT NULL,
    quote_text   TEXT    NOT NULL,
    quote_sha256 TEXT    NOT NULL,
    verified     INTEGER NOT NULL DEFAULT 0,
    verify_note  TEXT
);
CREATE INDEX idx_citations_version ON citations(version_id, cite_key);

CREATE TABLE changelog (
    id            INTEGER PRIMARY KEY,
    kind          TEXT    NOT NULL CHECK (kind IN ('wiki', 'outline', 'system')),
    page_id       INTEGER REFERENCES wiki_pages(id),
    version_id    INTEGER REFERENCES wiki_versions(id),
    actor_user_id INTEGER REFERENCES users(id),
    action        TEXT    NOT NULL
                      CHECK (action IN ('create', 'edit', 'propose', 'confirm',
                                        'reject', 'revert', 'rename')),
    summary       TEXT    NOT NULL,
    created_at    TEXT    NOT NULL
);
CREATE INDEX idx_changelog_time ON changelog(created_at DESC, id DESC);
