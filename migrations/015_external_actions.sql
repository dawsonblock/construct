-- 015_external_actions.sql — external action ledger (item 25).
--
-- Every external side-effect (ERP writes, notifications, etc.) is recorded
-- here with an idempotency key. Before performing an external action, the
-- system checks if an action with the same (organization, scope, key) already
-- exists. If it does, the existing result is returned instead of performing
-- the action again — preventing duplicate external effects on retry.
--
-- This is the enforcement layer for the invariant:
--   RepeatedExecution ⇒ NoDuplicateFinancialEffect

CREATE TABLE external_actions (
  organization_id   uuid NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
  action_id         uuid NOT NULL DEFAULT gen_random_uuid(),
  action_type       text NOT NULL,             -- e.g. 'erp_invoice_submit'
  target_system     text NOT NULL DEFAULT 'erpnext',
  target_id         text,                      -- external system's entity id
  idempotency_key   text NOT NULL,             -- dedup key within (org, action_type)
  request_hash      text,                      -- hash of the request payload for audit
  result            jsonb,                     -- the result returned by the external system
  status            text NOT NULL DEFAULT 'completed'
                     CHECK (status IN ('pending','completed','failed')),
  created_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organization_id, action_id),
  UNIQUE (organization_id, action_type, idempotency_key)
);

CREATE INDEX external_actions_org_type_idx ON external_actions(organization_id, action_type);

ALTER TABLE external_actions ENABLE ROW LEVEL SECURITY;
ALTER TABLE external_actions FORCE ROW LEVEL SECURITY;
CREATE POLICY external_actions_tenant_isolation ON external_actions
  USING (organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid)
  WITH CHECK (organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid);

GRANT SELECT, INSERT ON external_actions TO construction_app;
-- Append-only: no UPDATE, DELETE, TRUNCATE for the app role.
GRANT SELECT ON external_actions TO construct_audit_reader;
