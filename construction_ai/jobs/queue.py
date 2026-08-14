"""Redis Streams-backed job queue over the scoped `jobs` table.

Redis carries `"<organization_id>:<job_id>"` and nothing else. The scope a worker
runs under comes from the job row, which was written under the enqueuing
request's authenticated scope — never from the job payload, and never from
anything a caller supplied.

v0.4.4: enqueue uses the transactional outbox pattern. The job row and an
outbox row are written in the same database transaction. A relay reads
unpublished outbox rows and pushes them to a Redis Stream. Workers consume via
a consumer group, which provides:
- At-least-once delivery: messages stay in the pending entries list (PEL)
  until XACK'd.
- Lease recovery: if a worker crashes, its unacked message can be XCLAIM'd by
  another worker after the lease expires.
- Worker leases: the job row's lease_expires_at tracks the DB-side lease; the
  Redis PEL tracks the stream-side delivery. Both must be recovered.
"""
from __future__ import annotations

import logging
import os
from uuid import UUID

from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories.jobs import COMPLETED, DEAD_LETTER, FAILED, QUEUED, RUNNING, Job

log = logging.getLogger("job_queue")

__all__ = ["COMPLETED", "DEAD_LETTER", "FAILED", "QUEUED", "RUNNING", "Job", "JobQueue"]

DEFAULT_LEASE_SECONDS = 300


class JobQueue:
    def __init__(
        self,
        repositories,
        redis_client,
        stream_name: str = "construction_ai:jobs",
        group: str = "workers",
        consumer: str = "worker-1",
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ):
        self.repos = repositories
        self.redis = redis_client
        self.stream_name = stream_name
        self.group = group
        self.consumer = consumer
        self.lease_seconds = lease_seconds
        self._group_ensured = False
        self._current_msg_id: str | None = None  # set by reserve, used by complete/fail

    @classmethod
    def from_env(cls, repositories):
        import redis

        url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        return cls(
            repositories,
            redis.Redis.from_url(url, decode_responses=True),
            os.getenv("JOB_QUEUE", "construction_ai:jobs"),
            consumer=os.getenv("WORKER_ID", "worker-1"),
        )

    @staticmethod
    def _token(job: Job) -> str:
        return f"{job.organization_id}:{job.job_id}"

    def _ensure_group(self):
        """Create the consumer group if it doesn't exist. Idempotent."""
        if self._group_ensured:
            return
        try:
            self.redis.xgroup_create(self.stream_name, self.group, id="0", mkstream=True)
        except Exception as exc:
            # BUSYGROUP means the group already exists — that's fine.
            if "BUSYGROUP" not in str(exc):
                raise
        self._group_ensured = True

    def enqueue(self, *, scope: Scope, job_type: str, payload: dict) -> Job:
        """Write the job row and an outbox row in one transaction.

        The relay (relay_outbox) pushes to the Redis Stream after the
        transaction commits.
        """
        with self.repos.db.transaction():
            job = self.repos.jobs.create(scope=scope, job_type=job_type, payload=payload)
            self.repos.outbox.create(scope=scope, job_id=job.job_id)
        return job

    def relay_outbox(self, *, scope: Scope | None = None, limit: int = 100) -> int:
        """Push unpublished outbox rows to the Redis Stream and mark published."""
        if scope is not None:
            events = self.repos.outbox.fetch_unpublished(scope=scope, limit=limit)
        else:
            events = self._fetch_all_unpublished(limit=limit)

        if not events:
            return 0

        pushed = 0
        for event in events:
            token = f"{event.organization_id}:{event.job_id}"
            self.redis.xadd(self.stream_name, {"token": token})
            pushed += 1

        if scope is not None:
            self.repos.outbox.mark_published(scope=scope, outbox_ids=[e.outbox_id for e in events])
        else:
            for event in events:
                org_scope = Scope(event.organization_id)
                self.repos.outbox.mark_published(scope=org_scope, outbox_ids=[event.outbox_id])

        return pushed

    def _fetch_all_unpublished(self, *, limit: int = 100) -> list:
        from construction_ai.persistence.repositories.outbox import OutboxEvent

        with self.repos.db.unscoped_auth() as cur:
            cur.execute(
                """SELECT outbox_id, organization_id, job_id, event_type, payload,
                          created_at, published_at
                   FROM outbox_events
                   WHERE published_at IS NULL
                   ORDER BY outbox_id LIMIT %s""",
                (limit,),
            )
            rows = cur.fetchall()
        return [
            OutboxEvent(
                outbox_id=r[0],
                organization_id=r[1],
                job_id=r[2],
                event_type=r[3],
                payload=r[4] or {},
                created_at=r[5],
                published_at=r[6],
            )
            for r in rows
        ]

    def reserve(self, timeout: int = 5) -> Job | None:
        """Next job from the stream consumer group, moved queued → running."""
        self._ensure_group()
        # Reclaim expired leases first — a crashed worker's job goes back to
        # the queue and can be picked up by this worker.
        self._reclaim_all_expired()

        # Read new messages from the stream.
        messages = self.redis.xreadgroup(
            self.group,
            self.consumer,
            {self.stream_name: ">"},
            count=1,
            block=timeout * 1000,
        )
        if not messages:
            return None

        # messages is [(stream_name, [(id, {field: value}), ...])]
        stream_name, entries = messages[0]
        msg_id, fields = entries[0]
        token = fields.get("token", "")
        organization_id, _, job_id = token.partition(":")
        try:
            scope = Scope(UUID(organization_id))
            job = self.repos.jobs.claim(
                scope=scope,
                job_id=UUID(job_id),
                lease_seconds=self.lease_seconds,
                claimed_by=self.consumer,
            )
            if job is None:
                # Already claimed or completed — ack the stream message to
                # remove it from the PEL.
                self.redis.xack(self.stream_name, self.group, msg_id)
                self._current_msg_id = None
            else:
                self._current_msg_id = msg_id
            return job
        except ValueError:
            # Malformed token — ack and drop.
            self.redis.xack(self.stream_name, self.group, msg_id)
            self._current_msg_id = None
            return None

    def _reclaim_all_expired(self):
        """Reclaim expired-lease jobs across all organizations and re-push to stream.

        Uses the SECURITY DEFINER function find_expired_leases() to find expired
        jobs across all tenants (bypassing RLS — the worker needs to find expired
        jobs regardless of tenant). Reclaimed jobs are re-pushed to the Redis
        Stream so a worker can pick them up.
        """
        with self.repos.db.unscoped_auth() as cur:
            cur.execute("SELECT organization_id, job_id FROM find_expired_leases()")
            expired = cur.fetchall()
        if not expired:
            return
        # Group by organization to reclaim efficiently.
        by_org: dict = {}
        for org_id, job_id in expired:
            by_org.setdefault(org_id, []).append(job_id)
        for org_id, _job_ids in by_org.items():
            try:
                scope = Scope(org_id)
                reclaimed = self.repos.jobs.reclaim_expired_jobs(scope=scope)
                for org_id_, job_id in reclaimed:
                    token = f"{org_id_}:{job_id}"
                    self.redis.xadd(self.stream_name, {"token": token})
            except Exception:
                log.exception("failed to reclaim expired jobs for org %s", org_id)

    def ack(self, job: Job, msg_id: str | None = None) -> None:
        """Acknowledge the stream message after successful processing."""
        mid = msg_id or self._current_msg_id
        if mid:
            self.redis.xack(self.stream_name, self.group, mid)
        self._current_msg_id = None

    def complete(self, job: Job, result: dict, msg_id: str | None = None) -> None:
        self.repos.jobs.complete(scope=Scope(job.organization_id), job_id=job.job_id, result=result)
        self.ack(job, msg_id)

    def fail(self, job: Job, error: str, msg_id: str | None = None) -> None:
        self.repos.jobs.fail(scope=Scope(job.organization_id), job_id=job.job_id, error=error)
        self.ack(job, msg_id)

    def get(self, *, scope: Scope, job_id: UUID) -> Job | None:
        return self.repos.jobs.get(scope=scope, job_id=job_id)

    def depth(self) -> int:
        """Approximate stream length (includes acked but not trimmed entries)."""
        try:
            info = self.redis.xlen(self.stream_name)
            return int(info)
        except Exception:
            return 0
