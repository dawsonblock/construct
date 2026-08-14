"""Jobs.

A job's tenant scope comes from its row, written under the enqueuing request's
authenticated scope. It is never read back out of the payload — a worker that
trusted `payload["organization_id"]` would let anyone who can enqueue a job
choose which tenant it runs against.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories.base import Repository

QUEUED = "queued"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
DEAD_LETTER = "dead_letter"

JOB_COLUMNS = (
    "organization_id, job_id, project_id, job_type, status, payload, result, error, "
    "created_at, updated_at, lease_expires_at, claimed_by, attempt_count, max_attempts"
)


@dataclass
class Job:
    job_id: UUID
    organization_id: UUID
    job_type: str
    payload: dict[str, Any]
    status: str = QUEUED
    project_id: UUID | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    lease_expires_at: Any = None  # datetime | None
    claimed_by: str | None = None
    attempt_count: int = 0
    max_attempts: int = 3

    @property
    def scope(self) -> Scope:
        """The only authority on which tenant this job runs against."""
        return Scope(self.organization_id, self.project_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": str(self.job_id),
            "job_type": self.job_type,
            "status": self.status,
            "project_id": str(self.project_id) if self.project_id else None,
            "payload": self.payload,
            "result": self.result,
            "error": self.error,
        }


def _to_job(row: dict[str, Any]) -> Job:
    return Job(
        job_id=row["job_id"],
        organization_id=row["organization_id"],
        job_type=row["job_type"],
        payload=row.get("payload") or {},
        status=row["status"],
        project_id=row.get("project_id"),
        result=row.get("result"),
        error=row.get("error"),
        lease_expires_at=row.get("lease_expires_at"),
        claimed_by=row.get("claimed_by"),
        attempt_count=row.get("attempt_count") or 0,
        max_attempts=row.get("max_attempts") or 3,
    )


class JobRepository(Repository):
    table = "jobs"
    id_column = "job_id"

    def create(self, *, scope: Scope, job_type: str, payload: dict[str, Any], created_by: str = "api") -> Job:
        from psycopg.types.json import Jsonb

        from construction_ai.persistence.serialization import dumps

        with self.db.scoped(scope) as cur:
            cur.execute(
                f"""INSERT INTO jobs(organization_id, project_id, job_type, payload, created_by)
                    VALUES(%s,%s,%s,%s,%s) RETURNING {JOB_COLUMNS}""",
                (scope.organization_id, scope.project_id, job_type, Jsonb(payload, dumps=dumps), created_by),
            )
            columns = [c.name for c in cur.description]
            return _to_job(dict(zip(columns, cur.fetchone(), strict=True)))

    def get(self, *, scope: Scope, job_id: UUID) -> Job | None:
        row = self.get_row(scope=scope, record_id=job_id, columns=JOB_COLUMNS)
        return _to_job(row) if row else None

    def claim(self, *, scope: Scope, job_id: UUID, lease_seconds: int = 300, claimed_by: str = "worker") -> Job | None:
        """queued → running, exactly once. Two workers cannot both win.

        Sets a lease: if the worker crashes, the lease expires and another
        worker can reclaim the job via reclaim_expired_jobs.
        """
        with self.db.scoped(scope) as cur:
            cur.execute(
                f"""UPDATE jobs SET status = 'running',
                    lease_expires_at = now() + %s::interval,
                    claimed_by = %s,
                    attempt_count = attempt_count + 1
                    WHERE organization_id = %s AND job_id = %s AND status = 'queued'
                    RETURNING {JOB_COLUMNS}""",
                (f"{lease_seconds} seconds", claimed_by, scope.organization_id, job_id),
            )
            row = cur.fetchone()
            if row is None:
                return None
            columns = [c.name for c in cur.description]
            return _to_job(dict(zip(columns, row, strict=True)))

    def reclaim_expired_jobs(self, *, scope: Scope) -> list[tuple[UUID, UUID]]:
        """Reset expired-lease jobs from running → queued so they can be retried.

        Returns a list of (organization_id, job_id) tuples for the reclaimed
        jobs. The queue re-pushes these to the Redis Stream so a worker can
        pick them up.
        """
        with self.db.scoped(scope) as cur:
            cur.execute(
                "UPDATE jobs SET status = 'queued', lease_expires_at = NULL, claimed_by = NULL "
                "WHERE organization_id = %s AND status = 'running' "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at < now() "
                "RETURNING organization_id, job_id",
                (scope.organization_id,),
            )
            return [(row[0], row[1]) for row in cur.fetchall()]

    def complete(self, *, scope: Scope, job_id: UUID, result: dict[str, Any]) -> None:
        from psycopg.types.json import Jsonb

        from construction_ai.persistence.serialization import dumps

        with self.db.scoped(scope) as cur:
            cur.execute(
                "UPDATE jobs SET status = 'completed', result = %s, error = NULL WHERE organization_id = %s AND job_id = %s",
                (Jsonb(result, dumps=dumps), scope.organization_id, job_id),
            )

    def fail(self, *, scope: Scope, job_id: UUID, error: str) -> None:
        with self.db.scoped(scope) as cur:
            cur.execute(
                "UPDATE jobs SET status = 'failed', error = %s WHERE organization_id = %s AND job_id = %s",
                (error, scope.organization_id, job_id),
            )

    def list(self, *, scope: Scope, status: str | None = None, limit: int = 100) -> list[Job]:
        clause, params = self._tenant_clause(scope)
        sql = f"SELECT {JOB_COLUMNS} FROM jobs WHERE {clause}"
        if status:
            sql += " AND status = %s"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        return [_to_job(row) for row in self._fetch_all(scope, sql, params)]
