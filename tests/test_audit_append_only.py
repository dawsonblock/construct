"""v0.4.3 — append-only audit and database-role separation (item 19).

The application role must not be able to rewrite its own history. These tests
prove that ``construction_app`` can append audit events and approval decisions
but cannot UPDATE, DELETE or TRUNCATE them, and cannot reconfigure the
organization security boundary. They run against a real PostgreSQL connected as
the non-superuser app role, so the privileges are the ones that actually apply
in production.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import psycopg
import pytest

from construction_ai.persistence.db import Scope


def _app_dsn() -> str:
    import os

    return os.getenv("TEST_DATABASE_URL") or os.getenv("APP_DATABASE_URL") or (
        "postgresql://construction_app:construction_app@localhost:5432/construction_ai"
    )


def _owner_dsn() -> str:
    import os

    return os.getenv("DATABASE_URL") or (
        "postgresql://construction:construction@localhost:5432/construction_ai"
    )


def _require_db() -> psycopg.Connection:
    try:
        return psycopg.connect(_app_dsn())
    except psycopg.OperationalError as exc:
        if os.getenv("REQUIRE_INTEGRATION") == "1":  # noqa: F821
            pytest.fail(f"no PostgreSQL for app role: {exc}")
        pytest.skip(f"no PostgreSQL for app role: {exc}")


import os  # noqa: E402


# --------------------------------------------------------------------------
# Privilege shape — the app role's grants on the append-only tables
# --------------------------------------------------------------------------

def test_app_role_cannot_update_audit_events():
    """UPDATE on audit_events must be revoked from construction_app."""
    with psycopg.connect(_owner_dsn()) as owner:
        with owner.cursor() as cur:
            cur.execute(
                """
                SELECT privilege_type FROM information_schema.role_table_grants
                WHERE grantee = 'construction_app' AND table_name = 'audit_events'
                """,
            )
            privs = {row[0] for row in cur.fetchall()}
    assert "INSERT" in privs, "app role must be able to append audit events"
    assert "SELECT" in privs, "app role must be able to read audit events"
    assert "UPDATE" not in privs, "app role must not UPDATE audit_events (append-only)"
    assert "DELETE" not in privs, "app role must not DELETE audit_events (append-only)"


def test_app_role_cannot_update_approval_decisions():
    """UPDATE/DELETE on approval_decisions must be revoked from construction_app."""
    with psycopg.connect(_owner_dsn()) as owner:
        with owner.cursor() as cur:
            cur.execute(
                """
                SELECT privilege_type FROM information_schema.role_table_grants
                WHERE grantee = 'construction_app' AND table_name = 'approval_decisions'
                """,
            )
            privs = {row[0] for row in cur.fetchall()}
    assert "INSERT" in privs, "app role must be able to record approval decisions"
    assert "SELECT" in privs, "app role must be able to read approval decisions"
    assert "UPDATE" not in privs, "app role must not UPDATE approval_decisions (append-only)"
    assert "DELETE" not in privs, "app role must not DELETE approval_decisions (append-only)"


def test_app_role_cannot_reconfigure_organizations():
    """UPDATE on organizations must be revoked: the security boundary is immutable to the app."""
    with psycopg.connect(_owner_dsn()) as owner:
        with owner.cursor() as cur:
            cur.execute(
                """
                SELECT privilege_type FROM information_schema.role_table_grants
                WHERE grantee = 'construction_app' AND table_name = 'organizations'
                """,
            )
            privs = {row[0] for row in cur.fetchall()}
    assert "UPDATE" not in privs, "app role must not UPDATE organizations (security boundary)"


# --------------------------------------------------------------------------
# Behaviour — the app role can append but not mutate or erase
# --------------------------------------------------------------------------

def test_app_role_can_append_audit_event(repos, org_a):
    """The legitimate append path still works after the privilege reduction."""
    scope: Scope = org_a["scope"]
    event = repos.audit.append(
        scope=scope,
        event_type="test.append_only_probe",
        actor="append-only-test",
        object_type="test",
        object_id=uuid.uuid4(),
        payload={"probe": "v0.4.3"},
        occurred_at=datetime.now(timezone.utc),
    )
    assert event.sequence >= 1
    assert event.entry_hash


def test_app_role_cannot_update_audit_event(repos, org_a):
    """A direct UPDATE on audit_events must be rejected by the database."""
    scope: Scope = org_a["scope"]
    event = repos.audit.append(
        scope=scope,
        event_type="test.update_probe",
        actor="append-only-test",
        object_type="test",
        object_id=uuid.uuid4(),
        payload={"probe": "before"},
    )
    with psycopg.connect(_app_dsn()) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.organization_id', %s, true)", (str(scope.organization_id),))
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(
                    "UPDATE audit_events SET actor = 'forged' WHERE organization_id = %s AND sequence = %s",
                    (scope.organization_id, event.sequence),
                )


def test_app_role_cannot_delete_audit_event(repos, org_a):
    """A direct DELETE on audit_events must be rejected by the database."""
    scope: Scope = org_a["scope"]
    event = repos.audit.append(
        scope=scope,
        event_type="test.delete_probe",
        actor="append-only-test",
        object_type="test",
        object_id=uuid.uuid4(),
        payload={"probe": "delete-me"},
    )
    with psycopg.connect(_app_dsn()) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.organization_id', %s, true)", (str(scope.organization_id),))
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(
                    "DELETE FROM audit_events WHERE organization_id = %s AND sequence = %s",
                    (scope.organization_id, event.sequence),
                )


def test_app_role_cannot_truncate_audit_events(repos, org_a):
    """TRUNCATE on audit_events must be rejected by the database."""
    scope: Scope = org_a["scope"]
    repos.audit.append(
        scope=scope,
        event_type="test.truncate_probe",
        actor="append-only-test",
        object_type="test",
        object_id=uuid.uuid4(),
        payload={"probe": "truncate-me"},
    )
    with psycopg.connect(_app_dsn()) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.organization_id', %s, true)", (str(scope.organization_id),))
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("TRUNCATE audit_events")


# --------------------------------------------------------------------------
# Separated roles exist for the deployment's role split
# --------------------------------------------------------------------------

def test_separated_database_roles_exist():
    """The deploy's role split depends on these group roles being declared."""
    with psycopg.connect(_owner_dsn()) as owner:
        with owner.cursor() as cur:
            cur.execute(
                """
                SELECT rolname, rolcanlogin, rolbypassrls
                FROM pg_roles
                WHERE rolname IN ('construct_audit_reader','construct_admin','construct_migrator','construct_worker')
                ORDER BY rolname
                """,
            )
            rows = {row[0]: (row[1], row[2]) for row in cur.fetchall()}
    assert "construct_audit_reader" in rows, "audit reader role must exist"
    assert "construct_admin" in rows, "admin role must exist"
    assert "construct_migrator" in rows, "migrator role must exist"
    assert "construct_worker" in rows, "worker role must exist"
    # All four are NOLOGIN group roles — the deploy assigns login passwords.
    for name, (can_login, bypass_rls) in rows.items():
        assert can_login is False, f"{name} must be NOLOGIN (a group role)"
        assert bypass_rls is False, f"{name} must not bypass RLS"


def test_audit_reader_role_has_read_only_audit_access():
    """construct_audit_reader can SELECT audit_events but not write."""
    with psycopg.connect(_owner_dsn()) as owner:
        with owner.cursor() as cur:
            cur.execute(
                """
                SELECT privilege_type FROM information_schema.role_table_grants
                WHERE grantee = 'construct_audit_reader' AND table_name = 'audit_events'
                """,
            )
            privs = {row[0] for row in cur.fetchall()}
    assert "SELECT" in privs
    assert "INSERT" not in privs
    assert "UPDATE" not in privs
    assert "DELETE" not in privs
