CREATE TABLE IF NOT EXISTS ai_objects (
 kind text NOT NULL,
 id text NOT NULL,
 organization_id text NOT NULL,
 payload jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(kind,id)
);
CREATE INDEX IF NOT EXISTS ai_objects_org_kind_idx ON ai_objects(organization_id,kind);

CREATE TABLE IF NOT EXISTS ai_approvals (
 approval_id text PRIMARY KEY,
 organization_id text NOT NULL,
 subject_id text NOT NULL,
 status text NOT NULL CHECK(status IN ('pending','approved','held','rejected')),
 payload jsonb NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ai_approvals_org_status_idx ON ai_approvals(organization_id,status);

CREATE TABLE IF NOT EXISTS approval_packets (
 packet_id text PRIMARY KEY,
 approval_id text NOT NULL REFERENCES ai_approvals(approval_id) DEFERRABLE INITIALLY DEFERRED,
 organization_id text NOT NULL,
 payload jsonb NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ai_idempotency (
 scope text NOT NULL,
 key text NOT NULL,
 result jsonb,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(scope,key)
);

CREATE TABLE IF NOT EXISTS ai_audit_log (
 seq bigserial PRIMARY KEY,
 timestamp timestamptz NOT NULL,
 actor text NOT NULL,
 action text NOT NULL,
 subject_id text NOT NULL,
 input_hash text NOT NULL,
 details jsonb NOT NULL,
 prev_hash text,
 entry_hash text NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS documents (
 document_id text PRIMARY KEY,
 organization_id text NOT NULL,
 source_id text,
 filename text NOT NULL,
 sha256 text NOT NULL,
 mime_type text NOT NULL,
 document_type text NOT NULL,
 storage_uri text,
 extracted_text text NOT NULL DEFAULT '',
 tables jsonb NOT NULL DEFAULT '[]'::jsonb,
 extraction_warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(organization_id,sha256)
);

CREATE TABLE IF NOT EXISTS communications (
 communication_id text NOT NULL,
 organization_id text NOT NULL,
 source text NOT NULL,
 thread_id text,
 sender text NOT NULL,
 recipients jsonb NOT NULL,
 subject text NOT NULL,
 body text NOT NULL,
 attachments jsonb NOT NULL,
 raw_hash text NOT NULL,
 received_at timestamptz NOT NULL,
 PRIMARY KEY(organization_id,communication_id),
 UNIQUE(organization_id,source,raw_hash)
);
