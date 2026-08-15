-- 027_rc6_approval_decisions_append_only.sql — restore append-only invariant.
--
-- Migration 026 granted UPDATE on approval_decisions to construction_app so
-- the rc6 policy_hash/policy_snapshot columns could be backfilled on existing
-- rows. That grant violates the append-only invariant established in
-- migration 011 (audit_events and approval_decisions must not be UPDATEable
-- by the app role). This migration revokes UPDATE on approval_decisions,
-- restoring the invariant. Future policy_snapshot writes must use INSERT.
REVOKE UPDATE ON approval_decisions FROM construction_app;
