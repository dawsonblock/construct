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
        sov_item_id=row.get("sov_item_id"),
        supersedes_confirmation_id=row.get("supersedes_confirmation_id"),
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
        sov_item_id: UUID | None = None,
        created_by: str = "system",
        supersedes_confirmation_id: UUID | None = None,
    ) -> WorkConfirmation:
        from decimal import Decimal

        with self.db.scoped(scope) as cur:
            cur.execute(
                """INSERT INTO work_confirmations(
                       organization_id, project_id, scope_id, invoice_id, confirmed_by_user_id,
                       confirmation_type, percent_complete, quantity, occurred_at, evidence_ids, sov_item_id, created_by,
                       supersedes_confirmation_id)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s, now()),%s,%s,%s,%s)
                   RETURNING confirmation_id, organization_id, project_id, scope_id, invoice_id,
                             confirmed_by_user_id, confirmation_type, status, percent_complete, occurred_at, sov_item_id,
                             supersedes_confirmation_id""",
                (
                    scope.organization_id, project_id, scope_id, invoice_id, confirmed_by_user_id,
                    confirmation_type,
                    percent_complete if percent_complete is None else float(percent_complete),
                    Decimal(str(quantity)) if quantity is not None else None,
                    occurred_at,
                    list(evidence_ids or []),
                    sov_item_id,
                    created_by,
                    supersedes_confirmation_id,
                ),
            )
            confirmation = _to_confirmation(row_to_dict(cur))

        # rc6: If this confirmation supersedes an earlier one, transition the
        # earlier record to 'superseded' so active selection filters it out.
        if supersedes_confirmation_id is not None:
            self._mark_superseded(scope=scope, confirmation_id=supersedes_confirmation_id)
        return confirmation

    def _mark_superseded(self, *, scope: Scope, confirmation_id: UUID) -> None:
        """rc6: Transition an earlier confirmation to 'superseded'."""
        with self.db.scoped(scope) as cur:
            cur.execute(
                """UPDATE work_confirmations
                   SET status = 'superseded', updated_at = now()
                   WHERE organization_id = %s AND confirmation_id = %s AND status = 'confirmed'""",
                (scope.organization_id, confirmation_id),
            )

    def for_project(self, *, scope: Scope, project_id: UUID) -> list[WorkConfirmation]:
        with self.db.scoped(scope) as cur:
            cur.execute(
                """SELECT confirmation_id, organization_id, project_id, scope_id, invoice_id,
                          confirmed_by_user_id, confirmation_type, status, percent_complete, occurred_at, sov_item_id,
                          supersedes_confirmation_id
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
                          confirmed_by_user_id, confirmation_type, status, percent_complete, occurred_at, sov_item_id,
                          supersedes_confirmation_id
                   FROM work_confirmations
                   WHERE organization_id = %s AND invoice_id = %s AND status = 'confirmed'
                   ORDER BY occurred_at DESC""",
                (scope.organization_id, invoice_id),
            )
            from construction_ai.persistence.db import rows_to_dicts

            return [_to_confirmation(r) for r in rows_to_dicts(cur)]

    def for_sov_item(self, *, scope: Scope, sov_item_id: UUID) -> list[WorkConfirmation]:
        """Phase 15: scope-specific confirmations bound to a SOV item."""
        with self.db.scoped(scope) as cur:
            cur.execute(
                """SELECT confirmation_id, organization_id, project_id, scope_id, invoice_id,
                          confirmed_by_user_id, confirmation_type, status, percent_complete, occurred_at, sov_item_id,
                          supersedes_confirmation_id
                   FROM work_confirmations
                   WHERE organization_id = %s AND sov_item_id = %s AND status = 'confirmed'
                   ORDER BY occurred_at DESC""",
                (scope.organization_id, sov_item_id),
            )
            from construction_ai.persistence.db import rows_to_dicts

            return [_to_confirmation(r) for r in rows_to_dicts(cur)]
