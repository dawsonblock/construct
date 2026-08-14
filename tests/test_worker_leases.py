"""v0.4.4 — Redis Streams and worker leases (item 23).

Verifies that:
1. The queue uses Redis Streams (XADD/XREADGROUP/XACK) instead of lists.
2. Worker leases prevent a crashed worker from permanently stalling a job.
3. Expired leases are reclaimed and the job goes back to queued for retry.
4. The consumer group provides at-least-once delivery (messages stay in the
   pending entries list until XACK'd).
"""
from __future__ import annotations


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
        results = []
        for stream_name, start_id in streams.items():
            if stream_name not in self.streams:
                continue
            stream = self.streams[stream_name]
            grp = self.groups.get(stream_name, {}).get(group)
            if grp is None:
                continue
            grp["consumers"].setdefault(consumer, [])
            delivered = []
            for msg_id, fields in stream:
                if start_id == ">":
                    if msg_id in grp["pending"]:
                        continue
                    delivered.append((msg_id, fields))
                    grp["pending"][msg_id] = consumer
                    grp["consumers"][consumer].append(msg_id)
                    if len(delivered) >= count:
                        break
            if delivered:
                results.append((stream_name, delivered))
        return results if results else []

    def xack(self, stream, group, *msg_ids):
        grp = self.groups.get(stream, {}).get(group)
        if grp is None:
            return 0
        acked = 0
        for mid in msg_ids:
            if mid in grp["pending"]:
                del grp["pending"][mid]
                acked += 1
        return acked

    def xlen(self, stream):
        return len(self.streams.get(stream, []))

    def xpending(self, stream, group):
        grp = self.groups.get(stream, {}).get(group)
        if grp is None:
            return 0
        return len(grp["pending"])

    # Legacy list ops for compatibility
    def rpush(self, name, value):
        pass
    def blpop(self, name, timeout=0):
        return None
    def llen(self, name):
        return 0


@pytest.fixture()
def queue(repos):
    return JobQueue(repos, FakeRedis(), "test:jobs", consumer="test-worker")


# --------------------------------------------------------------------------
# Redis Streams — the queue uses XADD/XREADGROUP/XACK
# --------------------------------------------------------------------------

def test_relay_pushes_to_stream_not_list(queue, org_a):
    """The relay pushes to a Redis Stream via XADD, not a list via RPUSH."""
    queue.enqueue(scope=org_a["scope"], job_type="invoice_document", payload={"text": "hello"})
    queue.relay_outbox(limit=10)
    # The stream has one entry.
    assert len(queue.redis.streams["test:jobs"]) == 1
    msg_id, fields = queue.redis.streams["test:jobs"][0]
    assert "token" in fields


def test_reserve_reads_from_consumer_group(queue, org_a):
    """reserve reads from the stream consumer group, not a list."""
    job = queue.enqueue(scope=org_a["scope"], job_type="invoice_document", payload={"text": "hello"})
    queue.relay_outbox(limit=10)
    # The stream has the entry, but it's not yet delivered to any consumer.
    assert queue.redis.xpending("test:jobs", "workers") == 0
    reserved = queue.reserve(timeout=0)
    assert reserved is not None
    assert reserved.job_id == job.job_id
    # After reserve, the message is in the pending entries list (PEL).
    assert queue.redis.xpending("test:jobs", "workers") == 1


def test_complete_acks_the_stream_message(queue, org_a):
    """complete XACKs the stream message, removing it from the PEL."""
    queue.enqueue(scope=org_a["scope"], job_type="invoice_document", payload={"text": "hello"})
    queue.relay_outbox(limit=10)
    reserved = queue.reserve(timeout=0)
    assert queue.redis.xpending("test:jobs", "workers") == 1
    queue.complete(reserved, {"status": "completed"})
    assert queue.redis.xpending("test:jobs", "workers") == 0


def test_fail_acks_the_stream_message(queue, org_a):
    """fail also XACKs — the message is removed from the PEL after processing."""
    queue.enqueue(scope=org_a["scope"], job_type="invoice_document", payload={"text": "hello"})
    queue.relay_outbox(limit=10)
    reserved = queue.reserve(timeout=0)
    assert queue.redis.xpending("test:jobs", "workers") == 1
    queue.fail(reserved, "something went wrong")
    assert queue.redis.xpending("test:jobs", "workers") == 0


# --------------------------------------------------------------------------
# Worker leases — crashed workers don't stall jobs
# --------------------------------------------------------------------------

def test_claim_sets_lease_expiry(repos, org_a):
    """claim sets lease_expires_at and increments attempt_count."""
    job = repos.jobs.create(scope=org_a["scope"], job_type="test", payload={})
    claimed = repos.jobs.claim(scope=org_a["scope"], job_id=job.job_id, lease_seconds=60, claimed_by="w1")
    assert claimed.status == "running"
    assert claimed.lease_expires_at is not None
    assert claimed.claimed_by == "w1"
    assert claimed.attempt_count == 1


def test_reclaim_expired_jobs_resets_to_queued(repos, org_a):
    """Expired-lease jobs go back to queued for retry."""
    scope = org_a["scope"]
    job = repos.jobs.create(scope=scope, job_type="test", payload={})
    # Claim with a 1-second lease.
    repos.jobs.claim(scope=scope, job_id=job.job_id, lease_seconds=1, claimed_by="w1")
    # Manually expire the lease by setting lease_expires_at in the past.
    with repos.db.scoped(scope) as cur:
        cur.execute(
            "UPDATE jobs SET lease_expires_at = now() - interval '1 second' "
            "WHERE organization_id = %s AND job_id = %s",
            (scope.organization_id, job.job_id),
        )
    # Reclaim expired jobs.
    reclaimed = repos.jobs.reclaim_expired_jobs(scope=scope)
    assert len(reclaimed) == 1
    assert reclaimed[0][1] == job.job_id
    # The job is back to queued.
    refreshed = repos.jobs.get(scope=scope, job_id=job.job_id)
    assert refreshed.status == "queued"
    assert refreshed.lease_expires_at is None


def test_reclaim_does_not_touch_active_leases(repos, org_a):
    """Jobs with unexpired leases are not reclaimed."""
    scope = org_a["scope"]
    job = repos.jobs.create(scope=scope, job_type="test", payload={})
    repos.jobs.claim(scope=scope, job_id=job.job_id, lease_seconds=3600, claimed_by="w1")
    reclaimed = repos.jobs.reclaim_expired_jobs(scope=scope)
    assert len(reclaimed) == 0
    refreshed = repos.jobs.get(scope=scope, job_id=job.job_id)
    assert refreshed.status == "running"


def test_reclaim_does_not_touch_completed_jobs(repos, org_a):
    """Completed jobs are not affected by reclaim."""
    scope = org_a["scope"]
    job = repos.jobs.create(scope=scope, job_type="test", payload={})
    repos.jobs.claim(scope=scope, job_id=job.job_id, lease_seconds=3600, claimed_by="w1")
    repos.jobs.complete(scope=scope, job_id=job.job_id, result={"status": "done"})
    reclaimed = repos.jobs.reclaim_expired_jobs(scope=scope)
    assert len(reclaimed) == 0


def test_attempt_count_increments_on_reclaim_and_retry(repos, org_a):
    """When a crashed worker's job is reclaimed and retried, attempt_count increments."""
    scope = org_a["scope"]
    job = repos.jobs.create(scope=scope, job_type="test", payload={})
    # First claim (attempt 1).
    claimed = repos.jobs.claim(scope=scope, job_id=job.job_id, lease_seconds=1, claimed_by="w1")
    assert claimed.attempt_count == 1
    # Expire the lease.
    with repos.db.scoped(scope) as cur:
        cur.execute(
            "UPDATE jobs SET lease_expires_at = now() - interval '1 second' "
            "WHERE organization_id = %s AND job_id = %s",
            (scope.organization_id, job.job_id),
        )
    repos.jobs.reclaim_expired_jobs(scope=scope)
    # Second claim (attempt 2).
    claimed2 = repos.jobs.claim(scope=scope, job_id=job.job_id, lease_seconds=60, claimed_by="w2")
    assert claimed2.attempt_count == 2


# --------------------------------------------------------------------------
# Worker integration — the worker reclaims expired jobs before reserving
# --------------------------------------------------------------------------

def test_worker_reclaims_expired_job(queue, repos, org_a):
    """A crashed worker's expired job is reclaimed and retried by the next worker cycle."""
    from apps.worker.main import run as run_worker

    scope = org_a["scope"]
    job = queue.enqueue(scope=scope, job_type="invoice_document", payload={"text": "hello"})
    queue.relay_outbox(limit=10)
    # Simulate a worker claiming the job then crashing (lease expires).
    reserved = queue.reserve(timeout=0)
    assert reserved is not None
    with repos.db.scoped(scope) as cur:
        cur.execute(
            "UPDATE jobs SET lease_expires_at = now() - interval '1 second' "
            "WHERE organization_id = %s AND job_id = %s",
            (scope.organization_id, job.job_id),
        )
    # The next worker cycle reclaims the expired lease and processes the job.
    run_worker(queue, once=True, timeout=0)
    record = queue.get(scope=scope, job_id=job.job_id)
    assert record.status in ("completed", "failed")
