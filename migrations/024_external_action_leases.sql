-- 024_external_action_leases.sql — real execution leases and remote-state tracking.
--
-- rc4 Phase 1: Add lease columns so EXECUTING actions are owned by a specific
-- worker with a heartbeat and expiry. A reaper can automatically move expired
-- EXECUTING actions to UNKNOWN.
--
-- rc4 Phase 3: Add remote_state to distinguish:
--   NO_REMOTE_EFFECT  — no ERP document created yet
--   REMOTE_DRAFT      — ERP draft exists (docstatus=0)
--   REMOTE_SUBMITTED  — ERP document submitted (docstatus=1)
--   REMOTE_MISMATCH   — ERP document exists but fields don't match
--   REMOTE_UNKNOWN    — remote state cannot be determined
--
-- rc4 Phase 5: Add idempotency_key_erp for the ERP-visible idempotency field.
--
-- rc4 Phase 16: Add final_audit_event_id and finalized_at for audit repair.

-- Phase 1: Lease columns.
ALTER TABLE external_actions ADD COLUMN IF NOT EXISTS execution_owner text;
ALTER TABLE external_actions ADD COLUMN IF NOT EXISTS lease_acquired_at timestamptz;
ALTER TABLE external_actions ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz;
ALTER TABLE external_actions ADD COLUMN IF NOT EXISTS heartbeat_at timestamptz;
ALTER TABLE external_actions ADD COLUMN IF NOT EXISTS recovery_attempts int NOT NULL DEFAULT 0;

-- Phase 3: Remote state column.
ALTER TABLE external_actions ADD COLUMN IF NOT EXISTS remote_state text
  CHECK (remote_state IN (
    'no_remote_effect',
    'remote_draft',
    'remote_submitted',
    'remote_mismatch',
    'remote_unknown'
  ));
-- Default existing rows: confirmed → remote_submitted, everything else → no_remote_effect.
UPDATE external_actions SET remote_state = 'remote_submitted' WHERE status = 'confirmed' AND remote_state IS NULL;
UPDATE external_actions SET remote_state = 'no_remote_effect' WHERE remote_state IS NULL;
ALTER TABLE external_actions ALTER COLUMN remote_state SET NOT NULL;

-- Phase 5: ERP-visible idempotency key (stored separately from the internal key).
ALTER TABLE external_actions ADD COLUMN IF NOT EXISTS erp_idempotency_key text;

-- Phase 8: Readback hash (SHA-256 of canonical readback representation).
ALTER TABLE external_actions ADD COLUMN IF NOT EXISTS readback_hash text;

-- Phase 16: Final audit completion tracking.
ALTER TABLE external_actions ADD COLUMN IF NOT EXISTS final_audit_event_id uuid;
ALTER TABLE external_actions ADD COLUMN IF NOT EXISTS finalized_at timestamptz;

-- Index for the reaper: find EXECUTING actions with expired leases.
CREATE INDEX IF NOT EXISTS external_actions_lease_expired_idx
  ON external_actions(organization_id, status, lease_expires_at)
  WHERE status = 'executing';

-- Index for audit repair: find CONFIRMED actions missing final audit.
CREATE INDEX IF NOT EXISTS external_actions_missing_audit_idx
  ON external_actions(organization_id, status, final_audit_event_id)
  WHERE status = 'confirmed' AND final_audit_event_id IS NULL;

-- Index for recovery daemon: find actions needing recovery.
CREATE INDEX IF NOT EXISTS external_actions_recovery_idx
  ON external_actions(organization_id, status, remote_state)
  WHERE status IN ('unknown', 'executing') OR (status = 'confirmed' AND final_audit_event_id IS NULL);

-- Grant UPDATE on new columns to the app role.
GRANT UPDATE ON external_actions TO construction_app;
