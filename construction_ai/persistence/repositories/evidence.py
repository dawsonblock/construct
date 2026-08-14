from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from construction_ai.domain.models import Evidence
from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories.base import Repository

EVIDENCE_COLUMNS = (
    "organization_id, evidence_id, project_id, field, value, confidence, authority, "
    "source_type, source_id, source_version_id, content_hash, extractor, observed_at, subject_type, subject_id"
)


def _to_evidence(row: dict[str, Any]) -> Evidence:
    return Evidence(
        evidence_id=str(row["evidence_id"]),
        source_type=row["source_type"],
        source_id=row["source_id"],
        field=row["field"],
        value=row["value"],
        confidence=row["confidence"],
        authority=row["authority"],
        observed_at=row["observed_at"],
        extractor=row["extractor"],
        organization_id=str(row["organization_id"]),
        source_version_id=str(row["source_version_id"]) if row.get("source_version_id") else None,
        project_id=str(row["project_id"]) if row.get("project_id") else None,
        subject_type=row.get("subject_type"),
        subject_id=str(row["subject_id"]) if row.get("subject_id") else None,
    )


class EvidenceRepository(Repository):
    table = "evidence"
    id_column = "evidence_id"

    def record(
        self,
        *,
        scope: Scope,
        field: str,
        value: Any,
        confidence: float,
        authority: float,
        source_type: str,
        source_id: str,
        source_version_id: UUID | None = None,
        content_hash: str | None = None,
        extractor: str = "deterministic",
        observed_at: datetime | None = None,
        subject_type: str | None = None,
        subject_id: UUID | None = None,
        created_by: str = "system",
    ) -> Evidence:
        from psycopg.types.json import Jsonb

        with self.db.scoped(scope) as cur:
            cur.execute(
                f"""INSERT INTO evidence(organization_id, project_id, field, value, confidence, authority,
                        source_type, source_id, source_version_id, content_hash, extractor, observed_at,
                        subject_type, subject_id, created_by)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s, now()),%s,%s,%s)
                    RETURNING {EVIDENCE_COLUMNS}""",
                (
                    scope.organization_id, scope.project_id, field, Jsonb(value), confidence, authority,
                    source_type, source_id, source_version_id, content_hash, extractor, observed_at,
                    subject_type, subject_id, created_by,
                ),
            )
            columns = [c.name for c in cur.description]
            return _to_evidence(dict(zip(columns, cur.fetchone(), strict=True)))

    def get_many(self, *, scope: Scope, evidence_ids: list[UUID]) -> list[Evidence]:
        if not evidence_ids:
            return []
        clause, params = self._tenant_clause(scope)
        rows = self._fetch_all(
            scope,
            f"SELECT {EVIDENCE_COLUMNS} FROM evidence WHERE {clause} AND evidence_id = ANY(%s) ORDER BY observed_at",
            [*params, list(evidence_ids)],
        )
        return [_to_evidence(row) for row in rows]

    def for_project(self, *, scope: Scope, field: str | None = None) -> list[Evidence]:
        project_id = scope.require_project()
        sql = f"SELECT {EVIDENCE_COLUMNS} FROM evidence WHERE organization_id = %s AND project_id = %s"
        params: list[Any] = [scope.organization_id, project_id]
        if field:
            sql += " AND field = %s"
            params.append(field)
        return [_to_evidence(row) for row in self._fetch_all(scope, sql + " ORDER BY observed_at", params)]

    def for_subject(self, *, scope: Scope, subject_type: str, subject_id: UUID) -> list[Evidence]:
        """Everything asserted about one record. What a reviewer actually asks for."""
        clause, params = self._tenant_clause(scope)
        rows = self._fetch_all(
            scope,
            f"SELECT {EVIDENCE_COLUMNS} FROM evidence WHERE {clause} AND subject_type = %s AND subject_id = %s ORDER BY field, observed_at",
            [*params, subject_type, subject_id],
        )
        return [_to_evidence(row) for row in rows]

    def for_source(self, *, scope: Scope, source_type: str, source_id: str) -> list[Evidence]:
        clause, params = self._tenant_clause(scope)
        rows = self._fetch_all(
            scope,
            f"SELECT {EVIDENCE_COLUMNS} FROM evidence WHERE {clause} AND source_type = %s AND source_id = %s ORDER BY observed_at",
            [*params, source_type, source_id],
        )
        return [_to_evidence(row) for row in rows]
