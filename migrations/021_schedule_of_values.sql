-- 021_schedule_of_values.sql — schedule of values, scope-bound work confirmation,
-- and quantitative progress billing (Phases 15, 16, 17).
--
-- v0.4.2 treated work confirmation as a project-level boolean: "electrical work
-- confirmed" could satisfy a "roofing invoice." That is too broad for invoice
-- payment. This migration introduces the scope hierarchy:
--
--     Project → Contract → ScheduleOfValuesItem / Scope
--                            ├── WorkConfirmation
--                            └── InvoiceAllocation (invoice line → SOV item)
--
-- Verification can now ask ConfirmedWork(scope) for the same scope being billed,
-- and progress billing can reason about earned value per scope instead of one
-- undifferentiated project-completion boolean.
--
-- All money is numeric(16,2) — Decimal at the boundary, never float.

CREATE TABLE IF NOT EXISTS contracts (
  organization_id     uuid NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
  contract_id         uuid NOT NULL DEFAULT gen_random_uuid(),
  project_id          uuid NOT NULL,
  company_id          uuid NOT NULL,           -- the supplier / subcontractor
  reference           text NOT NULL,           -- e.g. "CON-001"
  name                text NOT NULL DEFAULT '',
  base_contract_value numeric(16,2) NOT NULL DEFAULT 0,
  currency            text NOT NULL DEFAULT 'CAD',
  status              text NOT NULL DEFAULT 'active'
                        CHECK (status IN ('draft','active','closed','cancelled')),
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organization_id, contract_id),
  UNIQUE (organization_id, reference),
  FOREIGN KEY (organization_id, project_id) REFERENCES projects(organization_id, project_id) ON UPDATE CASCADE ON DELETE CASCADE,
  FOREIGN KEY (organization_id, company_id) REFERENCES companies(organization_id, company_id) ON UPDATE CASCADE ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS contracts_project_idx ON contracts(organization_id, project_id, status);

CREATE TABLE IF NOT EXISTS sov_items (
  organization_id     uuid NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
  sov_item_id         uuid NOT NULL DEFAULT gen_random_uuid(),
  contract_id         uuid NOT NULL,
  reference           text NOT NULL,           -- cost code, e.g. "SOV-001"
  name                text NOT NULL,           -- "Electrical rough-in"
  base_value          numeric(16,2) NOT NULL DEFAULT 0,
  currency            text NOT NULL DEFAULT 'CAD',
  sort_order          int NOT NULL DEFAULT 0,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organization_id, sov_item_id),
  UNIQUE (organization_id, contract_id, reference),
  FOREIGN KEY (organization_id, contract_id) REFERENCES contracts(organization_id, contract_id) ON UPDATE CASCADE ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS sov_items_contract_idx ON sov_items(organization_id, contract_id, sort_order);

CREATE TABLE IF NOT EXISTS change_orders (
  organization_id     uuid NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
  change_order_id     uuid NOT NULL DEFAULT gen_random_uuid(),
  contract_id         uuid NOT NULL,
  reference           text NOT NULL,
  name                text NOT NULL DEFAULT '',
  amount              numeric(16,2) NOT NULL,  -- signed: +increase / -decrease
  currency            text NOT NULL DEFAULT 'CAD',
  status              text NOT NULL DEFAULT 'approved'
                        CHECK (status IN ('pending','approved','rejected')),
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organization_id, change_order_id),
  UNIQUE (organization_id, reference),
  FOREIGN KEY (organization_id, contract_id) REFERENCES contracts(organization_id, contract_id) ON UPDATE CASCADE ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS change_orders_contract_idx
  ON change_orders(organization_id, contract_id, status);

-- invoice_allocations: binds an invoice (or invoice line) to a SOV item.
-- This is how the system knows which scope an invoice bills.
CREATE TABLE IF NOT EXISTS invoice_allocations (
  organization_id     uuid NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
  allocation_id       uuid NOT NULL DEFAULT gen_random_uuid(),
  invoice_id          uuid NOT NULL,
  sov_item_id         uuid NOT NULL,
  amount              numeric(16,2) NOT NULL,  -- amount of this invoice allocated to this SOV item
  currency            text NOT NULL DEFAULT 'CAD',
  created_at          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organization_id, allocation_id),
  UNIQUE (organization_id, invoice_id, sov_item_id),
  FOREIGN KEY (organization_id, invoice_id) REFERENCES invoices(organization_id, invoice_id) ON UPDATE CASCADE ON DELETE CASCADE,
  FOREIGN KEY (organization_id, sov_item_id) REFERENCES sov_items(organization_id, sov_item_id) ON UPDATE CASCADE ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS invoice_allocations_invoice_idx
  ON invoice_allocations(organization_id, invoice_id);
CREATE INDEX IF NOT EXISTS invoice_allocations_sov_idx
  ON invoice_allocations(organization_id, sov_item_id);

-- Phase 15: bind work_confirmations to a specific SOV item (scope), not just a
-- project. scope_id (added in 010) remains as a loose cost-code escape hatch;
-- sov_item_id is the authoritative scope reference with an FK.
ALTER TABLE work_confirmations ADD COLUMN IF NOT EXISTS sov_item_id uuid;
ALTER TABLE work_confirmations DROP CONSTRAINT IF EXISTS work_confirmations_sov_item_fk;
ALTER TABLE work_confirmations ADD CONSTRAINT work_confirmations_sov_item_fk
  FOREIGN KEY (organization_id, sov_item_id) REFERENCES sov_items(organization_id, sov_item_id)
  ON UPDATE CASCADE ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS work_confirmations_sov_item_idx
  ON work_confirmations(organization_id, sov_item_id, status);

-- Row-level security + grants, mirroring the existing tenant-isolation pattern.
ALTER TABLE contracts ENABLE ROW LEVEL SECURITY;
ALTER TABLE contracts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS contracts_tenant_isolation ON contracts;
CREATE POLICY contracts_tenant_isolation ON contracts
  USING (organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid)
  WITH CHECK (organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON contracts TO construction_app;

ALTER TABLE sov_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE sov_items FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sov_items_tenant_isolation ON sov_items;
CREATE POLICY sov_items_tenant_isolation ON sov_items
  USING (organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid)
  WITH CHECK (organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON sov_items TO construction_app;

ALTER TABLE change_orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE change_orders FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS change_orders_tenant_isolation ON change_orders;
CREATE POLICY change_orders_tenant_isolation ON change_orders
  USING (organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid)
  WITH CHECK (organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON change_orders TO construction_app;

ALTER TABLE invoice_allocations ENABLE ROW LEVEL SECURITY;
ALTER TABLE invoice_allocations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS invoice_allocations_tenant_isolation ON invoice_allocations;
CREATE POLICY invoice_allocations_tenant_isolation ON invoice_allocations
  USING (organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid)
  WITH CHECK (organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON invoice_allocations TO construction_app;

-- updated_at triggers.
DROP TRIGGER IF EXISTS contracts_touch ON contracts;
CREATE TRIGGER contracts_touch BEFORE UPDATE ON contracts
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
DROP TRIGGER IF EXISTS sov_items_touch ON sov_items;
CREATE TRIGGER sov_items_touch BEFORE UPDATE ON sov_items
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
DROP TRIGGER IF EXISTS change_orders_touch ON change_orders;
CREATE TRIGGER change_orders_touch BEFORE UPDATE ON change_orders
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
