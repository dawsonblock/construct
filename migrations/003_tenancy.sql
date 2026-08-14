-- 003_tenancy.sql — the scoped relational core.
--
-- v0.3.0 stored control-plane state in tables whose owner was a string inside a
-- JSON payload (ai_objects) or an unscoped text column (evidence, ai_audit_log).
-- Tenant identity now lives in relational keys. See docs/TENANCY.md.
--
-- This migration REPLACES the 001/002 shapes rather than backfilling them.
-- There is no production deployment of this system; carrying a compatibility
-- layer for state that does not exist would cost more than it protects.

-- ---------------------------------------------------------------------------
-- Superseded shapes
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS entity_edges;          -- -> relationships
DROP TABLE IF EXISTS external_entity_map;   -- -> entities
DROP TABLE IF EXISTS evidence;              -- -> evidence (scoped, version-pinned)
DROP TABLE IF EXISTS approval_packets;      -- -> approval_packets (scoped)
DROP TABLE IF EXISTS ai_approvals;          -- -> approvals
DROP TABLE IF EXISTS ai_audit_log;          -- -> audit_events (per-org chain)
DROP TABLE IF EXISTS ai_idempotency;        -- -> idempotency_keys (scoped)
DROP TABLE IF EXISTS communications;        -- -> communications (scoped)
DROP TABLE IF EXISTS documents;             -- -> documents + document_versions
DROP TABLE IF EXISTS ai_objects;            -- no successor: untyped state is the bug

-- ---------------------------------------------------------------------------
-- Shared plumbing
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------------
-- Organizations and authentication
-- ---------------------------------------------------------------------------
CREATE TABLE organizations (
  organization_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug            text NOT NULL UNIQUE,
  name            text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER organizations_touch BEFORE UPDATE ON organizations
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- Deliberately NOT under RLS: this is the table consulted to discover which
-- organization a caller belongs to, before any scope exists. See TENANCY.md §6.
CREATE TABLE organization_api_keys (
  organization_id uuid NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
  key_id          uuid NOT NULL DEFAULT gen_random_uuid(),
  key_hash        text NOT NULL UNIQUE,
  label           text NOT NULL DEFAULT '',
  created_at      timestamptz NOT NULL DEFAULT now(),
  revoked_at      timestamptz,
  PRIMARY KEY (organization_id, key_id)
);

-- ---------------------------------------------------------------------------
-- Directory: people and companies
-- ---------------------------------------------------------------------------
CREATE TABLE people (
  organization_id uuid NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
  person_id       uuid NOT NULL DEFAULT gen_random_uuid(),
  reference       text NOT NULL,
  name            text NOT NULL,
  emails          text[] NOT NULL DEFAULT '{}',
  phones          text[] NOT NULL DEFAULT '{}',
  company_id      uuid,
  role            text,
  annotations     jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  created_by      text NOT NULL DEFAULT 'system',
  PRIMARY KEY (organization_id, person_id),
  UNIQUE (organization_id, reference)
);
CREATE TRIGGER people_touch BEFORE UPDATE ON people FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE TABLE companies (
  organization_id uuid NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
  company_id      uuid NOT NULL DEFAULT gen_random_uuid(),
  reference       text NOT NULL,
  name            text NOT NULL,
  company_type    text NOT NULL DEFAULT 'vendor'
                    CHECK (company_type IN ('vendor','supplier','subcontractor','client','other')),
  aliases         text[] NOT NULL DEFAULT '{}',
  erp_supplier_id text,
  tax_id          text,
  annotations     jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  created_by      text NOT NULL DEFAULT 'system',
  PRIMARY KEY (organization_id, company_id),
  UNIQUE (organization_id, reference)
);
CREATE TRIGGER companies_touch BEFORE UPDATE ON companies FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE INDEX companies_org_erp_idx ON companies(organization_id, erp_supplier_id);

ALTER TABLE people ADD CONSTRAINT people_company_fk
  FOREIGN KEY (organization_id, company_id) REFERENCES companies(organization_id, company_id) ON UPDATE CASCADE ON DELETE SET NULL;

-- ---------------------------------------------------------------------------
-- Projects
-- ---------------------------------------------------------------------------
CREATE TABLE projects (
  organization_id           uuid NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
  project_id                uuid NOT NULL DEFAULT gen_random_uuid(),
  reference                 text NOT NULL,
  name                      text NOT NULL,
  address                   text,
  status                    text NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active','on_hold','closed','archived')),
  client_company_id         uuid,
  project_manager_person_id uuid,
  identifiers               jsonb NOT NULL DEFAULT '{}'::jsonb,
  annotations               jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at                timestamptz NOT NULL DEFAULT now(),
  updated_at                timestamptz NOT NULL DEFAULT now(),
  created_by                text NOT NULL DEFAULT 'system',
  PRIMARY KEY (organization_id, project_id),
  UNIQUE (organization_id, reference),
  FOREIGN KEY (organization_id, client_company_id) REFERENCES companies(organization_id, company_id) ON UPDATE CASCADE ON DELETE SET NULL,
  FOREIGN KEY (organization_id, project_manager_person_id) REFERENCES people(organization_id, person_id) ON UPDATE CASCADE ON DELETE SET NULL
);
CREATE TRIGGER projects_touch BEFORE UPDATE ON projects FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE INDEX projects_org_status_idx ON projects(organization_id, status);

CREATE TABLE project_memberships (
  organization_id uuid NOT NULL,
  project_id      uuid NOT NULL,
  membership_id   uuid NOT NULL DEFAULT gen_random_uuid(),
  person_id       uuid,
  company_id      uuid,
  role            text NOT NULL DEFAULT 'participant',
  active_from     date,
  active_until    date,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  created_by      text NOT NULL DEFAULT 'system',
  PRIMARY KEY (organization_id, membership_id),
  FOREIGN KEY (organization_id, project_id) REFERENCES projects(organization_id, project_id) ON UPDATE CASCADE ON DELETE CASCADE,
  FOREIGN KEY (organization_id, person_id) REFERENCES people(organization_id, person_id) ON UPDATE CASCADE ON DELETE CASCADE,
  FOREIGN KEY (organization_id, company_id) REFERENCES companies(organization_id, company_id) ON UPDATE CASCADE ON DELETE CASCADE,
  CHECK (person_id IS NOT NULL OR company_id IS NOT NULL)
);
CREATE TRIGGER project_memberships_touch BEFORE UPDATE ON project_memberships FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE INDEX project_memberships_project_idx ON project_memberships(organization_id, project_id);

-- ---------------------------------------------------------------------------
-- Communications and documents
--
-- project_id is nullable throughout: an unresolved project is a real state, not
-- an error to be defaulted away.
-- ---------------------------------------------------------------------------
CREATE TABLE communications (
  organization_id  uuid NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
  communication_id uuid NOT NULL DEFAULT gen_random_uuid(),
  project_id       uuid,
  source           text NOT NULL CHECK (source IN ('gmail','microsoft365','manual','import')),
  external_id      text NOT NULL,
  thread_id        text,
  sender           text NOT NULL,
  recipients       text[] NOT NULL DEFAULT '{}',
  subject          text NOT NULL DEFAULT '',
  body             text NOT NULL DEFAULT '',
  content_hash     text NOT NULL,
  received_at      timestamptz NOT NULL,
  observed_at      timestamptz NOT NULL DEFAULT now(),
  annotations      jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  created_by       text NOT NULL DEFAULT 'system',
  PRIMARY KEY (organization_id, communication_id),
  UNIQUE (organization_id, project_id, communication_id),
  -- One canonical communication per source message, per tenant.
  UNIQUE (organization_id, source, external_id),
  UNIQUE (organization_id, source, content_hash),
  FOREIGN KEY (organization_id, project_id) REFERENCES projects(organization_id, project_id) ON UPDATE CASCADE ON DELETE SET NULL
);
CREATE TRIGGER communications_touch BEFORE UPDATE ON communications FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE INDEX communications_thread_idx ON communications(organization_id, thread_id);

CREATE TABLE documents (
  organization_id  uuid NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
  document_id      uuid NOT NULL DEFAULT gen_random_uuid(),
  project_id       uuid,
  filename         text NOT NULL,
  document_type    text NOT NULL DEFAULT 'unknown',
  document_family  text,
  current_version  integer NOT NULL DEFAULT 0,
  source_id        uuid,
  annotations      jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  created_by       text NOT NULL DEFAULT 'system',
  PRIMARY KEY (organization_id, document_id),
  UNIQUE (organization_id, project_id, document_id),
  FOREIGN KEY (organization_id, project_id) REFERENCES projects(organization_id, project_id) ON UPDATE CASCADE ON DELETE SET NULL,
  FOREIGN KEY (organization_id, source_id) REFERENCES communications(organization_id, communication_id) ON UPDATE CASCADE ON DELETE SET NULL
);
CREATE TRIGGER documents_touch BEFORE UPDATE ON documents FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE INDEX documents_org_type_idx ON documents(organization_id, document_type);
CREATE INDEX documents_family_idx ON documents(organization_id, document_family);

-- The bytes are immutable and content-addressed; the row records where they
-- live and what was extracted from them. Evidence points here, never at the
-- mutable document row.
CREATE TABLE document_versions (
  organization_id     uuid NOT NULL,
  document_id         uuid NOT NULL,
  document_version_id uuid NOT NULL DEFAULT gen_random_uuid(),
  project_id          uuid,
  version_number      integer NOT NULL,
  revision_label      text,
  content_hash        text NOT NULL,
  byte_size           bigint NOT NULL DEFAULT 0,
  mime_type           text NOT NULL DEFAULT 'application/octet-stream',
  storage_uri         text NOT NULL,
  extracted_text      text NOT NULL DEFAULT '',
  extraction_warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
  tables              jsonb NOT NULL DEFAULT '[]'::jsonb,
  supersedes_id       uuid,
  observed_at         timestamptz NOT NULL DEFAULT now(),
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  created_by          text NOT NULL DEFAULT 'system',
  PRIMARY KEY (organization_id, document_version_id),
  UNIQUE (organization_id, document_id, version_number),
  -- Identical bytes ingested twice are one version, per tenant.
  UNIQUE (organization_id, content_hash),
  FOREIGN KEY (organization_id, document_id) REFERENCES documents(organization_id, document_id) ON UPDATE CASCADE ON DELETE CASCADE,
  FOREIGN KEY (organization_id, project_id, document_id) REFERENCES documents(organization_id, project_id, document_id) ON UPDATE CASCADE ON DELETE CASCADE,
  FOREIGN KEY (organization_id, supersedes_id) REFERENCES document_versions(organization_id, document_version_id) ON UPDATE CASCADE ON DELETE SET NULL
);
CREATE TRIGGER document_versions_touch BEFORE UPDATE ON document_versions FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- ---------------------------------------------------------------------------
-- Evidence
-- ---------------------------------------------------------------------------
CREATE TABLE evidence (
  organization_id     uuid NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
  evidence_id         uuid NOT NULL DEFAULT gen_random_uuid(),
  project_id          uuid,
  field               text NOT NULL,
  value               jsonb NOT NULL,
  confidence          double precision NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  authority           double precision NOT NULL CHECK (authority >= 0 AND authority <= 1),
  source_type         text NOT NULL,
  source_id           text NOT NULL,
  source_version_id   uuid,
  content_hash        text,
  extractor           text NOT NULL DEFAULT 'deterministic',
  observed_at         timestamptz NOT NULL DEFAULT now(),
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  created_by          text NOT NULL DEFAULT 'system',
  PRIMARY KEY (organization_id, evidence_id),
  FOREIGN KEY (organization_id, project_id) REFERENCES projects(organization_id, project_id) ON UPDATE CASCADE ON DELETE SET NULL,
  FOREIGN KEY (organization_id, source_version_id) REFERENCES document_versions(organization_id, document_version_id) ON UPDATE CASCADE ON DELETE SET NULL
);
CREATE TRIGGER evidence_touch BEFORE UPDATE ON evidence FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE INDEX evidence_org_project_field_idx ON evidence(organization_id, project_id, field);
CREATE INDEX evidence_source_idx ON evidence(organization_id, source_type, source_id);

-- ---------------------------------------------------------------------------
-- Commercial records
-- ---------------------------------------------------------------------------
CREATE TABLE purchase_orders (
  organization_id uuid NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
  purchase_order_id uuid NOT NULL DEFAULT gen_random_uuid(),
  project_id      uuid,
  vendor_company_id uuid,
  reference       text NOT NULL,
  amount          numeric(14,2) NOT NULL DEFAULT 0,
  currency        text NOT NULL DEFAULT 'CAD',
  quote_reference text,
  status          text NOT NULL DEFAULT 'open',
  erp_docname     text,
  annotations     jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  created_by      text NOT NULL DEFAULT 'system',
  PRIMARY KEY (organization_id, purchase_order_id),
  UNIQUE (organization_id, reference),
  UNIQUE (organization_id, project_id, purchase_order_id),
  FOREIGN KEY (organization_id, project_id) REFERENCES projects(organization_id, project_id) ON UPDATE CASCADE ON DELETE SET NULL,
  FOREIGN KEY (organization_id, vendor_company_id) REFERENCES companies(organization_id, company_id) ON UPDATE CASCADE ON DELETE SET NULL
);
CREATE TRIGGER purchase_orders_touch BEFORE UPDATE ON purchase_orders FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE TABLE quotes (
  organization_id   uuid NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
  quote_id          uuid NOT NULL DEFAULT gen_random_uuid(),
  project_id        uuid,
  vendor_company_id uuid,
  reference         text NOT NULL,
  amount            numeric(14,2) NOT NULL DEFAULT 0,
  currency          text NOT NULL DEFAULT 'CAD',
  revision          integer NOT NULL DEFAULT 1,
  approved          boolean NOT NULL DEFAULT false,
  erp_docname       text,
  annotations       jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  created_by        text NOT NULL DEFAULT 'system',
  PRIMARY KEY (organization_id, quote_id),
  UNIQUE (organization_id, reference, revision),
  UNIQUE (organization_id, project_id, quote_id),
  FOREIGN KEY (organization_id, project_id) REFERENCES projects(organization_id, project_id) ON UPDATE CASCADE ON DELETE SET NULL,
  FOREIGN KEY (organization_id, vendor_company_id) REFERENCES companies(organization_id, company_id) ON UPDATE CASCADE ON DELETE SET NULL
);
CREATE TRIGGER quotes_touch BEFORE UPDATE ON quotes FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE TABLE invoices (
  organization_id     uuid NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
  invoice_id          uuid NOT NULL DEFAULT gen_random_uuid(),
  project_id          uuid,
  vendor_company_id   uuid,
  reference           text NOT NULL,
  invoice_number      text NOT NULL,
  vendor_name         text NOT NULL DEFAULT '',
  subtotal            numeric(14,2),
  tax                 numeric(14,2),
  total               numeric(14,2) NOT NULL,
  currency            text NOT NULL DEFAULT 'CAD',
  po_reference        text,
  quote_reference     text,
  status              text NOT NULL DEFAULT 'received'
                        CHECK (status IN ('received','prepared','held','rejected','approved','submitted')),
  source_id           uuid,
  source_version_id   uuid,
  observed_at         timestamptz NOT NULL DEFAULT now(),
  annotations         jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  created_by          text NOT NULL DEFAULT 'system',
  PRIMARY KEY (organization_id, invoice_id),
  UNIQUE (organization_id, reference),
  UNIQUE (organization_id, project_id, invoice_id),
  -- Phase 15 duplicate detection starts here: one invoice number per vendor per tenant.
  UNIQUE (organization_id, vendor_company_id, invoice_number),
  FOREIGN KEY (organization_id, project_id) REFERENCES projects(organization_id, project_id) ON UPDATE CASCADE ON DELETE SET NULL,
  FOREIGN KEY (organization_id, vendor_company_id) REFERENCES companies(organization_id, company_id) ON UPDATE CASCADE ON DELETE SET NULL,
  FOREIGN KEY (organization_id, source_version_id) REFERENCES document_versions(organization_id, document_version_id) ON UPDATE CASCADE ON DELETE SET NULL
);
CREATE TRIGGER invoices_touch BEFORE UPDATE ON invoices FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE INDEX invoices_org_status_idx ON invoices(organization_id, status);
CREATE INDEX invoices_org_project_idx ON invoices(organization_id, project_id);

CREATE TABLE invoice_lines (
  organization_id uuid NOT NULL,
  invoice_id      uuid NOT NULL,
  line_id         uuid NOT NULL DEFAULT gen_random_uuid(),
  project_id      uuid,
  line_number     integer NOT NULL,
  description     text NOT NULL DEFAULT '',
  quantity        numeric(14,4),
  unit_amount     numeric(14,2),
  amount          numeric(14,2) NOT NULL DEFAULT 0,
  annotations     jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  created_by      text NOT NULL DEFAULT 'system',
  PRIMARY KEY (organization_id, invoice_id, line_id),
  UNIQUE (organization_id, invoice_id, line_number),
  -- Always enforced, including while the invoice's project is unresolved.
  FOREIGN KEY (organization_id, invoice_id) REFERENCES invoices(organization_id, invoice_id) ON UPDATE CASCADE ON DELETE CASCADE,
  -- Preserves project scope and re-files lines when resolution assigns the invoice.
  FOREIGN KEY (organization_id, project_id, invoice_id) REFERENCES invoices(organization_id, project_id, invoice_id) ON UPDATE CASCADE ON DELETE CASCADE
);
CREATE TRIGGER invoice_lines_touch BEFORE UPDATE ON invoice_lines FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- ---------------------------------------------------------------------------
-- Decisions, approvals, packets
-- ---------------------------------------------------------------------------
CREATE TABLE decisions (
  organization_id uuid NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
  decision_id     uuid NOT NULL DEFAULT gen_random_uuid(),
  project_id      uuid,
  subject_type    text NOT NULL,
  subject_id      uuid,
  action          text NOT NULL,
  rationale       text NOT NULL DEFAULT '',
  evidence_ids    uuid[] NOT NULL DEFAULT '{}',
  confidence      double precision,
  actor           text NOT NULL DEFAULT 'ai',
  annotations     jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  created_by      text NOT NULL DEFAULT 'system',
  PRIMARY KEY (organization_id, decision_id),
  FOREIGN KEY (organization_id, project_id) REFERENCES projects(organization_id, project_id) ON UPDATE CASCADE ON DELETE SET NULL
);
CREATE TRIGGER decisions_touch BEFORE UPDATE ON decisions FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE TABLE approvals (
  organization_id   uuid NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
  approval_id       uuid NOT NULL DEFAULT gen_random_uuid(),
  project_id        uuid,
  reference         text NOT NULL,
  approval_type     text NOT NULL,
  subject_type      text NOT NULL,
  subject_id        uuid NOT NULL,
  recommended_action text NOT NULL,
  amount            numeric(14,2),
  currency          text NOT NULL DEFAULT 'CAD',
  status            text NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','approved','held','rejected')),
  exceptions        jsonb NOT NULL DEFAULT '[]'::jsonb,
  evidence_ids      uuid[] NOT NULL DEFAULT '{}',
  requested_by      text NOT NULL DEFAULT 'ai',
  decided_by        text,
  decided_at        timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  created_by        text NOT NULL DEFAULT 'system',
  PRIMARY KEY (organization_id, approval_id),
  UNIQUE (organization_id, reference),
  UNIQUE (organization_id, project_id, approval_id),
  -- Phase 19 separation of duties: an AI actor can never be the decider.
  CHECK (decided_by IS NULL OR decided_by <> 'ai'),
  CHECK ((status = 'pending') = (decided_by IS NULL)),
  FOREIGN KEY (organization_id, project_id) REFERENCES projects(organization_id, project_id) ON UPDATE CASCADE ON DELETE SET NULL
);
CREATE TRIGGER approvals_touch BEFORE UPDATE ON approvals FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE INDEX approvals_org_status_idx ON approvals(organization_id, status);

CREATE TABLE approval_packets (
  organization_id uuid NOT NULL,
  packet_id       uuid NOT NULL DEFAULT gen_random_uuid(),
  approval_id     uuid NOT NULL,
  project_id      uuid,
  reference       text NOT NULL,
  -- The packet is a rendered view over authoritative rows, not the source of
  -- truth for any number in it. It is jsonb because its shape evolves.
  payload         jsonb NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  created_by      text NOT NULL DEFAULT 'system',
  PRIMARY KEY (organization_id, packet_id),
  UNIQUE (organization_id, reference),
  FOREIGN KEY (organization_id, approval_id) REFERENCES approvals(organization_id, approval_id) ON UPDATE CASCADE ON DELETE CASCADE,
  FOREIGN KEY (organization_id, project_id, approval_id) REFERENCES approvals(organization_id, project_id, approval_id) ON UPDATE CASCADE ON DELETE CASCADE
);
CREATE TRIGGER approval_packets_touch BEFORE UPDATE ON approval_packets FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE INDEX approval_packets_approval_idx ON approval_packets(organization_id, approval_id);

-- ---------------------------------------------------------------------------
-- Graph: entities and typed relationships
--
-- Schema lands here so tenancy and RLS are defined once. Phase 3 writes them.
-- ---------------------------------------------------------------------------
CREATE TABLE entities (
  organization_id uuid NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
  entity_id       uuid NOT NULL DEFAULT gen_random_uuid(),
  project_id      uuid,
  entity_type     text NOT NULL
                    CHECK (entity_type IN ('PROJECT','DOCUMENT','DRAWING','SPECIFICATION','CONTRACT',
                                           'VENDOR','INVOICE','PURCHASE_ORDER','CHANGE_ORDER','CLAIM',
                                           'EVIDENCE','APPROVAL','PERSON','LOCATION','COST_CODE')),
  -- The row in its own table that this node stands for.
  record_table    text,
  record_id       uuid,
  label           text NOT NULL DEFAULT '',
  external_system text,
  external_id     text,
  annotations     jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  created_by      text NOT NULL DEFAULT 'system',
  PRIMARY KEY (organization_id, entity_id),
  UNIQUE (organization_id, entity_type, record_table, record_id),
  UNIQUE (organization_id, external_system, external_id),
  FOREIGN KEY (organization_id, project_id) REFERENCES projects(organization_id, project_id) ON UPDATE CASCADE ON DELETE SET NULL
);
CREATE TRIGGER entities_touch BEFORE UPDATE ON entities FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- A model may only ever write status='proposed'. Promotion to 'approved' is a
-- deterministic rule or a human decision. See TENANCY.md §10.
CREATE TABLE relationships (
  organization_id   uuid NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
  relationship_id   uuid NOT NULL DEFAULT gen_random_uuid(),
  project_id        uuid,
  source_entity_id  uuid NOT NULL,
  target_entity_id  uuid NOT NULL,
  relation          text NOT NULL
                      CHECK (relation IN ('BELONGS_TO','REFERENCES','SUPERSEDES','SUPPORTS','CONTRADICTS',
                                          'BILLED_BY','ORDERED_FROM','MATCHES','APPROVES','REQUIRES',
                                          'DERIVED_FROM','AFFECTS')),
  status            text NOT NULL DEFAULT 'proposed'
                      CHECK (status IN ('proposed','approved','rejected','superseded')),
  origin            text NOT NULL DEFAULT 'derived'
                      CHECK (origin IN ('observed','derived','approved')),
  confidence        double precision CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
  evidence_ids      uuid[] NOT NULL DEFAULT '{}',
  valid_from        timestamptz,
  valid_until       timestamptz,
  decided_by        text,
  decided_at        timestamptz,
  annotations       jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  created_by        text NOT NULL DEFAULT 'system',
  PRIMARY KEY (organization_id, relationship_id),
  UNIQUE (organization_id, source_entity_id, relation, target_entity_id, valid_from),
  -- A relationship an AI proposed cannot be marked approved without a human.
  CHECK (status <> 'approved' OR (decided_by IS NOT NULL AND decided_by <> 'ai') OR origin = 'observed'),
  FOREIGN KEY (organization_id, project_id) REFERENCES projects(organization_id, project_id) ON UPDATE CASCADE ON DELETE SET NULL,
  FOREIGN KEY (organization_id, source_entity_id) REFERENCES entities(organization_id, entity_id) ON UPDATE CASCADE ON DELETE CASCADE,
  FOREIGN KEY (organization_id, target_entity_id) REFERENCES entities(organization_id, entity_id) ON UPDATE CASCADE ON DELETE CASCADE
);
CREATE TRIGGER relationships_touch BEFORE UPDATE ON relationships FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE INDEX relationships_source_idx ON relationships(organization_id, source_entity_id, relation);
CREATE INDEX relationships_target_idx ON relationships(organization_id, target_entity_id, relation);
CREATE INDEX relationships_project_status_idx ON relationships(organization_id, project_id, status);

-- ---------------------------------------------------------------------------
-- Jobs and idempotency
-- ---------------------------------------------------------------------------
CREATE TABLE jobs (
  organization_id uuid NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
  job_id          uuid NOT NULL DEFAULT gen_random_uuid(),
  project_id      uuid,
  job_type        text NOT NULL,
  status          text NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued','running','completed','failed')),
  payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
  result          jsonb,
  error           text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  created_by      text NOT NULL DEFAULT 'system',
  PRIMARY KEY (organization_id, job_id),
  FOREIGN KEY (organization_id, project_id) REFERENCES projects(organization_id, project_id) ON UPDATE CASCADE ON DELETE SET NULL
);
CREATE TRIGGER jobs_touch BEFORE UPDATE ON jobs FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE INDEX jobs_org_status_idx ON jobs(organization_id, status);

CREATE TABLE idempotency_keys (
  organization_id uuid NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
  scope           text NOT NULL,
  key             text NOT NULL,
  result          jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organization_id, scope, key)
);

-- ---------------------------------------------------------------------------
-- Audit: hash-chained per organization
-- ---------------------------------------------------------------------------
CREATE TABLE audit_events (
  organization_id uuid NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
  audit_event_id  uuid NOT NULL DEFAULT gen_random_uuid(),
  sequence        bigint NOT NULL,
  project_id      uuid,
  event_type      text NOT NULL,
  actor           text NOT NULL,
  object_type     text NOT NULL,
  object_id       uuid,
  payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
  occurred_at     timestamptz NOT NULL,
  prev_hash       text,
  entry_hash      text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organization_id, audit_event_id),
  -- The chain is per organization; a shared sequence leaks activity between tenants.
  UNIQUE (organization_id, sequence),
  UNIQUE (organization_id, entry_hash)
);
CREATE INDEX audit_events_object_idx ON audit_events(organization_id, object_type, object_id);

-- ---------------------------------------------------------------------------
-- Row-level security — the second wall (see TENANCY.md §6)
--
-- FORCE so the table owner is subject to it too. Superusers still bypass RLS,
-- which is exactly why the application connects as construction_app.
-- A session that never set app.organization_id sees nothing.
-- ---------------------------------------------------------------------------
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'people','companies','projects','project_memberships','communications','documents',
    'document_versions','evidence','purchase_orders','quotes','invoices','invoice_lines',
    'decisions','approvals','approval_packets','entities','relationships','jobs',
    'idempotency_keys','audit_events'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format($p$
      CREATE POLICY %I ON %I
        USING (organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid)
        WITH CHECK (organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid)
    $p$, t || '_tenant_isolation', t);
  END LOOP;
END $$;

-- The application role. Non-superuser by construction: superusers bypass RLS.
-- scripts/bootstrap_app_role.py gives it a password from the environment.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'construction_app') THEN
    CREATE ROLE construction_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
END $$;

GRANT USAGE ON SCHEMA public TO construction_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO construction_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO construction_app;
-- The app must not be able to rewrite its own schema or migration history.
REVOKE INSERT, UPDATE, DELETE ON schema_migrations FROM construction_app;
