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
    audit_event_id: UUID | None = None  # rc4 Phase 16: for final-audit linking


@dataclass(frozen=True)
class AuditCheckpoint:
    checkpoint_id: UUID
    sequence: int
    entry_hash: str
    event_count: int
    checkpoint_hash: str
    exported_by: str
    created_at: datetime


def checkpoint_hash(
    *,
    organization_id: UUID,
    sequence: int,
    entry_hash: str,
    event_count: int,
    created_at: datetime,
) -> str:
    """Bind the chain state into a single tamper-evident hash."""
    material = CHAIN_SEPARATOR.join(
        [str(organization_id), str(sequence), entry_hash, str(event_count), created_at.isoformat()]
    )
    return hashlib.sha256(material.encode()).hexdigest()


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
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   RETURNING audit_event_id""",
                (
                    scope.organization_id, sequence, scope.project_id, event_type, actor,
                    object_type, object_id, Jsonb(payload, dumps=dumps), occurred_at, prev_hash, entry_hash,
                ),
            )
            event_id_row = cur.fetchone()
            audit_event_id = event_id_row[0] if event_id_row else None
        return AuditEvent(sequence, event_type, actor, object_type, object_id, occurred_at, entry_hash, prev_hash, scope.project_id, payload, audit_event_id=audit_event_id)

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

    # -- external checkpointing (v0.4.3 item 20) ---------------------------

    def create_checkpoint(self, *, scope: Scope, exported_by: str) -> AuditCheckpoint:
        """Capture the current chain head as a tamper-evident checkpoint.

        The checkpoint binds (organization, last sequence, last entry_hash,
        event count, timestamp) into a single hash. Stored append-only in
        audit_checkpoints AND returned to the caller for external anchoring.
        """
        created_at = datetime.now(timezone.utc)
        with self.db.scoped(scope) as cur:
            cur.execute(
                "SELECT sequence, entry_hash, count(*) OVER () AS total "
                "FROM audit_events WHERE organization_id = %s ORDER BY sequence DESC LIMIT 1",
                (scope.organization_id,),
            )
            head = cur.fetchone()
            if head is None:
                sequence, entry_hash, event_count = 0, "", 0
            else:
                sequence, entry_hash, event_count = head[0], head[1], head[2]
            cp_hash = checkpoint_hash(
                organization_id=scope.organization_id,
                sequence=sequence,
                entry_hash=entry_hash,
                event_count=event_count,
                created_at=created_at,
            )
            cur.execute(
                """INSERT INTO audit_checkpoints(organization_id, sequence, entry_hash, event_count,
                       checkpoint_hash, exported_by, created_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING checkpoint_id""",
                (scope.organization_id, sequence, entry_hash, event_count, cp_hash, exported_by, created_at),
            )
            row = cur.fetchone()
        return AuditCheckpoint(
            checkpoint_id=row[0],
            sequence=sequence,
            entry_hash=entry_hash,
            event_count=event_count,
            checkpoint_hash=cp_hash,
            exported_by=exported_by,
            created_at=created_at,
        )

    def verify_checkpoint(self, *, scope: Scope, checkpoint: AuditCheckpoint) -> bool:
        """Verify a previously exported checkpoint against the current chain.

        Returns True only if:
        1. The chain head matches the checkpoint — same last sequence, same
           last entry_hash, same event count.
        2. The chain is internally consistent (verify_chain passes) — no event
           has been tampered with, since recomputing the chain would detect a
           changed payload even if the stored hash column was left alone.

        A rewritten chain (even by someone with owner access) will fail one or
        both checks if the original checkpoint was anchored externally.
        """
        # Check 1: chain integrity — recompute all hashes.
        if not self.verify_chain(scope=scope):
            return False
        # Check 2: head matches the checkpoint.
        with self.db.scoped(scope) as cur:
            cur.execute(
                "SELECT sequence, entry_hash, count(*) OVER () AS total "
                "FROM audit_events WHERE organization_id = %s ORDER BY sequence DESC LIMIT 1",
                (scope.organization_id,),
            )
            head = cur.fetchone()
        if head is None:
            current_seq, current_hash, current_count = 0, "", 0
        else:
            current_seq, current_hash, current_count = head[0], head[1], head[2]
        return (
            current_seq == checkpoint.sequence
            and current_hash == checkpoint.entry_hash
            and current_count == checkpoint.event_count
        )

    def list_checkpoints(self, *, scope: Scope) -> list[dict[str, Any]]:
        return self._fetch_all(
            scope,
            """SELECT checkpoint_id, sequence, entry_hash, event_count, checkpoint_hash,
                      exported_by, created_at
               FROM audit_checkpoints WHERE organization_id = %s ORDER BY created_at""",
            [scope.organization_id],
        )
