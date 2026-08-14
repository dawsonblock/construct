-- 013_outbox.sql — transactional outbox for reliable job dispatch (item 22).
--
-- The outbox pattern: instead of writing a job row AND pushing to Redis in two
-- separate operations (which can fail independently), the enqueuer writes the
-- job row and an outbox row in the same database transaction. A relay reads
-- unpublished outbox rows and pushes them to Redis, marking them published.
--
-- If the DB transaction rolls back, neither the job nor the outbox row exists,
-- so the relay never pushes anything — no phantom jobs. If the relay crashes
-- after pushing to Redis but before marking published, it may push twice on
-- recovery, but the worker's claim (queued → running) is idempotent, so a
-- double-push is safe.

CREATE TABLE outbox_events (
  organization_id uuid NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
  outbox_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  job_id          uuid NOT NULL,          -- references jobs(job_id) within (organization_id, job_id)
  event_type      text NOT NULL DEFAULT 'job_enqueue',
  payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),
  published_at    timestamptz             -- NULL = unpublished, set by the relay after Redis push
);

-- The relay scans for unpublished rows in creation order. This index makes
-- that scan fast without sorting.
CREATE INDEX outbox_unpublished_idx ON outbox_events(created_at) WHERE published_at IS NULL;

-- Foreign key to jobs (deferred because jobs and outbox are written together).
ALTER TABLE outbox_events
  ADD CONSTRAINT outbox_events_job_fk
  FOREIGN KEY (organization_id, job_id) REFERENCES jobs(organization_id, job_id)
  ON DELETE CASCADE;

-- No RLS on outbox_events: the relay must read across all organizations to
-- push unpublished rows to Redis. This mirrors organization_api_keys and
-- schema_migrations, which are also readable unscoped because they serve
-- infrastructure functions that run before a tenant scope exists. The
-- outbox carries only job IDs and timestamps — no tenant-sensitive data.
-- Tenant isolation is enforced at the job row level (RLS on jobs) and at
-- the worker level (the scope comes from the job row, never the payload).

GRANT SELECT, INSERT, UPDATE ON outbox_events TO construction_app;
-- No DELETE or TRUNCATE for the app role — outbox rows are retained as
-- evidence of dispatch. The relay only UPDATEs published_at.
