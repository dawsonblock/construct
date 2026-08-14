-- 011_audit_hardening.sql — append-only audit and role separation (item 19).
--
-- The application role must not be able to rewrite its own history or
-- reconfigure its security boundary. audit_events and approval_decisions become
-- append-only for the app role: SELECT and INSERT only, no UPDATE, DELETE or
-- TRUNCATE. organizations can no longer be reconfigured (UPDATE) by the app
-- role; it can still SELECT and (for the cascade cleanup path) DELETE.
--
-- Separate roles are introduced for the deployment's role split:
--   construct_audit_reader — read-only over the audit ledger
--   construct_admin        — the security-boundary tables (orgs, keys, users)
--   construct_migrator     — schema ownership (the DATABASE_URL owner in compose)
--   construct_worker       — the job worker (currently shares construction_app)
-- They are created as NOLOGIN group roles here; the deploy assigns login
-- passwords the way scripts/bootstrap_app_role.py does for construction_app.
-- The runtime still connects as construction_app, now with reduced privileges.

-- ---------------------------------------------------------------------------
-- Append-only audit_events and approval_decisions
-- ---------------------------------------------------------------------------
REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM construction_app;
REVOKE UPDATE, DELETE, TRUNCATE ON approval_decisions FROM construction_app;

-- The app role cannot reconfigure the organization security boundary.
REVOKE UPDATE ON organizations FROM construction_app;

-- ---------------------------------------------------------------------------
-- construct_audit_reader — read-only over the audit ledger
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'construct_audit_reader') THEN
    CREATE ROLE construct_audit_reader NOLOGIN NOBYPASSRLS;
  END IF;
END $$;
GRANT SELECT ON audit_events TO construct_audit_reader;

-- ---------------------------------------------------------------------------
-- construct_admin — the security-boundary tables
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'construct_admin') THEN
    CREATE ROLE construct_admin NOLOGIN NOBYPASSRLS;
  END IF;
END $$;
GRANT SELECT, INSERT, UPDATE, DELETE ON organizations, organization_api_keys TO construct_admin;
GRANT SELECT, INSERT, UPDATE, DELETE ON users, identity_bindings, roles, role_permissions, user_roles, approval_authorities TO construct_admin;
GRANT SELECT ON permissions TO construct_admin;

-- ---------------------------------------------------------------------------
-- construct_migrator / construct_worker — declared for the deploy's role split
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'construct_migrator') THEN
    CREATE ROLE construct_migrator NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'construct_worker') THEN
    CREATE ROLE construct_worker NOLOGIN NOBYPASSRLS;
  END IF;
END $$;
