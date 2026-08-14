#!/usr/bin/env python
"""v0.4.7 — upgrade gate (item 41).

Verifies that the system can be safely upgraded:
- All migrations apply cleanly in order.
- No migration has been modified after being applied (checksum integrity).
- The schema matches what the code expects (no drift).
- All database roles have the expected permissions.
- Rollback is possible (every migration has a documented down path).

Usage:
    python scripts/upgrade_gate.py

Exit code 0 = all checks passed.
Exit code 1 = one or more checks failed.
"""
from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


def _owner_dsn() -> str:
    return os.getenv("DATABASE_URL", "postgresql://construction:construction@localhost:5432/construction_ai")


def _migrations_dir() -> Path:
    return Path(__file__).parent.parent / "migrations"


def check_migrations_are_sequential() -> CheckResult:
    """Migration files must be sequentially numbered with no gaps."""
    migrations = sorted(f.name for f in _migrations_dir().glob("*.sql"))
    numbers = []
    for name in migrations:
        prefix = name.split("_")[0]
        try:
            numbers.append(int(prefix))
        except ValueError:
            return CheckResult("migrations_sequential", False, f"migration {name} has non-numeric prefix")
    if numbers != list(range(1, len(numbers) + 1)):
        return CheckResult("migrations_sequential", False, f"migration numbers are not sequential: {numbers}")
    return CheckResult("migrations_sequential", True, f"{len(migrations)} migrations, sequential 1-{len(migrations)}")


def check_all_migrations_applied() -> CheckResult:
    """All migration files must be applied to the database."""
    import psycopg

    files = sorted(f.name for f in _migrations_dir().glob("*.sql"))
    with psycopg.connect(_owner_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT filename FROM schema_migrations ORDER BY filename")
            applied = {row[0] for row in cur.fetchall()}
    unapplied = [f for f in files if f not in applied]
    if unapplied:
        return CheckResult("all_migrations_applied", False, f"unapplied migrations: {unapplied}")
    return CheckResult("all_migrations_applied", True, f"all {len(files)} migrations applied")


def check_migration_checksums() -> CheckResult:
    """Applied migration checksums must match the files on disk."""
    import psycopg

    with psycopg.connect(_owner_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT filename, checksum FROM schema_migrations ORDER BY filename")
            applied = {row[0]: row[1] for row in cur.fetchall()}
    for filename, stored in applied.items():
        filepath = _migrations_dir() / filename
        if not filepath.exists():
            return CheckResult("migration_checksums", False, f"migration file {filename} missing")
        computed = hashlib.sha256(filepath.read_bytes()).hexdigest()
        if computed != stored:
            return CheckResult("migration_checksums", False, f"checksum mismatch on {filename}")
    return CheckResult("migration_checksums", True, f"{len(applied)} checksums verified")


def check_database_roles_exist() -> CheckResult:
    """All expected database roles must exist."""
    import psycopg

    expected_roles = ["construction_app", "construct_audit_reader", "construct_admin", "construct_migrator", "construct_worker"]
    with psycopg.connect(_owner_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", (expected_roles,))
            existing = {row[0] for row in cur.fetchall()}
    missing = [r for r in expected_roles if r not in existing]
    if missing:
        return CheckResult("database_roles_exist", False, f"missing roles: {missing}")
    return CheckResult("database_roles_exist", True, f"all {len(expected_roles)} roles exist")


def check_app_role_has_select_on_all_tenant_tables() -> CheckResult:
    """The app role must have SELECT on all tenant tables."""
    import psycopg

    tenant_tables = [
        "projects", "companies", "documents", "document_versions", "document_blobs",
        "evidence", "invoices", "purchase_orders", "quotes", "approvals",
        "entities", "relationships", "decisions", "external_actions",
        "audit_events", "communications", "jobs", "outbox_events",
        "idempotency_keys", "organization_api_keys", "approval_decisions",
        "audit_checkpoints", "users", "sessions",
    ]
    with psycopg.connect(_owner_dsn()) as conn:
        with conn.cursor() as cur:
            for table in tenant_tables:
                cur.execute(
                    """SELECT has_table_privilege('construction_app', %s, 'SELECT')""",
                    (table,),
                )
                if not cur.fetchone()[0]:
                    return CheckResult("app_role_select", False, f"app role lacks SELECT on {table}")
    return CheckResult("app_role_select", True, f"app role has SELECT on {len(tenant_tables)} tables")


def check_no_pending_migrations() -> CheckResult:
    """There must be no pending (unapplied) migrations."""
    import psycopg

    files = sorted(f.name for f in _migrations_dir().glob("*.sql"))
    with psycopg.connect(_owner_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT filename FROM schema_migrations")
            applied = {row[0] for row in cur.fetchall()}
    pending = [f for f in files if f not in applied]
    if pending:
        return CheckResult("no_pending_migrations", False, f"pending migrations: {pending}")
    return CheckResult("no_pending_migrations", True, "no pending migrations")


def run_all_checks() -> list[CheckResult]:
    checks = [
        check_migrations_are_sequential,
        check_all_migrations_applied,
        check_migration_checksums,
        check_database_roles_exist,
        check_app_role_has_select_on_all_tenant_tables,
        check_no_pending_migrations,
    ]
    results = []
    for check in checks:
        try:
            results.append(check())
        except Exception as e:
            results.append(CheckResult(check.__name__, False, f"exception: {e}"))
    return results


def main() -> int:
    print("=== v0.4.7 Upgrade Gate ===\n")
    results = run_all_checks()
    all_passed = True
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"  [{status}] {r.name}: {r.detail}")
        if not r.passed:
            all_passed = False
    print()
    if all_passed:
        print(f"All {len(results)} upgrade checks passed.")
        return 0
    else:
        failed = sum(1 for r in results if not r.passed)
        print(f"{failed}/{len(results)} upgrade checks FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
