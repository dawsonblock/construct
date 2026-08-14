"""Hash-chained audit, one chain per organization.

    H_i = H(H_{i-1} ‖ event_type ‖ actor ‖ organization ‖ project ‖ object_type
            ‖ object_id ‖ canonical_payload ‖ t_i)

The canonical serialization is `serialization.dumps` — the same function that
writes the payload column. A separate encoder for hashing produced a false
tamper report once; there is one encoder.

The chain is per organization on purpose: a shared global sequence lets one
tenant infer another's activity rate from the gaps in its own.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories.base import Repository
from construction_ai.persistence.serialization import dumps

CHAIN_SEPARATOR = "␟"  # unit separator: cannot appear in a UUID, hash or type name


@dataclass(frozen=True)
class AuditEvent:
    sequence: int
    event_type: str
    actor: str
    object_type: str
    object_id: UUID | None
    occurred_at: datetime
    entry_hash: str
    prev_hash: str | None
    project_id: UUID | None
    payload: dict[str, Any]


def chain_hash(
    *,
    prev_hash: str | None,
    event_type: str,
    actor: str,
    organization_id: UUID,
    project_id: UUID | None,
    object_type: str,
    object_id: UUID | None,
    payload: Any,
    occurred_at: datetime,
) -> str:
    material = CHAIN_SEPARATOR.join(
        [
            prev_hash or "",
            event_type,
            actor,
            str(organization_id),
            str(project_id) if project_id else "",
            object_type,
            str(object_id) if object_id else "",
            dumps(payload),
            occurred_at.isoformat(),
        ]
    )
    return hashlib.sha256(material.encode()).hexdigest()


class AuditRepository(Repository):
    table = "audit_events"
    id_column = "audit_event_id"

    def append(
        self,
        *,
        scope: Scope,
        event_type: str,
        actor: str,
        object_type: str,
        object_id: UUID | None = None,
        payload: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> AuditEvent:
        from psycopg.types.json import Jsonb

        payload = payload or {}
        occurred_at = occurred_at or datetime.now(timezone.utc)
        with self.db.scoped(scope) as cur:
            # Serialize appends within this organization's chain. Without it two
            # concurrent writers can read the same head and race on `sequence`;
            # the unique constraint would reject one, losing the event.
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(scope.organization_id),))
            cur.execute(
                "SELECT sequence, entry_hash FROM audit_events WHERE organization_id = %s ORDER BY sequence DESC LIMIT 1",
                (scope.organization_id,),
            )
            head = cur.fetchone()
            sequence = (head[0] + 1) if head else 1
            prev_hash = head[1] if head else None
            entry_hash = chain_hash(
                prev_hash=prev_hash,
                event_type=event_type,
                actor=actor,
                organization_id=scope.organization_id,
                project_id=scope.project_id,
                object_type=object_type,
                object_id=object_id,
                payload=payload,
                occurred_at=occurred_at,
            )
            cur.execute(
                """INSERT INTO audit_events(organization_id, sequence, project_id, event_type, actor,
                       object_type, object_id, payload, occurred_at, prev_hash, entry_hash)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    scope.organization_id, sequence, scope.project_id, event_type, actor,
                    object_type, object_id, Jsonb(payload, dumps=dumps), occurred_at, prev_hash, entry_hash,
                ),
            )
        return AuditEvent(sequence, event_type, actor, object_type, object_id, occurred_at, entry_hash, prev_hash, scope.project_id, payload)

    def verify_chain(self, *, scope: Scope) -> bool:
        """Recompute this organization's chain from its stored rows."""
        rows = self._fetch_all(
            scope.organization_only,
            """SELECT sequence, project_id, event_type, actor, object_type, object_id, payload,
                      occurred_at, prev_hash, entry_hash
               FROM audit_events WHERE organization_id = %s ORDER BY sequence""",
            [scope.organization_id],
        )
        prev: str | None = None
        for index, row in enumerate(rows, start=1):
            if row["sequence"] != index or row["prev_hash"] != prev:
                return False
            expected = chain_hash(
                prev_hash=prev,
                event_type=row["event_type"],
                actor=row["actor"],
                organization_id=scope.organization_id,
                project_id=row["project_id"],
                object_type=row["object_type"],
                object_id=row["object_id"],
                payload=row["payload"],
                occurred_at=row["occurred_at"],
            )
            if row["entry_hash"] != expected:
                return False
            prev = expected
        return True

    def for_object(self, *, scope: Scope, object_type: str, object_id: UUID) -> list[dict[str, Any]]:
        clause, params = self._tenant_clause(scope)
        return self._fetch_all(
            scope,
            f"""SELECT sequence, event_type, actor, object_type, object_id, payload, occurred_at, entry_hash
                FROM audit_events WHERE {clause} AND object_type = %s AND object_id = %s ORDER BY sequence""",
            [*params, object_type, object_id],
        )
