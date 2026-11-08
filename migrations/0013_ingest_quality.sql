-- Processing progress and knowledge quality are separate; proposals survive worker crashes.
ALTER TABLE ingest_jobs ADD COLUMN pages_updated INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ingest_jobs ADD COLUMN chunks_failed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ingest_jobs ADD COLUMN quality_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE ingest_jobs ADD COLUMN report_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE ingest_jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0;
CREATE TABLE ingest_candidates (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES ingest_jobs(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    candidate_json TEXT NOT NULL,
    result_path TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_ingest_candidates_job ON ingest_candidates(job_id, status);
