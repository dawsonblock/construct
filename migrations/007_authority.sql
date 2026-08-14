-- 007_authority.sql — human approver identity and authorization.
--
-- v0.3 authenticated the *organization* with a bearer API key but never the
-- *human*. Every approval mutation took `user_id` from the request body, so any
-- holder of the org key could approve as anyone — including a spoofed CFO. This
-- migration lands the authority model that v0.4.1 builds on:
--
--   provider_subject -> user -> organization        (identity)
--   user -> roles -> permissions                     (authorization, DENY by default)
--   user -> approval_authorities                     (amount / currency / project limits)
--   server session -> AuthenticatedActor             (no caller-supplied identity)
--   approval_decisions                              (append-only decision history)
--
-- Conventions match 003: tenant-owned tables carry organization_id as the
-- leading key, are ENABLE + FORCE RLS, and are granted to construction_app.
-- `identity_bindings` and `permissions` are deliberately NOT under RLS:
-- identity_bindings is consulted to discover who is logging in before a scope
-- exists (exactly like organization_api_keys), and permissions is a global
-- reference catalog shared by every tenant.

-- ---------------------------------------------------------------------------
-- Permissions catalog (global reference, shared)
-- ---------------------------------------------------------------------------
CREATE TABLE permissions (
  name        text PRIMARY KEY,
  description text NOT NULL DEFAULT ''
);

INSERT INTO permissions(name, description) VALUES
  ('invoice.read',      'Read invoices and approval packets'),
  ('invoice.review',    'Work the human review queue'),
  ('invoice.hold',      'Place an invoice on hold'),
  ('invoice.approve',   'Approve an invoice for ERP submission'),
  ('invoice.reject',    'Reject an invoice'),
  ('invoice.submit_erp','Submit an approved invoice to the ERP'),
  ('relationship.review','Review a proposed graph relationship'),
  ('relationship.promote','Promote a proposed graph relationship to approved'),
  ('project.admin',     'Administer a project and its references'),
  ('organization.admin','Administer an organization: users, roles, keys'),
  ('policy.admin',      'Administer authorization policy versions')
ON CONFLICT (name) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Users and identity bindings
-- ---------------------------------------------------------------------------
CREATE TABLE users (
  organization_id uuid NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
  user_id         uuid NOT NULL DEFAULT gen_random_uuid(),
  display_name    text NOT NULL,
  email           text,
  status          text NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','disabled')),
  created_at      timestamptz NOT NULL DEFAULT now(),
  disabled_at     timestamptz,
  PRIMARY KEY (organization_id, user_id),
  UNIQUE (organization_id, email)
);

-- Not under RLS: consulted to resolve a login before a scope exists, like
-- organization_api_keys. The (provider, provider_subject) pair is globally
-- authoritative; one external subject maps to exactly one user in one org.
CREATE TABLE identity_bindings (
  organization_id  uuid NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
  user_id          uuid NOT NULL,
  provider         text NOT NULL,
  provider_subject text NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (provider, provider_subject),
  UNIQUE (organization_id, user_id, provider),
  FOREIGN KEY (organization_id, user_id) REFERENCES users(organization_id, user_id) ON UPDATE CASCADE ON DELETE CASCADE
);
CREATE INDEX identity_bindings_user_idx ON identity_bindings(organization_id, user_id);

-- ---------------------------------------------------------------------------
-- Roles and the role -> permission grant
-- ---------------------------------------------------------------------------
CREATE TABLE roles (
  organization_id uuid NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
  role_id         uuid NOT NULL DEFAULT gen_random_uuid(),
  name            text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organization_id, role_id),
  UNIQUE (organization_id, name)
);

CREATE TABLE role_permissions (
  organization_id uuid NOT NULL,
  role_id         uuid NOT NULL,
  permission      text NOT NULL REFERENCES permissions(name) ON DELETE CASCADE,
  PRIMARY KEY (organization_id, role_id, permission),
  FOREIGN KEY (organization_id, role_id) REFERENCES roles(organization_id, role_id) ON UPDATE CASCADE ON DELETE CASCADE
);

CREATE TABLE user_roles (
  organization_id uuid NOT NULL,
  user_id         uuid NOT NULL,
  role_id         uuid NOT NULL,
  PRIMARY KEY (organization_id, user_id, role_id),
  FOREIGN KEY (organization_id, user_id) REFERENCES users(organization_id, user_id) ON UPDATE CASCADE ON DELETE CASCADE,
  FOREIGN KEY (organization_id, role_id) REFERENCES roles(organization_id, role_id) ON UPDATE CASCADE ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- Approval authority: amount / currency / project limits per user
-- ---------------------------------------------------------------------------
CREATE TABLE approval_authorities (
  organization_id uuid NOT NULL,
  user_id         uuid NOT NULL,
  authority_id    uuid NOT NULL DEFAULT gen_random_uuid(),
  project_id      uuid,
  currency        text NOT NULL DEFAULT 'CAD',
  minimum_amount  numeric(14,2) NOT NULL DEFAULT 0,
  maximum_amount  numeric(14,2),         -- NULL means unlimited
  permission      text NOT NULL REFERENCES permissions(name),
  valid_from      timestamptz NOT NULL DEFAULT now(),
  valid_until     timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organization_id, authority_id),
  FOREIGN KEY (organization_id, user_id) REFERENCES users(organization_id, user_id) ON UPDATE CASCADE ON DELETE CASCADE,
  FOREIGN KEY (organization_id, project_id) REFERENCES projects(organization_id, project_id) ON UPDATE CASCADE ON DELETE CASCADE,
  CHECK (maximum_amount IS NULL OR maximum_amount >= minimum_amount)
);
CREATE INDEX approval_authorities_user_idx ON approval_authorities(organization_id, user_id, permission);

-- ---------------------------------------------------------------------------
-- Server-side sessions
--
-- A session is established by authentication, never by a request field. It
-- carries the user, the provider that vouched for them, and an authentication
-- strength the policy engine can require (e.g. MFA for high-value approvals).
-- ---------------------------------------------------------------------------
CREATE TABLE sessions (
  organization_id        uuid NOT NULL,
  session_id             uuid NOT NULL DEFAULT gen_random_uuid(),
  user_id                uuid NOT NULL,
  provider               text NOT NULL,
  authentication_strength text NOT NULL DEFAULT 'oidc',
  issued_at              timestamptz NOT NULL DEFAULT now(),
  expires_at             timestamptz NOT NULL,
  revoked_at             timestamptz,
  PRIMARY KEY (organization_id, session_id),
  FOREIGN KEY (organization_id, user_id) REFERENCES users(organization_id, user_id) ON UPDATE CASCADE ON DELETE CASCADE
);
CREATE INDEX sessions_user_idx ON sessions(organization_id, user_id);

-- ---------------------------------------------------------------------------
-- Append-only approval decisions
--
-- The approvals row still records the current decision, but the authoritative
-- history lives here. A later decision references the earlier one; the earlier
-- row is never overwritten. actor_id is a real FK to users — no free text.
-- ---------------------------------------------------------------------------
CREATE TABLE approval_decisions (
  organization_id          uuid NOT NULL,
  decision_id              uuid NOT NULL DEFAULT gen_random_uuid(),
  approval_id              uuid NOT NULL,
  project_id               uuid,
  invoice_id               uuid,
  decision                 text NOT NULL CHECK (decision IN ('approved','held','rejected')),
  actor_id                 uuid NOT NULL,
  actor_role_snapshot      jsonb NOT NULL DEFAULT '[]'::jsonb,
  policy_version           text,
  verification_packet_hash text,
  reason                   text NOT NULL DEFAULT '',
  previous_decision_id     uuid,
  created_at               timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organization_id, decision_id),
  FOREIGN KEY (organization_id, approval_id) REFERENCES approvals(organization_id, approval_id) ON UPDATE CASCADE ON DELETE CASCADE,
  FOREIGN KEY (organization_id, actor_id) REFERENCES users(organization_id, user_id) ON UPDATE CASCADE ON DELETE RESTRICT,
  FOREIGN KEY (organization_id, previous_decision_id) REFERENCES approval_decisions(organization_id, decision_id) ON UPDATE CASCADE ON DELETE RESTRICT
);
CREATE INDEX approval_decisions_approval_idx ON approval_decisions(organization_id, approval_id, created_at);

-- ---------------------------------------------------------------------------
-- Row-level security — same wall, same policy, as 003.
-- ---------------------------------------------------------------------------
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'users','roles','role_permissions','user_roles','approval_authorities',
    'sessions','approval_decisions'
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

-- The app role gets full CRUD on the new tenant-owned tables, SELECT on the
-- catalogs it reads. It must not grant itself anything on schema_migrations
-- (already revoked in 003) and must not write to the permissions catalog.
GRANT SELECT, INSERT, UPDATE, DELETE
  ON users, roles, role_permissions, user_roles, approval_authorities,
       sessions, approval_decisions TO construction_app;
GRANT SELECT ON permissions, identity_bindings TO construction_app;
