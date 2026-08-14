"""Typed nodes and typed relationships.

The distinction this file exists to hold:

    G_observed   structural, read straight off an authoritative row
    G_derived    proposed by a model or heuristic, carrying evidence and a score
    G_approved   promoted by a human or by a named deterministic rule

A model may only ever write `status='proposed'`. `observe()` is reserved for
edges that are already true because a column says so; `propose()` is what
inference gets. Nothing in this module lets `ai` be the decider — that is a
database CHECK, not a convention here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories.base import Repository

ENTITY_COLUMNS = "organization_id, entity_id, project_id, entity_type, record_table, record_id, label, external_system, external_id"
RELATIONSHIP_COLUMNS = (
    "organization_id, relationship_id, project_id, source_entity_id, target_entity_id, relation, "
    "status, origin, confidence, evidence_ids, valid_from, valid_until, decided_by, decided_at, annotations, producer"
)

PROPOSED = "proposed"
APPROVED = "approved"
REJECTED = "rejected"
SUPERSEDED = "superseded"

OBSERVED = "observed"
DERIVED = "derived"


@dataclass(frozen=True)
class Entity:
    entity_id: UUID
    entity_type: str
    label: str
    project_id: UUID | None = None
    record_table: str | None = None
    record_id: UUID | None = None
    external_system: str | None = None
    external_id: str | None = None


@dataclass(frozen=True)
class Relationship:
    relationship_id: UUID
    source_entity_id: UUID
    target_entity_id: UUID
    relation: str
    status: str
    origin: str
    confidence: float | None = None
    evidence_ids: tuple[UUID, ...] = ()
    project_id: UUID | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    annotations: dict[str, Any] | None = None
    producer: str | None = None

    @property
    def is_authoritative(self) -> bool:
        """True only for edges the system is entitled to reason from as fact."""
        return self.status == APPROVED and self.origin in {OBSERVED, DERIVED}


def _to_entity(row: dict[str, Any]) -> Entity:
    return Entity(
        entity_id=row["entity_id"],
        entity_type=row["entity_type"],
        label=row["label"],
        project_id=row.get("project_id"),
        record_table=row.get("record_table"),
        record_id=row.get("record_id"),
        external_system=row.get("external_system"),
        external_id=row.get("external_id"),
    )


def _to_relationship(row: dict[str, Any]) -> Relationship:
    return Relationship(
        relationship_id=row["relationship_id"],
        source_entity_id=row["source_entity_id"],
        target_entity_id=row["target_entity_id"],
        relation=row["relation"],
        status=row["status"],
        origin=row["origin"],
        confidence=row.get("confidence"),
        evidence_ids=tuple(row.get("evidence_ids") or ()),
        project_id=row.get("project_id"),
        valid_from=row.get("valid_from"),
        valid_until=row.get("valid_until"),
        decided_by=row.get("decided_by"),
        decided_at=row.get("decided_at"),
        annotations=row.get("annotations") or {},
        producer=row.get("producer"),
    )


class EntityRepository(Repository):
    table = "entities"
    id_column = "entity_id"

    def ensure(
        self,
        *,
        scope: Scope,
        entity_type: str,
        record_table: str,
        record_id: UUID,
        label: str = "",
        created_by: str = "projection",
    ) -> Entity:
        """Idempotent node for an authoritative row. Re-projection is a no-op."""
        with self.db.scoped(scope) as cur:
            cur.execute(
                f"""INSERT INTO entities(organization_id, project_id, entity_type, record_table, record_id, label, created_by)
                    VALUES(%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (organization_id, entity_type, record_table, record_id) DO UPDATE
                      SET label = EXCLUDED.label, project_id = EXCLUDED.project_id
                    RETURNING {ENTITY_COLUMNS}""",
                (scope.organization_id, scope.project_id, entity_type, record_table, record_id, label, created_by),
            )
            columns = [c.name for c in cur.description]
            return _to_entity(dict(zip(columns, cur.fetchone(), strict=True)))

    def get(self, *, scope: Scope, entity_id: UUID) -> Entity | None:
        row = self.get_row(scope=scope, record_id=entity_id, columns=ENTITY_COLUMNS)
        return _to_entity(row) if row else None

    def for_record(self, *, scope: Scope, record_table: str, record_id: UUID) -> Entity | None:
        row = self._fetch_one(
            scope,
            f"SELECT {ENTITY_COLUMNS} FROM entities WHERE organization_id = %s AND record_table = %s AND record_id = %s",
            [scope.organization_id, record_table, record_id],
        )
        return _to_entity(row) if row else None

    def for_project(self, *, scope: Scope) -> list[Entity]:
        project_id = scope.require_project()
        rows = self._fetch_all(
            scope,
            f"""SELECT {ENTITY_COLUMNS} FROM entities WHERE organization_id = %s AND project_id = %s
                ORDER BY entity_type, label, entity_id""",
            [scope.organization_id, project_id],
        )
        return [_to_entity(row) for row in rows]


class RelationshipRepository(Repository):
    table = "relationships"
    id_column = "relationship_id"

    def observe(
        self,
        *,
        scope: Scope,
        source_entity_id: UUID,
        target_entity_id: UUID,
        relation: str,
        evidence_ids: list[UUID] | None = None,
        producer: str = "projection",
        created_by: str = "projection",
    ) -> Relationship:
        """A structural edge: true because an authoritative column says so.

        Approved on creation with no decider, which the schema permits only for
        `origin='observed'`. Nothing inferred may use this path.
        """
        return self._upsert_current(
            scope=scope,
            source_entity_id=source_entity_id,
            target_entity_id=target_entity_id,
            relation=relation,
            status=APPROVED,
            origin=OBSERVED,
            confidence=1.0,
            evidence_ids=evidence_ids,
            decided_by=None,
            producer=producer,
            created_by=created_by,
        )

    def propose(
        self,
        *,
        scope: Scope,
        source_entity_id: UUID,
        target_entity_id: UUID,
        relation: str,
        confidence: float,
        evidence_ids: list[UUID] | None = None,
        annotations: dict[str, Any] | None = None,
        producer: str = "unspecified",
        created_by: str = "ai",
    ) -> Relationship:
        """A candidate. Never authoritative until something promotes it.

        `producer` names the rule and version that emitted it, so a change in
        matching logic is visible in the data rather than only in git history.
        """
        return self._upsert_current(
            scope=scope,
            source_entity_id=source_entity_id,
            target_entity_id=target_entity_id,
            relation=relation,
            status=PROPOSED,
            origin=DERIVED,
            confidence=confidence,
            evidence_ids=evidence_ids,
            decided_by=None,
            annotations=annotations,
            producer=producer,
            created_by=created_by,
        )

    def _upsert_current(
        self,
        *,
        scope: Scope,
        source_entity_id: UUID,
        target_entity_id: UUID,
        relation: str,
        status: str,
        origin: str,
        confidence: float | None,
        evidence_ids: list[UUID] | None,
        decided_by: str | None,
        producer: str,
        annotations: dict[str, Any] | None = None,
        created_by: str,
    ) -> Relationship:
        from psycopg.types.json import Jsonb

        from construction_ai.persistence.serialization import dumps

        with self.db.scoped(scope) as cur:
            cur.execute(
                f"""INSERT INTO relationships(organization_id, project_id, source_entity_id, target_entity_id,
                        relation, status, origin, confidence, evidence_ids, annotations, producer, created_by)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (organization_id, source_entity_id, relation, target_entity_id)
                      WHERE valid_from IS NULL
                    DO UPDATE SET
                      confidence = EXCLUDED.confidence,
                      evidence_ids = EXCLUDED.evidence_ids,
                      annotations = EXCLUDED.annotations,
                      producer = EXCLUDED.producer,
                      -- A decision already taken is never overwritten by a
                      -- re-proposal: re-running inference must not un-approve or
                      -- un-reject what a human settled.
                      status = CASE WHEN relationships.decided_by IS NOT NULL THEN relationships.status ELSE EXCLUDED.status END,
                      origin = CASE WHEN relationships.decided_by IS NOT NULL THEN relationships.origin ELSE EXCLUDED.origin END
                    RETURNING {RELATIONSHIP_COLUMNS}""",
                (
                    scope.organization_id, scope.project_id, source_entity_id, target_entity_id,
                    relation, status, origin, confidence, list(evidence_ids or []),
                    Jsonb(annotations or {}, dumps=dumps), producer, created_by,
                ),
            )
            columns = [c.name for c in cur.description]
            return _to_relationship(dict(zip(columns, cur.fetchone(), strict=True)))

    def decide(self, *, scope: Scope, relationship_id: UUID, status: str, decided_by: str) -> Relationship | None:
        """Human promotion or rejection of a candidate."""
        if status not in {APPROVED, REJECTED, SUPERSEDED}:
            raise ValueError(f"unsupported relationship status: {status!r}")
        if not decided_by or decided_by == "ai":
            raise PermissionError("promoting an inferred relationship requires a human or a named rule")
        return self._decide(scope=scope, relationship_id=relationship_id, status=status, decided_by=decided_by)

    def promote_by_rule(self, *, scope: Scope, relationship_id: UUID, rule: str) -> Relationship | None:
        """Deterministic promotion.

        Recorded as `rule:<name>` so an audit can tell a rule's decision from a
        person's. A rule is allowed to promote because it is reproducible from
        state; a model is not, which is why there is no equivalent for `ai`.
        """
        if not rule:
            raise ValueError("a promotion rule must be named")
        return self._decide(scope=scope, relationship_id=relationship_id, status=APPROVED, decided_by=f"rule:{rule}")

    def _decide(self, *, scope: Scope, relationship_id: UUID, status: str, decided_by: str) -> Relationship | None:
        clause, params = self._tenant_clause(scope)
        with self.db.scoped(scope) as cur:
            cur.execute(
                f"""UPDATE relationships SET status = %s, decided_by = %s, decided_at = now()
                    WHERE {clause} AND relationship_id = %s AND status = 'proposed'
                    RETURNING {RELATIONSHIP_COLUMNS}""",  # noqa: S608
                [status, decided_by, *params, relationship_id],
            )
            row = cur.fetchone()
            if row is None:
                return None
            columns = [c.name for c in cur.description]
            return _to_relationship(dict(zip(columns, row, strict=True)))

    def get(self, *, scope: Scope, relationship_id: UUID) -> Relationship | None:
        row = self.get_row(scope=scope, record_id=relationship_id, columns=RELATIONSHIP_COLUMNS)
        return _to_relationship(row) if row else None

    def for_project(self, *, scope: Scope, status: str | None = None) -> list[Relationship]:
        project_id = scope.require_project()
        sql = f"SELECT {RELATIONSHIP_COLUMNS} FROM relationships WHERE organization_id = %s AND project_id = %s"
        params: list[Any] = [scope.organization_id, project_id]
        if status:
            sql += " AND status = %s"
            params.append(status)
        # Total ordering: reconstruction must be byte-identical across calls.
        sql += " ORDER BY relation, source_entity_id, target_entity_id, relationship_id"
        return [_to_relationship(row) for row in self._fetch_all(scope, sql, params)]

    def for_entity(self, *, scope: Scope, entity_id: UUID) -> list[Relationship]:
        clause, params = self._tenant_clause(scope)
        rows = self._fetch_all(
            scope,
            f"""SELECT {RELATIONSHIP_COLUMNS} FROM relationships
                WHERE {clause} AND (source_entity_id = %s OR target_entity_id = %s)
                ORDER BY relation, source_entity_id, target_entity_id, relationship_id""",
            [*params, entity_id, entity_id],
        )
        return [_to_relationship(row) for row in rows]
