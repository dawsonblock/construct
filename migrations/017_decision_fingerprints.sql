-- 017_decision_fingerprints.sql — decision fingerprints and replay (items 35, 36).
--
-- The decisions table exists since 003 but has no writer. This migration adds:
-- 1. A decision_fingerprint column — SHA-256 over the evidence IDs, action,
--    rationale, and the project state fingerprint at decision time. This lets
--    a verifier confirm that a decision was made from the same evidence and
--    state that reconstruction would produce today.
-- 2. A state_fingerprint column — the ProjectState.fingerprint() at decision
--    time, so replay can verify that the same state produces the same decision.
-- 3. An index for looking up decisions by fingerprint (replay verification).
--
-- Replay (item 35): reconstruct the project from current state, compute the
-- fingerprint, and compare against stored decision fingerprints. If they
-- match, the decision is still valid. If they differ, the state has changed
-- since the decision was made, and the decision may need to be revisited.

ALTER TABLE decisions ADD COLUMN IF NOT EXISTS decision_fingerprint text;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS state_fingerprint text;

CREATE INDEX IF NOT EXISTS decisions_fingerprint_idx
  ON decisions(organization_id, decision_fingerprint)
  WHERE decision_fingerprint IS NOT NULL;

CREATE INDEX IF NOT EXISTS decisions_state_fingerprint_idx
  ON decisions(organization_id, state_fingerprint)
  WHERE state_fingerprint IS NOT NULL;
