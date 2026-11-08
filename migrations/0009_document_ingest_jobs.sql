-- 0009_document_ingest_jobs.sql：整本书后台摄入的任务表（S6c）。
--
-- 一次「整本摄入」= 一份已解析的材料（upload_files，parse_status='done'）切成若干块，
-- 后台 worker 逐块调模型抽 wiki 页、逐字校验引注后写进 vault。这张表只是任务状态：
-- 页面本身仍然只存在于 data/wiki（文件 + git），不在这里冗余正文。
--
-- 为什么状态用 CHECK 白名单而不是自由文本：worker、路由、前端三处都按这几个值分支，
-- 多一个没约定的值（例如 'error'）就会让前端渲染不出人话；白名单让写错直接失败。
--   pending    排队中（同进程一次只跑一个 job，其余在这里等）
--   running    worker 正在跑
--   done       跑完（可能有跳过的块/页，见 error 里的提示）
--   failed     未处理异常终止（error 是中文短句）
--   interrupted 进程重启时还在 running 的（启动时被 mark_interrupted 改掉）
--   cancelled  用户取消（pending 直接取消；running 在块间边界生效）
--
-- cursor 是已处理到的字符偏移（块可能重叠，它是"这一块结束的位置"，不是字节数）；
-- chunks_done/chunks_total 是前端进度条唯一的数据源（progress 由 API 现算）。
-- actor_id 可空：老任务/脚本没有登录用户时仍然能记录是谁发起的（NULL = 系统/脚本）。

CREATE TABLE ingest_jobs (
  id            INTEGER PRIMARY KEY,
  upload_id     INTEGER NOT NULL REFERENCES upload_files(id),
  actor_id      INTEGER,
  title         TEXT    NOT NULL DEFAULT '',
  status        TEXT    NOT NULL CHECK(status IN ('pending','running','done','failed','interrupted','cancelled')),
  total_chars   INTEGER NOT NULL DEFAULT 0,
  chunk_size    INTEGER NOT NULL DEFAULT 8000,
  cursor        INTEGER NOT NULL DEFAULT 0,
  chunks_total  INTEGER NOT NULL DEFAULT 0,
  chunks_done   INTEGER NOT NULL DEFAULT 0,
  pages_created INTEGER NOT NULL DEFAULT 0,
  pages_skipped INTEGER NOT NULL DEFAULT 0,
  error         TEXT    NOT NULL DEFAULT '',
  created_at    TEXT    NOT NULL,
  updated_at    TEXT    NOT NULL,
  finished_at   TEXT
);

CREATE INDEX idx_ingest_jobs_status ON ingest_jobs(status, id);
