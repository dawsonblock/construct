from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from construction_ai.persistence.db import Database, Scope, row_to_dict
from construction_ai.work.models import WorkConfirmation


def _to_confirmation(row: dict[str, Any]) -> WorkConfirmation:
    return WorkConfirmation(
        confirmation_id=row["confirmation_id"],
        organization_id=row["organization_id"],
        project_id=row["project_id"],
        scope_id=row.get("scope_id"),
        invoice_id=row.get("invoice_id"),
        confirmed_by_user_id=row.get("confirmed_by_user_id"),
        confirmation_type=row["confirmation_type"],
        status=row["status"],
        percent_complete=float(row["percent_complete"]) if row.get("percent_complete") is not None else None,
        occurred_at=row["occurred_at"],
    )


class WorkConfirmationRepository:
    def __init__(self, db: Database):
        self.db = db

    def record(
        self,
        *,
        scope: Scope,
        project_id: UUID | None = None,
        scope_id: UUID | None = None,
        invoice_id: UUID | None = None,
        confirmed_by_user_id: UUID | None = None,
        confirmation_type: str,
        percent_complete: float | None = None,
        quantity: float | None = None,
        occurred_at: datetime | None = None,
        evidence_ids: list[UUID] | None = None,
        created_by: str = "system",
    ) -> WorkConfirmation:
        from decimal import Decimal

        with self.db.scoped(scope) as cur:
            cur.execute(
                """INSERT INTO work_confirmations(
                       organization_id, project_id, scope_id, invoice_id, confirmed_by_user_id,
                       confirmation_type, percent_complete, quantity, occurred_at, evidence_ids, created_by)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s, now()),%s,%s)
                   RETURNING confirmation_id, organization_id, project_id, scope_id, invoice_id,
                             confirmed_by_user_id, confirmation_type, status, percent_complete, occurred_at""",
                (
                    scope.organization_id, project_id, scope_id, invoice_id, confirmed_by_user_id,
                    confirmation_type,
                    percent_complete if percent_complete is None else float(percent_complete),
                    Decimal(str(quantity)) if quantity is not None else None,
                    occurred_at,
                    list(evidence_ids or []),
                    created_by,
                ),
            )
            return _to_confirmation(row_to_dict(cur))

    def for_project(self, *, scope: Scope, project_id: UUID) -> list[WorkConfirmation]:
        with self.db.scoped(scope) as cur:
            cur.execute(
                """SELECT confirmation_id, organization_id, project_id, scope_id, invoice_id,
                          confirmed_by_user_id, confirmation_type, status, percent_complete, occurred_at
                   FROM work_confirmations
                   WHERE organization_id = %s AND project_id = %s AND status = 'confirmed'
                   ORDER BY occurred_at DESC""",
                (scope.organization_id, project_id),
            )
            from construction_ai.persistence.db import rows_to_dicts

            return [_to_confirmation(r) for r in rows_to_dicts(cur)]

    def for_invoice(self, *, scope: Scope, invoice_id: UUID) -> list[WorkConfirmation]:
        with self.db.scoped(scope) as cur:
            cur.execute(
                """SELECT confirmation_id, organization_id, project_id, scope_id, invoice_id,
                          confirmed_by_user_id, confirmation_type, status, percent_complete, occurred_at
                   FROM work_confirmations
                   WHERE organization_id = %s AND invoice_id = %s AND status = 'confirmed'
                   ORDER BY occurred_at DESC""",
                (scope.organization_id, invoice_id),
            )
            from construction_ai.persistence.db import rows_to_dicts

            return [_to_confirmation(r) for r in rows_to_dicts(cur)]
