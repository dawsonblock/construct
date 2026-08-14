-- 012_audit_checkpoints.sql — external audit checkpointing (item 20).
--
-- A checkpoint captures the state of an organization's audit chain at a point
-- in time: the last sequence number, the last entry_hash, the event count, and
-- a checkpoint hash binding them together. Checkpoints are stored in the
-- database (append-only) AND can be exported externally for independent
-- verification. If someone with owner-level DB access rewrites the chain, the
-- next checkpoint comparison will detect the discrepancy.

CREATE TABLE audit_checkpoints (
  organization_id   uuid NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
  checkpoint_id     uuid NOT NULL DEFAULT gen_random_uuid(),
  sequence          bigint NOT NULL,          -- last audit_events.sequence at checkpoint time
  entry_hash        text NOT NULL,            -- last audit_events.entry_hash at checkpoint time
  event_count       bigint NOT NULL,          -- total events in the chain at checkpoint time
  checkpoint_hash   text NOT NULL,            -- H(org_id ‖ sequence ‖ entry_hash ‖ count ‖ timestamp)
  exported_by       text NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organization_id, checkpoint_id),
  UNIQUE (organization_id, checkpoint_hash)
);

CREATE INDEX audit_checkpoints_org_seq_idx ON audit_checkpoints(organization_id, sequence);

ALTER TABLE audit_checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_checkpoints FORCE ROW LEVEL SECURITY;
CREATE POLICY audit_checkpoints_tenant_isolation ON audit_checkpoints
  USING (organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid)
  WITH CHECK (organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid);

GRANT SELECT, INSERT ON audit_checkpoints TO construction_app;
-- Append-only: no UPDATE, DELETE, TRUNCATE for the app role.

-- Read-only for the audit reader role.
GRANT SELECT ON audit_checkpoints TO construct_audit_reader;
