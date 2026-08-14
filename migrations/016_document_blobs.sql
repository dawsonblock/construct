-- 016_document_blobs.sql — blob/occurrence split and concurrency-safe versions
-- (items 26, 27).
--
-- The previous schema had UNIQUE(organization_id, content_hash) on
-- document_versions, meaning the same bytes could only exist as one version
-- per tenant. But the same PDF might legitimately appear in multiple documents
-- (e.g., attached to two projects). The blob/occurrence split fixes this:
--
--   document_blobs: content-addressed immutable bytes (one row per unique
--                   content hash per tenant). Stores the storage_uri, byte_size,
--                   mime_type, and extracted text/tables.
--   document_versions: an occurrence of a blob within a document. Multiple
--                      versions can point to the same blob. Version numbers
--                      are concurrency-safe via an advisory lock.
--
-- The UNIQUE(organization_id, content_hash) constraint on document_versions
-- is dropped; the content_hash column is retained for backward compatibility
-- but the canonical reference is document_blobs.content_hash.

CREATE TABLE document_blobs (
  organization_id  uuid NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
  blob_id          uuid NOT NULL DEFAULT gen_random_uuid(),
  content_hash     text NOT NULL,
  byte_size        bigint NOT NULL DEFAULT 0,
  mime_type        text NOT NULL DEFAULT 'application/octet-stream',
  storage_uri      text NOT NULL,
  extracted_text   text NOT NULL DEFAULT '',
  extraction_warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
  tables           jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organization_id, blob_id),
  UNIQUE (organization_id, content_hash)
);

ALTER TABLE document_blobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_blobs FORCE ROW LEVEL SECURITY;
CREATE POLICY document_blobs_tenant_isolation ON document_blobs
  USING (organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid)
  WITH CHECK (organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid);

GRANT SELECT, INSERT ON document_blobs TO construction_app;

-- Add blob_id to document_versions (nullable for backward compatibility —
-- existing versions don't have a blob row yet).
ALTER TABLE document_versions ADD COLUMN IF NOT EXISTS blob_id uuid;

-- Drop the content_hash uniqueness constraint so the same bytes can appear
-- in multiple documents.
ALTER TABLE document_versions DROP CONSTRAINT IF EXISTS document_versions_organization_id_content_hash_key;

-- Add a foreign key from document_versions to document_blobs.
ALTER TABLE document_versions
  ADD CONSTRAINT document_versions_blob_fk
  FOREIGN KEY (organization_id, blob_id) REFERENCES document_blobs(organization_id, blob_id)
  ON DELETE SET NULL;

-- Index for finding versions by blob.
CREATE INDEX IF NOT EXISTS document_versions_blob_idx
  ON document_versions(organization_id, blob_id) WHERE blob_id IS NOT NULL;
