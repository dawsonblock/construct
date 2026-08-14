-- 009_external_entity_mappings.sql — verified mappings between external
-- systems (ERP) and local entities.
--
-- Item 6: the ERP supplier -> local company resolution must be independent of
-- the invoice's own vendor resolution, and a verified mapping is one of the
-- resolution priorities. This table records that a human (or a verified import)
-- asserted that ERP supplier SUP-0042 is local company <uuid>. It is *not* an
-- automatic merge: a fuzzy match never writes here, and nothing here is ever
-- written by inference. See construction_ai/erp/supplier_resolution.py.

CREATE TABLE external_entity_mappings (
  organization_id        uuid NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
  mapping_id             uuid NOT NULL DEFAULT gen_random_uuid(),
  source_system          text NOT NULL,           -- e.g. 'ERPNext'
  external_entity_type   text NOT NULL,           -- e.g. 'Supplier'
  external_entity_id     text NOT NULL,           -- e.g. 'SUP-0042'
  local_entity_type      text NOT NULL,           -- e.g. 'company'
  local_entity_id        uuid NOT NULL,
  verified_by            uuid,                    -- FK to users when set by a human
  verified_at            timestamptz,
  annotations            jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organization_id, mapping_id),
  -- One local entity per (system, external type, external id) per tenant.
  UNIQUE (organization_id, source_system, external_entity_type, external_entity_id),
  FOREIGN KEY (organization_id, verified_by) REFERENCES users(organization_id, user_id) ON UPDATE CASCADE ON DELETE SET NULL
);
CREATE INDEX external_entity_mappings_local_idx
  ON external_entity_mappings(organization_id, local_entity_type, local_entity_id);

DO $$
BEGIN
  EXECUTE 'ALTER TABLE external_entity_mappings ENABLE ROW LEVEL SECURITY';
  EXECUTE 'ALTER TABLE external_entity_mappings FORCE ROW LEVEL SECURITY';
  EXECUTE format($p$
    CREATE POLICY external_entity_mappings_tenant_isolation ON external_entity_mappings
      USING (organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid)
      WITH CHECK (organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid)
  $p$);
END $$;

GRANT SELECT, INSERT, UPDATE, DELETE ON external_entity_mappings TO construction_app;
