-- 025_recovery_and_accounting_hardening.sql — unified recovery payload persistence and explicit change order allocations.
--
-- rc5 Phase 1: Add request_payload jsonb to external_actions so the original approved
-- financial intent is persisted immutably with the reservation, allowing recovery
-- reconciliation to perform the exact same canonical readback comparison as the
-- normal execution path.
--
-- rc5 Phase 2: Add change_order_allocations table to bind change orders explicitly to
-- specific Schedule of Values (SOV) items rather than spreading change order value
-- proportionally across all base SOV items.

-- Phase 1: Request payload on external_actions.
ALTER TABLE external_actions ADD COLUMN IF NOT EXISTS request_payload jsonb;

-- Phase 2: Explicit change order allocations to specific SOV items.
CREATE TABLE IF NOT EXISTS change_order_allocations (
  organization_id     uuid NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
  allocation_id       uuid NOT NULL DEFAULT gen_random_uuid(),
  change_order_id     uuid NOT NULL,
  sov_item_id         uuid NOT NULL,
  amount              numeric(16,2) NOT NULL,
  currency            text NOT NULL DEFAULT 'CAD',
  created_at          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organization_id, allocation_id),
  UNIQUE (organization_id, change_order_id, sov_item_id),
  FOREIGN KEY (organization_id, change_order_id) REFERENCES change_orders(organization_id, change_order_id) ON UPDATE CASCADE ON DELETE CASCADE,
  FOREIGN KEY (organization_id, sov_item_id) REFERENCES sov_items(organization_id, sov_item_id) ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS change_order_allocations_co_idx
  ON change_order_allocations(organization_id, change_order_id);
CREATE INDEX IF NOT EXISTS change_order_allocations_sov_idx
  ON change_order_allocations(organization_id, sov_item_id);

-- Row-level security + grants.
ALTER TABLE change_order_allocations ENABLE ROW LEVEL SECURITY;
ALTER TABLE change_order_allocations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS change_order_allocations_tenant_isolation ON change_order_allocations;
CREATE POLICY change_order_allocations_tenant_isolation ON change_order_allocations
  USING (organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid)
  WITH CHECK (organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON change_order_allocations TO construction_app;
GRANT UPDATE ON external_actions TO construction_app;
