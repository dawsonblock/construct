-- 008_identity_bindings_grants.sql — complete the app role's access to identity_bindings.
--
-- 007 granted only SELECT on identity_bindings, reasoning that it is a pre-scope
-- lookup table. But binding an identity (user provisioning, org-admin) writes to
-- it, and the app role runs those writes. identity_bindings is deliberately not
-- under RLS, exactly like organization_api_keys: it is consulted to *discover*
-- who is logging in before a scope exists, so the lookup cannot be scoped. The
-- (provider, provider_subject) pair is globally unique and unguessable, matching
-- the api-key hash lookup's trust model.
--
-- The permissions catalog stays read-only for the app role; only migrations
-- (which run as the owner) write to it.

GRANT INSERT, UPDATE, DELETE ON identity_bindings TO construction_app;
