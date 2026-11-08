-- R4 补：上传材料可以被引注（llm-wiki 的"摄入"）。
-- 引注要么指向某人说的原话（message_id），要么指向上传材料的解析原文（upload_id）。
-- 旧表 message_id 是 NOT NULL，SQLite 改约束只能重建。

CREATE TABLE citations_new (
    id           INTEGER PRIMARY KEY,
    version_id   INTEGER NOT NULL REFERENCES wiki_versions(id),
    cite_key     TEXT    NOT NULL,
    message_id   INTEGER REFERENCES messages(id),
    upload_id    INTEGER REFERENCES upload_files(id),
    char_start   INTEGER NOT NULL,
    char_end     INTEGER NOT NULL,
    quote_text   TEXT    NOT NULL,
    quote_sha256 TEXT    NOT NULL,
    verified     INTEGER NOT NULL DEFAULT 0,
    verify_note  TEXT,
    CHECK (message_id IS NOT NULL OR upload_id IS NOT NULL)
);

INSERT INTO citations_new
    (id, version_id, cite_key, message_id, upload_id, char_start, char_end,
     quote_text, quote_sha256, verified, verify_note)
SELECT id, version_id, cite_key, message_id, NULL, char_start, char_end,
       quote_text, quote_sha256, verified, verify_note
FROM citations;

DROP TABLE citations;
ALTER TABLE citations_new RENAME TO citations;
CREATE INDEX idx_citations_version ON citations(version_id, cite_key);
