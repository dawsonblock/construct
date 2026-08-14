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

JOB_COLUMNS = "organization_id, job_id, project_id, job_type, status, payload, result, error, created_at, updated_at"


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

    def claim(self, *, scope: Scope, job_id: UUID) -> Job | None:
        """queued → running, exactly once. Two workers cannot both win."""
        with self.db.scoped(scope) as cur:
            cur.execute(
                f"""UPDATE jobs SET status = 'running'
                    WHERE organization_id = %s AND job_id = %s AND status = 'queued'
                    RETURNING {JOB_COLUMNS}""",
                (scope.organization_id, job_id),
            )
            row = cur.fetchone()
            if row is None:
                return None
            columns = [c.name for c in cur.description]
            return _to_job(dict(zip(columns, row, strict=True)))

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
