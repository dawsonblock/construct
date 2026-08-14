-- 010_work_confirmations.sql — work completion is evidence, not a request field.
--
-- Item 12: v0.3 let a caller declare `work_confirmed: true` in the job payload,
-- so a client could assert that physical work was complete. A client may request
-- "evaluate this invoice" but cannot declare the work done. Work completion is
-- now an authoritative record written by a human (superintendent, inspector) or
-- a verified external source (ERP goods receipt, completed work order), and the
-- verifier reads these records.

CREATE TABLE work_confirmations (
  organization_id     uuid NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
  confirmation_id     uuid NOT NULL DEFAULT gen_random_uuid(),
  project_id          uuid,
  scope_id            uuid,                     -- optional: a cost code / work scope
  invoice_id          uuid,                     -- optional: tied to a specific invoice
  confirmed_by_user_id uuid,                    -- the human who confirmed (FK to users)
  confirmation_type   text NOT NULL
                        CHECK (confirmation_type IN ('superintendent','daily_field_report','signed_inspection',
                                                      'delivery_receipt','progress_certification','erp_goods_receipt',
                                                      'completed_work_order','manual')),
  status              text NOT NULL DEFAULT 'confirmed'
                        CHECK (status IN ('confirmed','retracted')),
  percent_complete    double precision CHECK (percent_complete IS NULL OR (percent_complete >= 0 AND percent_complete <= 100)),
  quantity            numeric(14,4),
  occurred_at         timestamptz NOT NULL DEFAULT now(),
  evidence_ids        uuid[] NOT NULL DEFAULT '{}',
  annotations         jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  created_by          text NOT NULL DEFAULT 'system',
  PRIMARY KEY (organization_id, confirmation_id),
  FOREIGN KEY (organization_id, project_id) REFERENCES projects(organization_id, project_id) ON UPDATE CASCADE ON DELETE CASCADE,
  FOREIGN KEY (organization_id, confirmed_by_user_id) REFERENCES users(organization_id, user_id) ON UPDATE CASCADE ON DELETE SET NULL
);
CREATE INDEX work_confirmations_project_idx ON work_confirmations(organization_id, project_id, status);
CREATE INDEX work_confirmations_invoice_idx ON work_confirmations(organization_id, invoice_id, status);
CREATE TRIGGER work_confirmations_touch BEFORE UPDATE ON work_confirmations
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

DO $$
BEGIN
  EXECUTE 'ALTER TABLE work_confirmations ENABLE ROW LEVEL SECURITY';
  EXECUTE 'ALTER TABLE work_confirmations FORCE ROW LEVEL SECURITY';
  EXECUTE format($p$
    CREATE POLICY work_confirmations_tenant_isolation ON work_confirmations
      USING (organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid)
      WITH CHECK (organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid)
  $p$);
END $$;

GRANT SELECT, INSERT, UPDATE, DELETE ON work_confirmations TO construction_app;
