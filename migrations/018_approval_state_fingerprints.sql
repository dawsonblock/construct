-- 018_approval_state_fingerprints.sql — stale-approval checking (item 48).
--
-- Before executing an approved invoice, the executor reconstructs the current
-- project state and compares its fingerprint to the fingerprint at approval
-- time. If the state has drifted, the approval is stale and execution is
-- refused — the human approved a different state than the one we'd execute
-- against.
--
-- This migration adds a state_fingerprint column to approvals, recording the
-- ProjectState.fingerprint() at the moment the approval was decided. The
-- approval service sets this when decide_approval() is called.

ALTER TABLE approvals ADD COLUMN IF NOT EXISTS state_fingerprint text;

CREATE INDEX IF NOT EXISTS approvals_state_fingerprint_idx
  ON approvals(organization_id, state_fingerprint)
  WHERE state_fingerprint IS NOT NULL;
