"""v0.4.4 — retry and dead-letter (item 24).

A job that fails is retried up to max_attempts times. After max_attempts, it
moves to dead_letter status — a terminal state that requires human intervention.
The attempt_count tracks how many times the job has been claimed.
"""
from __future__ import annotations

import pytest

from construction_ai.jobs.queue import DEAD_LETTER, FAILED, QUEUED, JobQueue


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


@pytest.fixture()
def queue(repos):
    return JobQueue(repos, FakeRedis(), "test:jobs", consumer="test-worker")


# --------------------------------------------------------------------------
# Retry — a failed job is requeued up to max_attempts times
# --------------------------------------------------------------------------

def test_fail_requeues_when_under_max_attempts(queue, org_a):
    """A job that fails on attempt 1 is requeued for retry."""
    job = queue.enqueue(scope=org_a["scope"], job_type="test", payload={})
    queue.relay_outbox(limit=10)
    claimed = queue.reserve(timeout=0)
    assert claimed.attempt_count == 1
    queue.fail(claimed, "transient error")
    record = queue.get(scope=org_a["scope"], job_id=job.job_id)
    assert record.status == QUEUED
    assert record.error == "transient error"
    assert record.attempt_count == 1  # attempt_count doesn't reset on requeue


def test_fail_pushes_new_stream_message_for_retry(queue, org_a):
    """A retried job gets a new stream message so a worker can pick it up."""
    queue.enqueue(scope=org_a["scope"], job_type="test", payload={})
    queue.relay_outbox(limit=10)
    claimed = queue.reserve(timeout=0)
    initial_stream_len = len(queue.redis.streams["test:jobs"])
    queue.fail(claimed, "transient error")
    # A new stream message was added for the retry.
    assert len(queue.redis.streams["test:jobs"]) == initial_stream_len + 1


def test_retry_increments_attempt_count(queue, org_a):
    """Each claim increments attempt_count. After max_attempts, dead-letter."""
    job = queue.enqueue(scope=org_a["scope"], job_type="test", payload={})
    queue.relay_outbox(limit=10)

    # Attempt 1
    claimed = queue.reserve(timeout=0)
    assert claimed.attempt_count == 1
    queue.fail(claimed, "error 1")

    # Attempt 2
    queue.relay_outbox(limit=10)  # relay the retry message
    claimed2 = queue.reserve(timeout=0)
    assert claimed2.attempt_count == 2
    queue.fail(claimed2, "error 2")

    # Attempt 3 (max_attempts default = 3)
    queue.relay_outbox(limit=10)
    claimed3 = queue.reserve(timeout=0)
    assert claimed3.attempt_count == 3
    queue.fail(claimed3, "error 3")

    # Should be dead-lettered now
    record = queue.get(scope=org_a["scope"], job_id=job.job_id)
    assert record.status == DEAD_LETTER


# --------------------------------------------------------------------------
# Dead-letter — after max_attempts, the job is terminal
# --------------------------------------------------------------------------

def test_dead_letter_after_max_attempts(queue, org_a):
    """A job that exceeds max_attempts is dead-lettered, not retried."""
    job = queue.enqueue(scope=org_a["scope"], job_type="test", payload={})
    queue.relay_outbox(limit=10)

    for attempt in range(job.max_attempts):
        claimed = queue.reserve(timeout=0)
        assert claimed is not None
        queue.fail(claimed, f"error attempt {attempt + 1}")
        if attempt < job.max_attempts - 1:
            queue.relay_outbox(limit=10)

    record = queue.get(scope=org_a["scope"], job_id=job.job_id)
    assert record.status == DEAD_LETTER
    assert record.attempt_count == job.max_attempts
    assert "error attempt" in record.error


def test_dead_letter_does_not_retry(queue, org_a):
    """A dead-lettered job is not retried — no new stream message."""
    job = queue.enqueue(scope=org_a["scope"], job_type="test", payload={})
    queue.relay_outbox(limit=10)

    for _ in range(job.max_attempts):
        claimed = queue.reserve(timeout=0)
        queue.fail(claimed, "error")
        queue.relay_outbox(limit=10)

    record = queue.get(scope=org_a["scope"], job_id=job.job_id)
    assert record.status == DEAD_LETTER
    stream_len = len(queue.redis.streams["test:jobs"])
    # No new messages should be added for a dead-lettered job.
    # (fail already acked and didn't push a new message)
    # The relay shouldn't find anything new to push.
    queue.relay_outbox(limit=10)
    assert len(queue.redis.streams["test:jobs"]) == stream_len


def test_dead_letter_status_is_terminal(queue, org_a):
    """A dead-lettered job cannot be claimed or requeued."""
    job = queue.enqueue(scope=org_a["scope"], job_type="test", payload={})
    queue.relay_outbox(limit=10)

    for _ in range(job.max_attempts):
        claimed = queue.reserve(timeout=0)
        queue.fail(claimed, "error")
        queue.relay_outbox(limit=10)

    # The job is dead-lettered — claim returns None (status is not 'queued').
    from construction_ai.persistence.db import Scope
    result = queue.repos.jobs.claim(scope=Scope(job.organization_id), job_id=job.job_id)
    assert result is None


# --------------------------------------------------------------------------
# Repository methods
# --------------------------------------------------------------------------

def test_requeue_sets_status_to_queued(repos, org_a):
    """requeue resets a failed job to queued for retry."""
    scope = org_a["scope"]
    job = repos.jobs.create(scope=scope, job_type="test", payload={})
    repos.jobs.claim(scope=scope, job_id=job.job_id, claimed_by="w1")
    repos.jobs.fail(scope=scope, job_id=job.job_id, error="something")
    assert repos.jobs.get(scope=scope, job_id=job.job_id).status == FAILED
    repos.jobs.requeue(scope=scope, job_id=job.job_id, error="retry needed")
    record = repos.jobs.get(scope=scope, job_id=job.job_id)
    assert record.status == QUEUED
    assert record.error == "retry needed"
    assert record.lease_expires_at is None
    assert record.claimed_by is None


def test_dead_letter_sets_status_to_dead_letter(repos, org_a):
    """dead_letter moves a job to terminal dead_letter status."""
    scope = org_a["scope"]
    job = repos.jobs.create(scope=scope, job_type="test", payload={})
    repos.jobs.dead_letter(scope=scope, job_id=job.job_id, error="exhausted retries")
    record = repos.jobs.get(scope=scope, job_id=job.job_id)
    assert record.status == DEAD_LETTER
    assert record.error == "exhausted retries"


# --------------------------------------------------------------------------
# Worker integration — the worker retries and dead-letters automatically
# --------------------------------------------------------------------------

def test_worker_retries_failed_job(queue, org_a, monkeypatch):
    """The worker retries a failed job up to max_attempts times."""
    from apps.worker.main import run as run_worker
    from construction_ai.jobs import handlers

    call_count = [0]

    def flaky_handler(repos, job):
        call_count[0] += 1
        if call_count[0] < 3:
            raise ValueError("transient")
        return {"status": "ok"}

    monkeypatch.setitem(handlers.HANDLERS, "test_flaky", flaky_handler)
    job = queue.enqueue(scope=org_a["scope"], job_type="test_flaky", payload={})

    # Run worker cycles until the job succeeds or dead-letters.
    for _ in range(5):
        record = queue.get(scope=org_a["scope"], job_id=job.job_id)
        if record.status in ("completed", "dead_letter"):
            break
        run_worker(queue, once=True, timeout=0)

    record = queue.get(scope=org_a["scope"], job_id=job.job_id)
    assert record.status == "completed"
    assert call_count[0] == 3  # failed twice, succeeded on third


def test_worker_dead_letters_after_max_attempts(queue, org_a, monkeypatch):
    """The worker dead-letters a job that always fails."""
    from apps.worker.main import run as run_worker
    from construction_ai.jobs import handlers

    def always_fail(repos, job):
        raise ValueError("permanent failure")

    monkeypatch.setitem(handlers.HANDLERS, "test_permanent", always_fail)
    job = queue.enqueue(scope=org_a["scope"], job_type="test_permanent", payload={})

    for _ in range(5):
        record = queue.get(scope=org_a["scope"], job_id=job.job_id)
        if record.status in ("completed", "dead_letter"):
            break
        run_worker(queue, once=True, timeout=0)

    record = queue.get(scope=org_a["scope"], job_id=job.job_id)
    assert record.status == DEAD_LETTER
    assert record.attempt_count == record.max_attempts
