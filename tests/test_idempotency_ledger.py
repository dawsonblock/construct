"""v0.4.4 — idempotency and external action ledger (item 25).

Verifies that:
1. Job enqueue with an idempotency key is idempotent — repeated calls with the
   same key return the same job, not duplicates.
2. The external action ledger prevents duplicate external effects on retry —
   recording an action with the same idempotency key returns the existing
   result instead of creating a duplicate.
3. The external action ledger is append-only (no UPDATE/DELETE for the app role).
4. The external action ledger is tenant-isolated.

These enforce the invariant:
  RepeatedExecution ⇒ NoDuplicateFinancialEffect
"""
from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest

from construction_ai.jobs.queue import JobQueue


class FakeRedis:
    def __init__(self):
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.groups: dict[str, dict[str, dict]] = {}
        self._msg_counter = 0

    def xadd(self, stream, fields):
        self._msg_counter += 1
        msg_id = f"0-{self._msg_counter}"
        self.streams.setdefault(stream, []).append((msg_id, dict(fields)))
        return msg_id

    def xgroup_create(self, stream, group, id="0", mkstream=False):
        if mkstream and stream not in self.streams:
            self.streams[stream] = []
        if stream not in self.streams:
            raise Exception("ERR no such key")
        if group in self.groups.get(stream, {}):
            raise Exception("BUSYGROUP Consumer Group name already exists")
        self.groups.setdefault(stream, {})[group] = {"consumers": {}, "pending": {}, "last_delivered_id": id}

    def xreadgroup(self, group, consumer, streams, count=1, block=0):
        return []

    def xack(self, stream, group, *msg_ids):
        return 0

    def xlen(self, stream):
        return len(self.streams.get(stream, []))

    def xpending(self, stream, group):
        return 0


@pytest.fixture()
def queue(repos):
    return JobQueue(repos, FakeRedis(), "test:jobs")


# --------------------------------------------------------------------------
# Idempotent enqueue — same key returns same job
# --------------------------------------------------------------------------

def test_idempotent_enqueue_returns_same_job(queue, org_a):
    """Enqueuing with the same idempotency key returns the same job."""
    key = f"inv-{uuid4().hex[:8]}"
    job1 = queue.enqueue(scope=org_a["scope"], job_type="invoice_document", payload={"text": "hello"}, idempotency_key=key)
    job2 = queue.enqueue(scope=org_a["scope"], job_type="invoice_document", payload={"text": "hello"}, idempotency_key=key)
    assert job1.job_id == job2.job_id


def test_idempotent_enqueue_does_not_create_duplicate(queue, org_a):
    """A second enqueue with the same key does not create a second job."""
    key = f"inv-{uuid4().hex[:8]}"
    queue.enqueue(scope=org_a["scope"], job_type="invoice_document", payload={"text": "hello"}, idempotency_key=key)
    queue.enqueue(scope=org_a["scope"], job_type="invoice_document", payload={"text": "hello"}, idempotency_key=key)
    jobs = queue.repos.jobs.list(scope=org_a["scope"])
    assert len(jobs) == 1


def test_different_idempotency_keys_create_different_jobs(queue, org_a):
    """Different idempotency keys create different jobs."""
    job1 = queue.enqueue(scope=org_a["scope"], job_type="invoice_document", payload={"text": "a"}, idempotency_key="key-1")
    job2 = queue.enqueue(scope=org_a["scope"], job_type="invoice_document", payload={"text": "b"}, idempotency_key="key-2")
    assert job1.job_id != job2.job_id
    assert len(queue.repos.jobs.list(scope=org_a["scope"])) == 2


def test_enqueue_without_idempotency_key_always_creates(queue, org_a):
    """Without an idempotency key, every enqueue creates a new job."""
    queue.enqueue(scope=org_a["scope"], job_type="invoice_document", payload={"text": "a"})
    queue.enqueue(scope=org_a["scope"], job_type="invoice_document", payload={"text": "b"})
    assert len(queue.repos.jobs.list(scope=org_a["scope"])) == 2


def test_idempotency_key_is_tenant_scoped(queue, org_a, org_b):
    """The same idempotency key in different tenants creates different jobs."""
    key = "shared-key"
    job_a = queue.enqueue(scope=org_a["scope"], job_type="invoice_document", payload={"text": "a"}, idempotency_key=key)
    job_b = queue.enqueue(scope=org_b["scope"], job_type="invoice_document", payload={"text": "b"}, idempotency_key=key)
    assert job_a.job_id != job_b.job_id


# --------------------------------------------------------------------------
# External action ledger — prevents duplicate external effects
# --------------------------------------------------------------------------

def test_external_action_record_is_idempotent(repos, org_a):
    """Recording an action with the same key returns the existing record."""
    scope = org_a["scope"]
    key = "erp-submit-001"
    action1 = repos.external_actions.record(
        scope=scope,
        action_type="erp_invoice_submit",
        idempotency_key=key,
        target_id="ERP-INV-001",
        result={"erp_id": "ERP-INV-001", "status": "submitted"},
    )
    action2 = repos.external_actions.record(
        scope=scope,
        action_type="erp_invoice_submit",
        idempotency_key=key,
        target_id="ERP-INV-001",
        result={"erp_id": "ERP-INV-001", "status": "submitted"},
    )
    assert action1.action_id == action2.action_id


def test_external_action_find_by_key(repos, org_a):
    """find_by_key returns the existing action or None."""
    scope = org_a["scope"]
    assert repos.external_actions.find_by_key(scope=scope, action_type="erp_invoice_submit", idempotency_key="nope") is None
    repos.external_actions.record(
        scope=scope,
        action_type="erp_invoice_submit",
        idempotency_key="key-1",
        result={"status": "ok"},
    )
    found = repos.external_actions.find_by_key(scope=scope, action_type="erp_invoice_submit", idempotency_key="key-1")
    assert found is not None
    assert found.idempotency_key == "key-1"


def test_external_action_different_keys_create_different_records(repos, org_a):
    """Different idempotency keys create different action records."""
    scope = org_a["scope"]
    a1 = repos.external_actions.record(scope=scope, action_type="erp_invoice_submit", idempotency_key="k1", result={"r": 1})
    a2 = repos.external_actions.record(scope=scope, action_type="erp_invoice_submit", idempotency_key="k2", result={"r": 2})
    assert a1.action_id != a2.action_id
    assert len(repos.external_actions.list(scope=scope)) == 2


def test_external_action_is_tenant_isolated(repos, org_a, org_b):
    """Org B cannot see org A's external actions."""
    scope_a = org_a["scope"]
    scope_b = org_b["scope"]
    repos.external_actions.record(scope=scope_a, action_type="erp_invoice_submit", idempotency_key="k1", result={"r": 1})
    # Org B sees zero actions.
    assert repos.external_actions.list(scope=scope_b) == []
    # Org B can use the same key without conflict.
    action_b = repos.external_actions.record(scope=scope_b, action_type="erp_invoice_submit", idempotency_key="k1", result={"r": 2})
    assert action_b is not None


def test_external_action_request_hash_is_stored(repos, org_a):
    """The request hash is stored for audit trail."""
    scope = org_a["scope"]
    action = repos.external_actions.record(
        scope=scope,
        action_type="erp_invoice_submit",
        idempotency_key="k1",
        request_payload={"invoice_id": "INV-001", "amount": 100},
        result={"status": "ok"},
    )
    assert action.request_hash is not None
    assert len(action.request_hash) == 64  # SHA-256 hex


# --------------------------------------------------------------------------
# External action ledger — DELETE is denied, UPDATE is allowed (state machine)
# --------------------------------------------------------------------------

def test_app_role_cannot_delete_external_actions(repos, org_a):
    """The app role must not be able to DELETE external_actions.

    v0.5.0-rc2: external_actions is now a state machine that allows controlled
    UPDATEs for state transitions. DELETE remains denied — external actions are
    permanent records.
    """
    scope = org_a["scope"]
    action = repos.external_actions.record(
        scope=scope, action_type="erp_invoice_submit", idempotency_key="k1", result={"r": 1}
    )
    dsn = os.getenv("TEST_DATABASE_URL") or os.getenv("APP_DATABASE_URL") or (
        "postgresql://construction_app:construction_app@localhost:5432/construction_ai"
    )
    with psycopg.connect(dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.organization_id', %s, true)", (str(scope.organization_id),))
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("DELETE FROM external_actions WHERE action_id = %s", (action.action_id,))
