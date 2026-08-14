"""Decision persistence with fingerprints (items 35, 36).

A decision is recorded with:
- decision_fingerprint: SHA-256 over the evidence IDs, action, and rationale.
  This identifies the decision itself — two decisions with the same fingerprint
  were made from the same inputs.
- state_fingerprint: the ProjectState.fingerprint() at decision time. This
  identifies the state the decision was made from — if reconstruction today
  produces a different state fingerprint, the state has changed and the
  decision may need to be revisited.

Replay (item 35): reconstruct the project from current state, compute the
fingerprint, and compare against stored state_fingerprints. Matching
fingerprints mean the decision is still valid; differing fingerprints mean
the state has drifted.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories.base import Repository


@dataclass(frozen=True)
class Decision:
    decision_id: UUID
    organization_id: UUID
    project_id: UUID | None
    subject_type: str
    subject_id: UUID | None
    action: str
    rationale: str
    evidence_ids: tuple[UUID, ...]
    confidence: float | None
    actor: str
    annotations: dict[str, Any]
    decision_fingerprint: str | None
    state_fingerprint: str | None
    created_at: datetime


DECISION_COLUMNS = (
    "organization_id, decision_id, project_id, subject_type, subject_id, action, "
    "rationale, evidence_ids, confidence, actor, annotations, decision_fingerprint, "
    "state_fingerprint, created_at"
)


def compute_decision_fingerprint(
    *, action: str, rationale: str, evidence_ids: list[UUID]
) -> str:
    """SHA-256 over the decision's inputs — action, rationale, sorted evidence IDs."""
    import json

    payload = json.dumps({
        "action": action,
        "rationale": rationale,
        "evidence_ids": sorted(str(e) for e in evidence_ids),
    }, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


class DecisionRepository(Repository):
    table = "decisions"
    id_column = "decision_id"

    def record(
        self,
        *,
        scope: Scope,
        subject_type: str,
        subject_id: UUID | None,
        action: str,
        rationale: str = "",
        evidence_ids: list[UUID] | None = None,
        confidence: float | None = None,
        actor: str = "ai",
        annotations: dict[str, Any] | None = None,
        state_fingerprint: str | None = None,
        created_by: str = "system",
    ) -> Decision:
        """Record a decision with a fingerprint of its inputs and state."""
        from psycopg.types.json import Jsonb

        from construction_ai.persistence.serialization import dumps

        evidence_ids = evidence_ids or []
        decision_fp = compute_decision_fingerprint(
            action=action, rationale=rationale, evidence_ids=evidence_ids,
        )
        with self.db.scoped(scope) as cur:
            cur.execute(
                f"""INSERT INTO decisions(organization_id, project_id, subject_type, subject_id,
                        action, rationale, evidence_ids, confidence, actor, annotations,
                        decision_fingerprint, state_fingerprint, created_by)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING {DECISION_COLUMNS}""",
                (
                    scope.organization_id, scope.project_id, subject_type, subject_id,
                    action, rationale, list(evidence_ids), confidence, actor,
                    Jsonb(annotations or {}, dumps=dumps), decision_fp, state_fingerprint, created_by,
                ),
            )
            columns = [c.name for c in cur.description]
            return _to_decision(dict(zip(columns, cur.fetchone(), strict=True)))

    def get(self, *, scope: Scope, decision_id: UUID) -> Decision | None:
        row = self.get_row(scope=scope, record_id=decision_id, columns=DECISION_COLUMNS)
        return _to_decision(row) if row else None

    def for_project(self, *, scope: Scope) -> list[Decision]:
        project_id = scope.require_project()
        rows = self._fetch_all(
            scope,
            f"SELECT {DECISION_COLUMNS} FROM decisions WHERE organization_id = %s AND project_id = %s ORDER BY created_at",
            [scope.organization_id, project_id],
        )
        return [_to_decision(row) for row in rows]

    def by_state_fingerprint(self, *, scope: Scope, state_fingerprint: str) -> list[Decision]:
        """Find decisions made from a given state fingerprint (replay verification)."""
        rows = self._fetch_all(
            scope,
            f"SELECT {DECISION_COLUMNS} FROM decisions WHERE organization_id = %s AND state_fingerprint = %s ORDER BY created_at",
            [scope.organization_id, state_fingerprint],
        )
        return [_to_decision(row) for row in rows]

    def stale_decisions(self, *, scope: Scope, current_state_fingerprint: str) -> list[Decision]:
        """Decisions whose state_fingerprint differs from the current state.

        These decisions were made from a state that has since changed — the
        state has drifted, and the decisions may need to be revisited.
        """
        project_id = scope.require_project()
        rows = self._fetch_all(
            scope,
            f"""SELECT {DECISION_COLUMNS} FROM decisions
                WHERE organization_id = %s AND project_id = %s
                AND state_fingerprint IS NOT NULL
                AND state_fingerprint != %s
                ORDER BY created_at""",
            [scope.organization_id, project_id, current_state_fingerprint],
        )
        return [_to_decision(row) for row in rows]


def _to_decision(row: dict[str, Any]) -> Decision:
    return Decision(
        decision_id=row["decision_id"],
        organization_id=row["organization_id"],
        project_id=row.get("project_id"),
        subject_id=row.get("subject_id"),
        subject_type=row["subject_type"],
        action=row["action"],
        rationale=row.get("rationale") or "",
        evidence_ids=tuple(row.get("evidence_ids") or ()),
        confidence=row.get("confidence"),
        actor=row.get("actor") or "ai",
        annotations=row.get("annotations") or {},
        decision_fingerprint=row.get("decision_fingerprint"),
        state_fingerprint=row.get("state_fingerprint"),
        created_at=row.get("created_at"),
    )
