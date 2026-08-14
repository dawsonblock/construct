"""Redis-backed job queue over the scoped `jobs` table.

Redis carries `"<organization_id>:<job_id>"` and nothing else. The scope a worker
runs under comes from the job row, which was written under the enqueuing
request's authenticated scope — never from the job payload, and never from
anything a caller supplied.
"""
from __future__ import annotations

import os
from uuid import UUID

from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories.jobs import COMPLETED, FAILED, QUEUED, RUNNING, Job

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
        job = self.repos.jobs.create(scope=scope, job_type=job_type, payload=payload)
        self.redis.rpush(self.queue_name, self._token(job))
        return job

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
