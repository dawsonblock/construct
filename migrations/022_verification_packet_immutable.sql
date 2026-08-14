-- 022_verification_packet_immutable.sql — immutable versioned verification
-- packets (Phase 19).
--
-- Once an approval decision references a verification packet, that packet must
-- not mutate. Any new verification produces a new packet. The approval decision
-- points at an exact, versioned packet instead of mutable current state.
--
-- This migration:
-- 1. Adds version, canonical_hash, verifier_version, policy_inputs columns.
-- 2. Makes approval_packets append-only: REVOKE UPDATE and DELETE from the app
--    role. The touch trigger is dropped (rows never update).
-- 3. Adds an index for looking up the latest packet for an approval by
--    created_at (the new ordering, since updated_at no longer changes).

ALTER TABLE approval_packets ADD COLUMN IF NOT EXISTS version int NOT NULL DEFAULT 1;
ALTER TABLE approval_packets ADD COLUMN IF NOT EXISTS canonical_hash text;
ALTER TABLE approval_packets ADD COLUMN IF NOT EXISTS verifier_version text;
ALTER TABLE approval_packets ADD COLUMN IF NOT EXISTS policy_inputs jsonb NOT NULL DEFAULT '{}'::jsonb;

-- Append-only: the app role may INSERT and SELECT but not UPDATE or DELETE.
-- (Cascade deletes from approvals are performed by the owner/foreign-key
-- machinery, not by the app role.)
REVOKE UPDATE, DELETE ON approval_packets FROM construction_app;

-- The touch trigger is meaningless once rows are immutable.
DROP TRIGGER IF EXISTS approval_packets_touch ON approval_packets;

CREATE INDEX IF NOT EXISTS approval_packets_approval_created_idx
  ON approval_packets(organization_id, approval_id, created_at DESC);
