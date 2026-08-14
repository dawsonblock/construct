-- PostgreSQL production shape for AI control-plane state.
CREATE TABLE IF NOT EXISTS external_entity_map (
 internal_id text NOT NULL, system text NOT NULL, entity_type text NOT NULL,
 external_id text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(system, entity_type, external_id), UNIQUE(internal_id, system)
);
CREATE TABLE IF NOT EXISTS entity_edges (
 source_id text NOT NULL, relationship_type text NOT NULL, target_id text NOT NULL,
 valid_from timestamptz, valid_until timestamptz, confidence double precision NOT NULL,
 source_evidence_id text, PRIMARY KEY(source_id,relationship_type,target_id,valid_from)
);
CREATE TABLE IF NOT EXISTS evidence (
 evidence_id text PRIMARY KEY, organization_id text NOT NULL, source_type text NOT NULL,
 source_id text NOT NULL, field text NOT NULL, value jsonb NOT NULL,
 confidence double precision NOT NULL, authority double precision NOT NULL,
 observed_at timestamptz NOT NULL, extractor text NOT NULL
);
