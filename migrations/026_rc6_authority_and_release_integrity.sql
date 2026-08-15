-- 026_rc6_authority_and_release_integrity.sql — rc6 release-blocking remediations.
--
-- rc6 closes the remaining authority/recovery/release-integrity gaps identified
-- in the rc5 audit:
--
-- Phase 1: Persist the exact approval policy snapshot/hash on approvals and
--          approval_decisions so the executor can recompute the decision
--          fingerprint under the SAME policy that was active at decision time,
--          not today's DEFAULT_POLICY. Without this, a decision made under a
--          non-default policy appears stale immediately during execution.
--
-- Phase 2: Add explicit work-confirmation supersession semantics. The previous
--          schema only allowed 'confirmed' / 'retracted'; rc6 introduces
--          'superseded' and a supersedes_confirmation_id FK so a higher-authority
--          certification can explicitly retire an earlier confirmation, and
--          active selection can distinguish "still controlling" from
--          "replaced by a newer authoritative record".
--
-- Phase 3: Add first_negative_observation_at to external_actions so bounded
--          negative confirmation is both attempt-bounded AND time-bounded.
--          Two queries microseconds apart must not declare PROVEN_ABSENT when
--          ERP read-after-write visibility lags by seconds.

-- Phase 1: Approval policy binding.
ALTER TABLE approvals
  ADD COLUMN IF NOT EXISTS policy_hash text,
  ADD COLUMN IF NOT EXISTS policy_snapshot jsonb;

ALTER TABLE approval_decisions
  ADD COLUMN IF NOT EXISTS policy_hash text,
  ADD COLUMN IF NOT EXISTS policy_snapshot jsonb;

CREATE INDEX IF NOT EXISTS approvals_policy_hash_idx
  ON approvals(organization_id, policy_hash)
  WHERE policy_hash IS NOT NULL;

-- Phase 2: Work-confirmation supersession semantics.
ALTER TABLE work_confirmations
  DROP CONSTRAINT IF EXISTS work_confirmations_status_check;
ALTER TABLE work_confirmations
  ADD CONSTRAINT work_confirmations_status_check
  CHECK (status IN ('confirmed','retracted','superseded','revoked'));

ALTER TABLE work_confirmations
  ADD COLUMN IF NOT EXISTS supersedes_confirmation_id uuid;

ALTER TABLE work_confirmations
  ADD CONSTRAINT work_confirmations_supersedes_fk
  FOREIGN KEY (organization_id, supersedes_confirmation_id)
  REFERENCES work_confirmations(organization_id, confirmation_id)
  ON UPDATE CASCADE ON DELETE RESTRICT;

CREATE INDEX IF NOT EXISTS work_confirmations_supersedes_idx
  ON work_confirmations(organization_id, supersedes_confirmation_id)
  WHERE supersedes_confirmation_id IS NOT NULL;

-- Phase 3: Time-bounded negative confirmation.
ALTER TABLE external_actions
  ADD COLUMN IF NOT EXISTS first_negative_observation_at timestamptz;

-- rc6: approvals, work_confirmations, and external_actions need UPDATE for
-- transitions. approval_decisions UPDATE is revoked in migration 027 to
-- preserve the append-only invariant after the rc6 policy_snapshot column
-- was added.
GRANT UPDATE ON approvals, approval_decisions, work_confirmations, external_actions TO construction_app;
