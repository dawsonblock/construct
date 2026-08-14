"""Transactional outbox for reliable job dispatch.

The outbox row is written in the same database transaction as the job row.
A relay reads unpublished rows and pushes them to Redis, marking them
published. This ensures the Redis push happens if and only if the DB
transaction commits — no phantom jobs, no stranded rows.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories.base import Repository


@dataclass(frozen=True)
class OutboxEvent:
    outbox_id: int
    organization_id: UUID
    job_id: UUID
    event_type: str
    payload: dict[str, Any]
    created_at: datetime
    published_at: datetime | None


class OutboxRepository(Repository):
    table = "outbox_events"
    id_column = "outbox_id"

    def create(self, *, scope: Scope, job_id: UUID, event_type: str = "job_enqueue") -> OutboxEvent:
        """Insert an outbox row. Must be called within the same transaction as
        the job row creation so they commit or roll back together."""
        with self.db.scoped(scope) as cur:
            cur.execute(
                """INSERT INTO outbox_events(organization_id, job_id, event_type)
                   VALUES(%s, %s, %s) RETURNING outbox_id, created_at""",
                (scope.organization_id, job_id, event_type),
            )
            row = cur.fetchone()
        return OutboxEvent(
            outbox_id=row[0],
            organization_id=scope.organization_id,
            job_id=job_id,
            event_type=event_type,
            payload={},
            created_at=row[1],
            published_at=None,
        )

    def fetch_unpublished(self, *, scope: Scope, limit: int = 100) -> list[OutboxEvent]:
        """Read unpublished outbox rows in creation order. The relay pushes
        these to Redis and then marks them published."""
        rows = self._fetch_all(
            scope,
            """SELECT outbox_id, organization_id, job_id, event_type, payload,
                      created_at, published_at
               FROM outbox_events
               WHERE organization_id = %s AND published_at IS NULL
               ORDER BY outbox_id LIMIT %s""",
            [scope.organization_id, limit],
        )
        return [
            OutboxEvent(
                outbox_id=r["outbox_id"],
                organization_id=r["organization_id"],
                job_id=r["job_id"],
                event_type=r["event_type"],
                payload=r.get("payload") or {},
                created_at=r["created_at"],
                published_at=r.get("published_at"),
            )
            for r in rows
        ]

    def mark_published(self, *, scope: Scope, outbox_ids: list[int]) -> None:
        """Mark outbox rows as published after successful Redis push."""
        if not outbox_ids:
            return
        with self.db.scoped(scope) as cur:
            cur.execute(
                "UPDATE outbox_events SET published_at = now() "
                "WHERE organization_id = %s AND outbox_id = ANY(%s)",
                (scope.organization_id, outbox_ids),
            )
