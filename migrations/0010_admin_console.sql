-- 0010_admin_console.sql：管理员后台（站点设置）+ 成员「可建设 wiki」权限。
--
-- 权限只有一档（2026-10-30 委托人裁定）：能建设 wiki 的成员可以摄入材料、建/改/删页；
-- 只有使用权限的成员只能查询、搜索、阅读（查询时智能体新建 query/comparison/summary 沉淀页不算修改，
-- 见 app/agent/tools.py 的运行时限制）。管理员始终拥有建设权限，不受这一列约束。
--
-- 迁移是纯增量：老库所有用户自动得到 1（保持现状），旧代码忽略新列/新表，可安全回滚代码。
-- chat_visibility 默认 all：与迁移前的"全员可见可发言"完全一致，管理员要收紧时再改。

ALTER TABLE users ADD COLUMN can_build_wiki INTEGER NOT NULL DEFAULT 1
    CHECK (can_build_wiki IN (0, 1));

CREATE TABLE site_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by INTEGER REFERENCES users(id)
);

INSERT INTO site_settings(key, value, updated_at, updated_by)
VALUES (
    'chat_visibility',
    'all',
    strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'),
    NULL
);
