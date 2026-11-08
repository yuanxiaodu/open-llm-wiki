-- 0012_drop_citations.sql：句子级引注层下线（2026-10-30 最终裁定）。
--
-- 页面契约只剩 Hermes/LLM-wiki 约定：frontmatter + 正文 + [[wikilink]] + sources。
-- 历史引注已归档到 data/backups/citations-*.json；这里把按 (page_slug, commit_sha)
-- 绑定的核对记录表删掉。旧代码若还在，会读不到表——先把代码一起升上来。

DROP TABLE IF EXISTS citation_checks;
