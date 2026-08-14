"""v0.4.4 — transactional outbox for reliable job dispatch (item 22).

The outbox pattern ensures the Redis push happens if and only if the DB
transaction commits. If Redis is down, jobs are still enqueued — they sit in
the outbox until the relay can push them. If the DB transaction rolls back,
no outbox row exists, so no phantom job is pushed.
"""
from __future__ import annotations

import os

import pytest

from construction_ai.jobs.queue import JobQueue


class FakeRedis:
    def __init__(self):
        self.lists: dict[str, list[str]] = {}

    def rpush(self, name, value):
        self.lists.setdefault(name, []).append(value)

    def blpop(self, name, timeout=0):
        items = self.lists.get(name) or []
        return (name, items.pop(0)) if items else None

    def llen(self, name):
        return len(self.lists.get(name) or [])


@pytest.fixture()
def queue(repos):
    return JobQueue(repos, FakeRedis(), "test:jobs")


# --------------------------------------------------------------------------
# Enqueue writes to outbox, not directly to Redis
# --------------------------------------------------------------------------

def test_enqueue_does_not_push_to_redis_directly(queue, org_a):
    """enqueue writes the job and outbox row, but does not push to Redis."""
    job = queue.enqueue(scope=org_a["scope"], job_type="invoice_document", payload={"text": "hello"})
    # Redis is empty — the relay hasn't run yet.
    assert queue.redis.lists.get("test:jobs", []) == []
    # But the job exists in the database.
    assert queue.get(scope=org_a["scope"], job_id=job.job_id) is not None


def test_relay_pushes_unpublished_outbox_to_redis(queue, org_a):
    """The relay reads unpublished outbox rows and pushes them to Redis."""
    job = queue.enqueue(scope=org_a["scope"], job_type="invoice_document", payload={"text": "hello"})
    assert queue.redis.lists.get("test:jobs", []) == []
    pushed = queue.relay_outbox(limit=10)
    assert pushed == 1
    assert queue.redis.lists["test:jobs"] == [f"{org_a['organization_id']}:{job.job_id}"]


def test_relay_is_idempotent(queue, org_a):
    """Running the relay twice does not double-push."""
    queue.enqueue(scope=org_a["scope"], job_type="invoice_document", payload={"text": "hello"})
    queue.relay_outbox(limit=10)
    pushed_again = queue.relay_outbox(limit=10)
    assert pushed_again == 0
    assert len(queue.redis.lists["test:jobs"]) == 1


def test_relay_handles_multiple_jobs(queue, org_a):
    """Multiple unpublished jobs are all relayed in one pass."""
    job1 = queue.enqueue(scope=org_a["scope"], job_type="invoice_document", payload={"n": 1})
    job2 = queue.enqueue(scope=org_a["scope"], job_type="invoice_document", payload={"n": 2})
    job3 = queue.enqueue(scope=org_a["scope"], job_type="invoice_document", payload={"n": 3})
    pushed = queue.relay_outbox(limit=10)
    assert pushed == 3
    tokens = queue.redis.lists["test:jobs"]
    assert len(tokens) == 3
    assert f"{org_a['organization_id']}:{job1.job_id}" in tokens
    assert f"{org_a['organization_id']}:{job2.job_id}" in tokens
    assert f"{org_a['organization_id']}:{job3.job_id}" in tokens


# --------------------------------------------------------------------------
# Transactional integrity — outbox commits with the job, or not at all
# --------------------------------------------------------------------------

def test_outbox_row_is_written_in_same_transaction_as_job(queue, org_a):
    """The outbox row and job row are in the same transaction."""
    job = queue.enqueue(scope=org_a["scope"], job_type="invoice_document", payload={"text": "hello"})
    # The outbox row exists.
    events = queue.repos.outbox.fetch_unpublished(scope=org_a["scope"], limit=10)
    assert len(events) == 1
    assert events[0].job_id == job.job_id
    assert events[0].published_at is None


def test_relay_marks_outbox_rows_as_published(queue, org_a):
    """After the relay pushes, outbox rows are marked published."""
    queue.enqueue(scope=org_a["scope"], job_type="invoice_document", payload={"text": "hello"})
    queue.relay_outbox(limit=10)
    events = queue.repos.outbox.fetch_unpublished(scope=org_a["scope"], limit=10)
    assert len(events) == 0  # all published


# --------------------------------------------------------------------------
# Worker integration — the relay runs before reserve in the worker loop
# --------------------------------------------------------------------------

def test_worker_relay_delivers_outbox_jobs(queue, org_a):
    """A job enqueued via the outbox is delivered to the worker by the relay."""
    from apps.worker.main import run as run_worker

    job = queue.enqueue(scope=org_a["scope"], job_type="invoice_document", payload={"text": "hello"})
    # Redis is empty — no direct push.
    assert queue.redis.lists.get("test:jobs", []) == []
    # The worker runs the relay then reserves.
    run_worker(queue, once=True, timeout=0)
    record = queue.get(scope=org_a["scope"], job_id=job.job_id)
    assert record.status in ("completed", "failed")  # the worker processed it


# --------------------------------------------------------------------------
# Redis failure resilience — enqueue still works when Redis is down
# --------------------------------------------------------------------------

class FailingRedis:
    """Simulates a Redis that's down."""
    def rpush(self, name, value):
        raise ConnectionError("Redis is down")
    def blpop(self, name, timeout=0):
        raise ConnectionError("Redis is down")
    def llen(self, name):
        raise ConnectionError("Redis is down")


def test_enqueue_works_without_redis(repos, org_a):
    """enqueue succeeds even when Redis is unreachable — the job sits in the outbox."""
    failing_queue = JobQueue(repos, FailingRedis(), "test:jobs")
    job = failing_queue.enqueue(scope=org_a["scope"], job_type="invoice_document", payload={"text": "hello"})
    assert job is not None
    assert job.status == "queued"
    # The outbox row exists — the relay will push it when Redis recovers.
    events = repos.outbox.fetch_unpublished(scope=org_a["scope"], limit=10)
    assert len(events) == 1


# --------------------------------------------------------------------------
# Outbox is append-only (no DELETE for the app role)
# --------------------------------------------------------------------------

def test_app_role_cannot_delete_outbox_rows(queue, org_a):
    """The app role must not be able to DELETE outbox rows."""
    import psycopg

    queue.enqueue(scope=org_a["scope"], job_type="invoice_document", payload={"text": "hello"})
    events = queue.repos.outbox.fetch_unpublished(scope=org_a["scope"], limit=10)
    assert len(events) == 1
    outbox_id = events[0].outbox_id

    dsn = os.getenv("TEST_DATABASE_URL") or os.getenv("APP_DATABASE_URL") or (
        "postgresql://construction_app:construction_app@localhost:5432/construction_ai"
    )
    with psycopg.connect(dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("DELETE FROM outbox_events WHERE outbox_id = %s", (outbox_id,))
