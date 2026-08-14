-- 014_job_leases.sql — worker leases and retry metadata (items 23, 24).
--
-- A worker lease prevents a crashed worker from permanently stalling a job.
-- When a worker claims a job, it sets lease_expires_at = now() + lease_duration.
-- If the worker crashes, the lease expires and another worker can reclaim the
-- job (running → queued) and retry it.
--
-- attempt_count and max_attempts support retry-with-dead-letter (item 24):
-- a job that fails is retried up to max_attempts times, then moved to
-- 'dead_letter' status. The dead_letter status is a terminal state — the job
-- is not retried again without human intervention.

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS claimed_by text;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS attempt_count int NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS max_attempts int NOT NULL DEFAULT 3;

-- Add 'dead_letter' to the allowed statuses.
ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_status_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_status_check
  CHECK (status IN ('queued','running','completed','failed','dead_letter'));

-- Index for finding expired leases efficiently.
CREATE INDEX IF NOT EXISTS jobs_lease_expired_idx
  ON jobs(organization_id, lease_expires_at)
  WHERE status = 'running' AND lease_expires_at IS NOT NULL;

-- Function to find expired-lease jobs across all tenants. Runs as SECURITY
-- DEFINER (the migration owner) to bypass RLS — the worker needs to find
-- expired jobs regardless of which tenant they belong to. Returns
-- (organization_id, job_id) pairs for the reclaim logic.
CREATE OR REPLACE FUNCTION find_expired_leases() RETURNS TABLE(organization_id uuid, job_id uuid)
LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
  SELECT organization_id, job_id FROM jobs
  WHERE status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at < now()
$$;
