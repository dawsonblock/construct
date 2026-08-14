-- 019_external_action_state_machine.sql — rebuild external-action idempotency (Phase 1).
--
-- The previous design was unsafe:
--   check ledger → call ERP → submit invoice → record completed external action
--
-- The crash window between ERP success and the local ledger write meant a retry
-- could not distinguish "never submitted" from "submitted but not recorded."
--
-- The new design uses a state machine:
--   PENDING → EXECUTING → CONFIRMED
--                       → UNKNOWN → (reconcile) → CONFIRMED or FAILED_RETRYABLE
--                       → FAILED_RETRYABLE
--                       → FAILED_TERMINAL
--
-- The row is reserved BEFORE the external call. The invariant becomes:
--   ExternalEffectAttempt ⇒ ExternalActionReservationExists
--
-- This migration:
-- 1. Adds state-machine columns to external_actions.
-- 2. Replaces the status CHECK with the new state machine values.
-- 3. Grants UPDATE to construction_app (DELETE remains denied).
-- 4. Adds an index for state-based queries.

-- Add new columns.
ALTER TABLE external_actions ADD COLUMN IF NOT EXISTS operation text;
ALTER TABLE external_actions ADD COLUMN IF NOT EXISTS subject_type text;
ALTER TABLE external_actions ADD COLUMN IF NOT EXISTS subject_id uuid;
ALTER TABLE external_actions ADD COLUMN IF NOT EXISTS remote_system text DEFAULT 'erpnext';
ALTER TABLE external_actions ADD COLUMN IF NOT EXISTS remote_document_id text;
ALTER TABLE external_actions ADD COLUMN IF NOT EXISTS attempt_count int NOT NULL DEFAULT 0;
ALTER TABLE external_actions ADD COLUMN IF NOT EXISTS reserved_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE external_actions ADD COLUMN IF NOT EXISTS last_attempt_at timestamptz;
ALTER TABLE external_actions ADD COLUMN IF NOT EXISTS confirmed_at timestamptz;
ALTER TABLE external_actions ADD COLUMN IF NOT EXISTS last_error text;

-- Backfill operation from action_type for existing rows.
UPDATE external_actions SET operation = action_type WHERE operation IS NULL;

-- Make operation NOT NULL after backfill.
ALTER TABLE external_actions ALTER COLUMN operation SET NOT NULL;

-- Replace the status CHECK constraint.
ALTER TABLE external_actions DROP CONSTRAINT IF EXISTS external_actions_status_check;
ALTER TABLE external_actions ADD CONSTRAINT external_actions_status_check
  CHECK (status IN (
    'pending',           -- row reserved, no external call yet
    'executing',         -- external call in progress
    'unknown',           -- external call returned ambiguous result (timeout, 502, crash)
    'confirmed',         -- external effect verified by readback
    'failed_retryable',  -- external call failed, safe to retry
    'failed_terminal',   -- external call failed, not retryable
    'completed'          -- legacy status, mapped to 'confirmed' semantically
  ));

-- Grant UPDATE to the app role for state transitions.
-- DELETE remains denied — external actions are permanent records.
GRANT UPDATE ON external_actions TO construction_app;

-- Index for finding actions by state (e.g. all UNKNOWN actions needing reconciliation).
CREATE INDEX IF NOT EXISTS external_actions_status_idx
  ON external_actions(organization_id, status);

-- Index for finding actions by subject.
CREATE INDEX IF NOT EXISTS external_actions_subject_idx
  ON external_actions(organization_id, subject_type, subject_id)
  WHERE subject_id IS NOT NULL;
