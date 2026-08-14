from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from construction_ai.domain.models import Approval, ApprovalStatus
from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories.base import Repository

APPROVAL_COLUMNS = (
    "organization_id, approval_id, project_id, reference, approval_type, subject_type, subject_id, "
    "recommended_action, amount, currency, status, exceptions, evidence_ids, requested_by, decided_by, decided_at, "
    "state_fingerprint"
)


def _to_approval(row: dict[str, Any]) -> Approval:
    return Approval(
        approval_id=str(row["approval_id"]),
        organization_id=str(row["organization_id"]),
        type=row["approval_type"],
        subject_id=str(row["subject_id"]),
        recommended_action=row["recommended_action"],
        amount=float(row["amount"]) if row.get("amount") is not None else None,
        exceptions=list(row.get("exceptions") or []),
        evidence_ids=[str(e) for e in (row.get("evidence_ids") or [])],
        status=ApprovalStatus(row["status"]),
        approved_by=row.get("decided_by"),
        approved_at=row.get("decided_at"),
        reference=row["reference"],
        project_id=str(row["project_id"]) if row.get("project_id") else None,
        currency=row.get("currency") or "CAD",
        requested_by=row.get("requested_by") or "ai",
        state_fingerprint=row.get("state_fingerprint"),
    )


class ApprovalRepository(Repository):
    table = "approvals"
    id_column = "approval_id"

    def create(
        self,
        *,
        scope: Scope,
        reference: str,
        approval_type: str,
        subject_type: str,
        subject_id: UUID,
        recommended_action: str,
        amount: float | None = None,
        exceptions: list[str] | None = None,
        evidence_ids: list[UUID] | None = None,
        requested_by: str = "ai",
        created_by: str = "system",
        currency: str = "CAD",
    ) -> Approval:
        from psycopg.types.json import Jsonb

        with self.db.scoped(scope) as cur:
            cur.execute(
                f"""INSERT INTO approvals(organization_id, project_id, reference, approval_type, subject_type,
                        subject_id, recommended_action, amount, currency, exceptions, evidence_ids,
                        requested_by, created_by)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING {APPROVAL_COLUMNS}""",
                (
                    scope.organization_id, scope.project_id, reference, approval_type, subject_type,
                    subject_id, recommended_action,
                    Decimal(str(amount)) if amount is not None else None,
                    currency,
                    Jsonb(exceptions or []), list(evidence_ids or []), requested_by, created_by,
                ),
            )
            columns = [c.name for c in cur.description]
            return _to_approval(dict(zip(columns, cur.fetchone(), strict=True)))

    def get(self, *, scope: Scope, approval_id: UUID) -> Approval | None:
        row = self.get_row(scope=scope, record_id=approval_id, columns=APPROVAL_COLUMNS)
        return _to_approval(row) if row else None

    def get_for_update(self, *, scope: Scope, approval_id: UUID) -> Approval | None:
        """Lock the row for the duration of the shared transaction.

        Used by the atomic approval service so two concurrent approvers cannot
        both pass validation against a pending row and then both write. The
        conditional UPDATE in `decide` is the backstop; this is the race guard
        that lets the service read authoritative state before mutating.
        """
        clause, params = self._tenant_clause(scope)
        row = self._fetch_one(
            scope,
            f"SELECT {APPROVAL_COLUMNS} FROM approvals WHERE {clause} AND approval_id = %s FOR UPDATE",  # noqa: S608
            [*params, approval_id],
        )
        return _to_approval(row) if row else None

    def list(self, *, scope: Scope, status: str | None = None) -> list[Approval]:
        clause, params = self._tenant_clause(scope)
        sql = f"SELECT {APPROVAL_COLUMNS} FROM approvals WHERE {clause}"
        if status:
            sql += " AND status = %s"
            params.append(status)
        return [_to_approval(row) for row in self._fetch_all(scope, sql + " ORDER BY created_at DESC", params)]

    def for_subject(self, *, scope: Scope, subject_type: str, subject_id: UUID) -> Approval | None:
        clause, params = self._tenant_clause(scope)
        row = self._fetch_one(
            scope,
            f"SELECT {APPROVAL_COLUMNS} FROM approvals WHERE {clause} AND subject_type = %s AND subject_id = %s ORDER BY created_at DESC LIMIT 1",
            [*params, subject_type, subject_id],
        )
        return _to_approval(row) if row else None

    def decide(self, *, scope: Scope, approval_id: UUID, status: str, decided_by: str, state_fingerprint: str | None = None) -> Approval | None:
        """Only a pending approval can be decided, and never by 'ai'.

        The transition is a single conditional UPDATE so two concurrent approvers
        cannot both observe 'pending' and both write. The database CHECK is the
        backstop; this is the race guard.

        v0.5.0-rc1 (item 48): state_fingerprint records the project state at
        decision time, enabling stale-approval detection before execution.
        """
        if status not in {"approved", "held", "rejected"}:
            raise ValueError(f"unsupported approval status: {status!r}")
        if not decided_by or decided_by == "ai":
            raise PermissionError("an approval decision requires a human actor")
        clause, params = self._tenant_clause(scope)
        with self.db.scoped(scope) as cur:
            cur.execute(
                f"""UPDATE approvals SET status = %s, decided_by = %s, decided_at = now(), state_fingerprint = %s
                    WHERE {clause} AND approval_id = %s AND status = 'pending'
                    RETURNING {APPROVAL_COLUMNS}""",  # noqa: S608
                [status, decided_by, state_fingerprint, *params, approval_id],
            )
            row = cur.fetchone()
            if row is None:
                return None
            columns = [c.name for c in cur.description]
            return _to_approval(dict(zip(columns, row, strict=True)))


class ApprovalPacketRepository(Repository):
    table = "approval_packets"
    id_column = "packet_id"

    def create(self, *, scope: Scope, approval_id: UUID, reference: str, payload: dict[str, Any], created_by: str = "system") -> UUID:
        from psycopg.types.json import Jsonb

        from construction_ai.persistence.serialization import dumps

        with self.db.scoped(scope) as cur:
            cur.execute(
                """INSERT INTO approval_packets(organization_id, approval_id, project_id, reference, payload, created_by)
                   VALUES(%s,%s,%s,%s,%s,%s) RETURNING packet_id""",
                (scope.organization_id, approval_id, scope.project_id, reference, Jsonb(payload, dumps=dumps), created_by),
            )
            return cur.fetchone()[0]

    def get(self, *, scope: Scope, packet_id: UUID) -> dict[str, Any] | None:
        row = self.get_row(scope=scope, record_id=packet_id, columns="payload")
        return row["payload"] if row else None

    def for_approval(self, *, scope: Scope, approval_id: UUID) -> dict[str, Any] | None:
        clause, params = self._tenant_clause(scope)
        row = self._fetch_one(
            scope,
            f"SELECT payload FROM approval_packets WHERE {clause} AND approval_id = %s ORDER BY updated_at DESC LIMIT 1",
            [*params, approval_id],
        )
        return row["payload"] if row else None
