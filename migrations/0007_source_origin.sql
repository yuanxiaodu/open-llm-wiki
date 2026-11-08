-- 0007_source_origin.sql：给 upload_files 加来源溯源两列（S5b 多来源摄入）。
--
-- source_kind：这份材料从哪来。'file' = 用户上传的文件（旧行与新上传的默认值），
--              'web' = 由网页链接抓取摄入（POST /api/ingest/url、agent 的 ingest_url）。
-- source_url ：网页来源的最终地址（跟完跳转之后的 URL）；文件来源为 NULL。
--
-- 为什么用 ADD COLUMN 带默认值：SQLite 的 ADD COLUMN + NOT NULL DEFAULT 会给已有行
-- 直接补上 'file'（不重建表、不动老数据、不需要回填脚本）；新插入即使不写这两列
-- 也照样成立（默认值兜底），只有 web 来源显式写。source_url 可空：文件没有 URL。
--
-- 不建索引：目前没有"按来源类型/URL 查材料"的路径——加载与列表都按 upload_files.id。

ALTER TABLE upload_files ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'file';

ALTER TABLE upload_files ADD COLUMN source_url TEXT;
