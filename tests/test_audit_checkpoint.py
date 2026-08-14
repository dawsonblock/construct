"""v0.4.3 — external audit checkpointing (item 20).

A checkpoint captures the state of an organization's audit chain at a point in
time. Even if someone with owner-level DB access rewrites the chain, a
previously exported checkpoint will detect the discrepancy when verified.
"""
from __future__ import annotations

import os

import psycopg
import pytest

from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories.audit import checkpoint_hash


def _owner_dsn() -> str:
    return os.getenv("DATABASE_URL") or (
        "postgresql://construction:construction@localhost:5432/construction_ai"
    )


# --------------------------------------------------------------------------
# Checkpoint creation and verification
# --------------------------------------------------------------------------

def test_checkpoint_captures_chain_state(repos, org_a):
    """A checkpoint records the current chain head: sequence, hash, count."""
    scope: Scope = org_a["scope"]
    repos.audit.append(scope=scope, event_type="cp.test", actor="test", object_type="test", payload={"n": 1})
    repos.audit.append(scope=scope, event_type="cp.test", actor="test", object_type="test", payload={"n": 2})

    cp = repos.audit.create_checkpoint(scope=scope, exported_by="test")
    assert cp.sequence == 2
    assert cp.entry_hash  # non-empty
    assert cp.event_count == 2
    assert cp.checkpoint_hash
    assert cp.checkpoint_id

    # The checkpoint hash is deterministic given the same inputs.
    expected = checkpoint_hash(
        organization_id=scope.organization_id,
        sequence=cp.sequence,
        entry_hash=cp.entry_hash,
        event_count=cp.event_count,
        created_at=cp.created_at,
    )
    assert cp.checkpoint_hash == expected


def test_checkpoint_verifies_unchanged_chain(repos, org_a):
    """A checkpoint matches the chain when nothing has changed."""
    scope: Scope = org_a["scope"]
    repos.audit.append(scope=scope, event_type="cp.test", actor="test", object_type="test", payload={"n": 1})
    cp = repos.audit.create_checkpoint(scope=scope, exported_by="test")
    assert repos.audit.verify_checkpoint(scope=scope, checkpoint=cp) is True


def test_checkpoint_detects_new_events(repos, org_a):
    """A checkpoint does NOT match after new events are appended.

    This is expected behavior: the checkpoint anchored a point in time. New
    events mean the chain has advanced. The caller should create a new
    checkpoint to capture the new state.
    """
    scope: Scope = org_a["scope"]
    repos.audit.append(scope=scope, event_type="cp.test", actor="test", object_type="test", payload={"n": 1})
    cp = repos.audit.create_checkpoint(scope=scope, exported_by="test")
    repos.audit.append(scope=scope, event_type="cp.test", actor="test", object_type="test", payload={"n": 2})
    assert repos.audit.verify_checkpoint(scope=scope, checkpoint=cp) is False


def test_checkpoint_detects_tampered_chain(repos, org_a):
    """A checkpoint detects chain rewriting even by someone with owner access."""
    scope: Scope = org_a["scope"]
    repos.audit.append(scope=scope, event_type="cp.test", actor="test", object_type="test", payload={"n": 1})
    repos.audit.append(scope=scope, event_type="cp.test", actor="test", object_type="test", payload={"n": 2})
    cp = repos.audit.create_checkpoint(scope=scope, exported_by="test")

    # Simulate owner-level tampering: rewrite event 1's payload.
    with psycopg.connect(_owner_dsn()) as owner:
        with owner.cursor() as cur:
            cur.execute(
                "UPDATE audit_events SET payload = '{\"n\": 999}'::jsonb "
                "WHERE organization_id = %s AND sequence = 1",
                (scope.organization_id,),
            )
        owner.commit()

    # The chain hash no longer matches — the checkpoint detects it.
    assert repos.audit.verify_checkpoint(scope=scope, checkpoint=cp) is False
    # And the chain verification also fails.
    assert repos.audit.verify_chain(scope=scope) is False


def test_empty_chain_checkpoint(repos, org_a):
    """A checkpoint on an empty chain records sequence=0, count=0."""
    scope: Scope = org_a["scope"]
    cp = repos.audit.create_checkpoint(scope=scope, exported_by="test")
    assert cp.sequence == 0
    assert cp.event_count == 0
    assert cp.entry_hash == ""
    assert repos.audit.verify_checkpoint(scope=scope, checkpoint=cp) is True


def test_checkpoints_are_listed(repos, org_a):
    """Multiple checkpoints are listed in creation order."""
    scope: Scope = org_a["scope"]
    repos.audit.append(scope=scope, event_type="cp.test", actor="test", object_type="test", payload={"n": 1})
    repos.audit.create_checkpoint(scope=scope, exported_by="test-1")
    repos.audit.append(scope=scope, event_type="cp.test", actor="test", object_type="test", payload={"n": 2})
    repos.audit.create_checkpoint(scope=scope, exported_by="test-2")

    checkpoints = repos.audit.list_checkpoints(scope=scope)
    assert len(checkpoints) == 2
    assert checkpoints[0]["exported_by"] == "test-1"
    assert checkpoints[1]["exported_by"] == "test-2"
    assert checkpoints[0]["sequence"] == 1
    assert checkpoints[1]["sequence"] == 2


# --------------------------------------------------------------------------
# Checkpoints are append-only (same as audit_events)
# --------------------------------------------------------------------------

def test_app_role_cannot_update_checkpoints(repos, org_a):
    """The app role must not be able to UPDATE audit_checkpoints."""
    scope: Scope = org_a["scope"]
    cp = repos.audit.create_checkpoint(scope=scope, exported_by="test")
    with psycopg.connect(
        os.getenv("TEST_DATABASE_URL") or os.getenv("APP_DATABASE_URL") or
        "postgresql://construction_app:construction_app@localhost:5432/construction_ai"
    ) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.organization_id', %s, true)", (str(scope.organization_id),))
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(
                    "UPDATE audit_checkpoints SET exported_by = 'forged' WHERE organization_id = %s AND checkpoint_id = %s",
                    (scope.organization_id, cp.checkpoint_id),
                )


def test_app_role_cannot_delete_checkpoints(repos, org_a):
    """The app role must not be able to DELETE audit_checkpoints."""
    scope: Scope = org_a["scope"]
    cp = repos.audit.create_checkpoint(scope=scope, exported_by="test")
    with psycopg.connect(
        os.getenv("TEST_DATABASE_URL") or os.getenv("APP_DATABASE_URL") or
        "postgresql://construction_app:construction_app@localhost:5432/construction_ai"
    ) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.organization_id', %s, true)", (str(scope.organization_id),))
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(
                    "DELETE FROM audit_checkpoints WHERE organization_id = %s AND checkpoint_id = %s",
                    (scope.organization_id, cp.checkpoint_id),
                )


# --------------------------------------------------------------------------
# Tenant isolation for checkpoints
# --------------------------------------------------------------------------

def test_checkpoint_is_tenant_isolated(repos, org_a, org_b):
    """Org B cannot see org A's checkpoints."""
    scope_a: Scope = org_a["scope"]
    scope_b: Scope = org_b["scope"]
    repos.audit.append(scope=scope_a, event_type="cp.test", actor="test", object_type="test", payload={"n": 1})
    repos.audit.create_checkpoint(scope=scope_a, exported_by="test-a")

    # Org B sees zero checkpoints.
    assert repos.audit.list_checkpoints(scope=scope_b) == []
