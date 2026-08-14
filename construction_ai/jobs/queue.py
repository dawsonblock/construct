"""Redis-backed job queue over the scoped `jobs` table.

Redis carries `"<organization_id>:<job_id>"` and nothing else. The scope a worker
runs under comes from the job row, which was written under the enqueuing
request's authenticated scope — never from the job payload, and never from
anything a caller supplied.

v0.4.4: enqueue uses the transactional outbox pattern. The job row and an
outbox row are written in the same database transaction. A relay reads
unpublished outbox rows and pushes them to Redis. This ensures the Redis push
happens if and only if the DB transaction commits — no phantom jobs, no
stranded rows.
"""
from __future__ import annotations

import logging
import os
from uuid import UUID

from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories.jobs import COMPLETED, FAILED, QUEUED, RUNNING, Job

log = logging.getLogger("job_queue")

__all__ = ["COMPLETED", "FAILED", "QUEUED", "RUNNING", "Job", "JobQueue"]


class JobQueue:
    def __init__(self, repositories, redis_client, queue_name: str = "construction_ai:jobs"):
        self.repos = repositories
        self.redis = redis_client
        self.queue_name = queue_name

    @classmethod
    def from_env(cls, repositories):
        import redis

        url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        return cls(
            repositories,
            redis.Redis.from_url(url, decode_responses=True),
            os.getenv("JOB_QUEUE", "construction_ai:jobs"),
        )

    @staticmethod
    def _token(job: Job) -> str:
        return f"{job.organization_id}:{job.job_id}"

    def enqueue(self, *, scope: Scope, job_type: str, payload: dict) -> Job:
        """Write the job row and an outbox row in one transaction.

        The relay (relay_outbox) pushes to Redis after the transaction commits.
        If the caller is inside a Database.transaction(), the job and outbox
        rows commit with the rest of the business operation — no partial state.
        """
        with self.repos.db.transaction():
            job = self.repos.jobs.create(scope=scope, job_type=job_type, payload=payload)
            self.repos.outbox.create(scope=scope, job_id=job.job_id)
        return job

    def relay_outbox(self, *, scope: Scope | None = None, limit: int = 100) -> int:
        """Push unpublished outbox rows to Redis and mark them published.

        Returns the number of rows relayed. If scope is None, relays across all
        organizations (the relay runs unscoped with a superuser/owner connection
        in production; in tests it runs per-tenant).

        Idempotent: if the relay crashes after pushing to Redis but before
        marking published, it may push twice on recovery. The worker's claim
        (queued → running) is idempotent, so a double-push is safe.
        """
        if scope is not None:
            events = self.repos.outbox.fetch_unpublished(scope=scope, limit=limit)
        else:
            # Unscoped relay: iterate organizations that have unpublished rows.
            events = self._fetch_all_unpublished(limit=limit)

        if not events:
            return 0

        pushed = 0
        for event in events:
            token = f"{event.organization_id}:{event.job_id}"
            self.redis.rpush(self.queue_name, token)
            pushed += 1

        # Mark all as published. If this fails, the relay will re-push on the
        # next cycle — safe because claim is idempotent.
        if scope is not None:
            self.repos.outbox.mark_published(scope=scope, outbox_ids=[e.outbox_id for e in events])
        else:
            for event in events:
                org_scope = Scope(event.organization_id)
                self.repos.outbox.mark_published(scope=org_scope, outbox_ids=[event.outbox_id])

        return pushed

    def _fetch_all_unpublished(self, *, limit: int = 100) -> list:
        """Fetch unpublished outbox rows across all organizations (relay mode)."""
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
        """Next job, moved queued → running under its own tenant scope."""
        item = self.redis.blpop(self.queue_name, timeout=timeout)
        if not item:
            return None
        organization_id, _, job_id = item[1].partition(":")
        try:
            scope = Scope(UUID(organization_id))
            return self.repos.jobs.claim(scope=scope, job_id=UUID(job_id))
        except ValueError:
            # A malformed token cannot be attributed to a tenant, so there is no
            # job row to fail. Dropping it is the only safe move.
            return None

    def complete(self, job: Job, result: dict) -> None:
        self.repos.jobs.complete(scope=Scope(job.organization_id), job_id=job.job_id, result=result)

    def fail(self, job: Job, error: str) -> None:
        self.repos.jobs.fail(scope=Scope(job.organization_id), job_id=job.job_id, error=error)

    def get(self, *, scope: Scope, job_id: UUID) -> Job | None:
        return self.repos.jobs.get(scope=scope, job_id=job_id)

    def depth(self) -> int:
        return int(self.redis.llen(self.queue_name))
