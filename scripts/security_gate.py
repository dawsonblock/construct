#!/usr/bin/env python
"""v0.4.7 — security gate (item 40).

Runs a focused set of security checks that must pass before a release is
qualified. These are not unit tests — they are invariant checks against the
live database, verifying that the security posture holds after migrations.

Usage:
    python scripts/security_gate.py

Exit code 0 = all checks passed.
Exit code 1 = one or more checks failed.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


def _dsn() -> str:
    return os.getenv(
        "TEST_DATABASE_URL",
        os.getenv("APP_DATABASE_URL", "postgresql://construction_app:construction_app@localhost:5432/construction_ai"),
    )


def _owner_dsn() -> str:
    return os.getenv(
        "DATABASE_URL",
        "postgresql://construction:construction@localhost:5432/construction_ai",
    )


def check_app_role_is_not_superuser() -> CheckResult:
    """The app role must not be a superuser (RLS would be vacuous)."""
    import psycopg

    with psycopg.connect(_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_user, usesuper FROM pg_user WHERE usename = current_user")
            row = cur.fetchone()
            if row and row[1]:
                return CheckResult("app_role_not_superuser", False, f"app role {row[0]} is a superuser")
    return CheckResult("app_role_not_superuser", True, "app role is not a superuser")


def check_app_role_cannot_bypass_rls() -> CheckResult:
    """The app role must not have BYPASSRLS."""
    import psycopg

    with psycopg.connect(_owner_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT rolbypassrls FROM pg_roles WHERE rolname = 'construction_app'")
            row = cur.fetchone()
            if row and row[0]:
                return CheckResult("app_role_no_bypassrls", False, "app role has BYPASSRLS")
    return CheckResult("app_role_no_bypassrls", True, "app role does not have BYPASSRLS")


def check_rls_enabled_on_tenant_tables() -> CheckResult:
    """RLS must be enabled and forced on all tenant-scoped tables."""
    import psycopg

    tenant_tables = [
        "projects", "companies", "documents", "document_versions", "document_blobs",
        "evidence", "invoices", "purchase_orders", "quotes", "approvals",
        "entities", "relationships", "decisions", "external_actions",
        "audit_events", "communications", "jobs",
    ]
    with psycopg.connect(_owner_dsn()) as conn:
        with conn.cursor() as cur:
            for table in tenant_tables:
                cur.execute(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s",
                    (table,),
                )
                row = cur.fetchone()
                if row is None:
                    return CheckResult("rls_enabled", False, f"table {table} not found")
                if not row[0]:
                    return CheckResult("rls_enabled", False, f"RLS not enabled on {table}")
                if not row[1]:
                    return CheckResult("rls_enabled", False, f"RLS not forced on {table}")
    return CheckResult("rls_enabled", True, f"RLS enabled and forced on {len(tenant_tables)} tenant tables")


def check_audit_table_is_append_only_for_app_role() -> CheckResult:
    """The app role must not have UPDATE/DELETE/TRUNCATE on audit_events."""
    import psycopg

    with psycopg.connect(_dsn()) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            # Try to UPDATE — should fail.
            try:
                cur.execute("SELECT set_config('app.organization_id', '00000000-0000-0000-0000-000000000000', true)")
                cur.execute("UPDATE audit_events SET event_type = 'test' WHERE false")
                return CheckResult("audit_append_only", False, "app role can UPDATE audit_events")
            except Exception:
                pass  # expected

            # Try to DELETE — should fail.
            try:
                cur.execute("DELETE FROM audit_events WHERE false")
                return CheckResult("audit_append_only", False, "app role can DELETE audit_events")
            except Exception:
                pass  # expected

            # Try to TRUNCATE — should fail.
            try:
                cur.execute("TRUNCATE audit_events")
                return CheckResult("audit_append_only", False, "app role can TRUNCATE audit_events")
            except Exception:
                pass  # expected

    return CheckResult("audit_append_only", True, "audit_events is append-only for app role")


def check_external_actions_is_append_only_for_app_role() -> CheckResult:
    """The app role must not have UPDATE/DELETE on external_actions."""
    import psycopg

    with psycopg.connect(_dsn()) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT set_config('app.organization_id', '00000000-0000-0000-0000-000000000000', true)")
                cur.execute("UPDATE external_actions SET status = 'test' WHERE false")
                return CheckResult("external_actions_append_only", False, "app role can UPDATE external_actions")
            except Exception:
                pass
            try:
                cur.execute("DELETE FROM external_actions WHERE false")
                return CheckResult("external_actions_append_only", False, "app role can DELETE external_actions")
            except Exception:
                pass
    return CheckResult("external_actions_append_only", True, "external_actions is append-only for app role")


def check_cross_tenant_isolation() -> CheckResult:
    """An app-role connection with no tenant GUC sees zero rows from tenant tables."""
    import psycopg

    with psycopg.connect(_dsn()) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            # Clear any tenant setting.
            cur.execute("SELECT set_config('app.organization_id', '', true)")
            cur.execute("SELECT count(*) FROM projects")
            count = cur.fetchone()[0]
            if count != 0:
                return CheckResult("cross_tenant_isolation", False, f"app role sees {count} projects without tenant GUC")
    return CheckResult("cross_tenant_isolation", True, "no cross-tenant access without tenant GUC")


def check_migration_checksums_unchanged() -> CheckResult:
    """All applied migrations must have unchanged checksums."""
    import psycopg
    import hashlib
    from pathlib import Path

    migrations_dir = Path(__file__).parent.parent / "migrations"
    with psycopg.connect(_owner_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT filename, checksum FROM schema_migrations ORDER BY filename")
            applied = {row[0]: row[1] for row in cur.fetchall()}

    for filename, stored_checksum in applied.items():
        filepath = migrations_dir / filename
        if not filepath.exists():
            return CheckResult("migration_checksums", False, f"migration file {filename} is missing")
        content = filepath.read_bytes()
        computed = hashlib.sha256(content).hexdigest()
        if computed != stored_checksum:
            return CheckResult(
                "migration_checksums", False,
                f"migration {filename} checksum mismatch: stored={stored_checksum}, computed={computed}",
            )
    return CheckResult("migration_checksums", True, f"{len(applied)} migration checksums verified")


def run_all_checks() -> list[CheckResult]:
    """Run all security gate checks."""
    checks = [
        check_app_role_is_not_superuser,
        check_app_role_cannot_bypass_rls,
        check_rls_enabled_on_tenant_tables,
        check_audit_table_is_append_only_for_app_role,
        check_external_actions_is_append_only_for_app_role,
        check_cross_tenant_isolation,
        check_migration_checksums_unchanged,
    ]
    results = []
    for check in checks:
        try:
            results.append(check())
        except Exception as e:
            results.append(CheckResult(check.__name__, False, f"exception: {e}"))
    return results


def main() -> int:
    print("=== v0.4.7 Security Gate ===\n")
    results = run_all_checks()
    all_passed = True
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"  [{status}] {r.name}: {r.detail}")
        if not r.passed:
            all_passed = False
    print()
    if all_passed:
        print(f"All {len(results)} security checks passed.")
        return 0
    else:
        failed = sum(1 for r in results if not r.passed)
        print(f"{failed}/{len(results)} security checks FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
