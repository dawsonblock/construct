-- 028_rc7_payload_hash_and_reconciliation_provenance.sql — rc7 runtime hardening.
--
-- Phase 22: Persist request_payload_hash on external_actions so recovery can
-- verify the original ERP request was not corrupted before comparing remote
-- state. This gives the recovery system a stronger immutable intent record.
--
-- Phase 15: Add external_action_reconciliation_attempts table for forensic
-- visibility into how UNKNOWN actions became CONFIRMED or PROVEN_ABSENT.
-- Each reconciliation attempt records its strategy, query key, result count,
-- observed docstatus, observed hash, and classification.

-- Phase 22: request_payload_hash for immutable intent verification.
ALTER TABLE external_actions
  ADD COLUMN IF NOT EXISTS request_payload_hash text;

CREATE INDEX IF NOT EXISTS external_actions_payload_hash_idx
  ON external_actions(organization_id, request_payload_hash)
  WHERE request_payload_hash IS NOT NULL;

-- Phase 15: Reconciliation provenance table.
CREATE TABLE IF NOT EXISTS external_action_reconciliation_attempts (
  attempt_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(organization_id),
  action_id uuid NOT NULL,
  attempt_number int NOT NULL,
  strategy text NOT NULL,
  remote_document_id text,
  query_key text,
  result_count int NOT NULL DEFAULT 0,
  observed_docstatus int,
  observed_hash text,
  classification text NOT NULL,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  FOREIGN KEY (organization_id, action_id) REFERENCES external_actions(organization_id, action_id)
);

CREATE INDEX IF NOT EXISTS reconciliation_attempts_action_idx
  ON external_action_reconciliation_attempts(organization_id, action_id, attempt_number);

GRANT SELECT, INSERT ON external_action_reconciliation_attempts TO construction_app;
