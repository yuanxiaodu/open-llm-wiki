-- 0011_wiki_sync.sql：本机 Hermes → 线上 wiki 的同步状态（2026-10-30）。
--
-- 模型（委托人 2026-10-30 裁定）：本地库更权威、本地优先、手动发布。
-- 本地 vault 的 git 分支只当 outbox（`refs/heads/hermes/<机器>`）；线上同步器按
-- `last_local_sha..新本地 HEAD` 的 diff 逐页重放，页面一律过 store 的写路径
-- （frontmatter/引注/派生文件/乐观锁都在线上完成）。线上被本地覆盖或删掉的版本
-- 不做通知、不做人工冲突页——git 历史与版本页就是记录（裁定：只留版本历史可查）。
--
-- 状态只在"整批全部成功"后推进；失败页留在 runs.detail_json 里，下一次发布重试。
-- 迁移是纯增量：旧代码忽略新表，可安全回滚代码。

CREATE TABLE IF NOT EXISTS wiki_sync_state (
    branch         TEXT PRIMARY KEY,
    last_local_sha TEXT,
    last_main_sha  TEXT,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wiki_sync_runs (
    id          INTEGER PRIMARY KEY,
    branch      TEXT NOT NULL,
    local_sha   TEXT,
    main_sha    TEXT,
    ok          INTEGER NOT NULL CHECK (ok IN (0, 1)),
    applied     INTEGER NOT NULL DEFAULT 0,
    ignored     INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sync_runs_branch ON wiki_sync_runs (branch, id);
