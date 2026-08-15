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

        # rc9: The entire supersession operation (validate + insert + mark old)
        # now runs in ONE transaction with FOR UPDATE row locking on the
        # superseded record. This eliminates the race where another process
        # could revoke/retract the target between validation and mutation.
        #
        # The invariant is:
        #   SupersessionRequest = Atomic(
        #     LockOld(FOR UPDATE),
        #     ValidateSubject,
        #     ValidateOldStillConfirmed,
        #     InsertNew,
        #     SupersedeOld,
        #   )
        # If any step fails: NoNewConfirmationPersisted.
        with self.db.scoped(scope) as cur:
            # rc9: Lock and validate the superseded record in the same transaction.
            old_record = None
            if supersedes_confirmation_id is not None:
                cur.execute(
                    """SELECT project_id, scope_id, invoice_id, sov_item_id, status
                       FROM work_confirmations
                       WHERE organization_id = %s AND confirmation_id = %s
                       FOR UPDATE""",
                    (scope.organization_id, supersedes_confirmation_id),
                )
                old_record = row_to_dict(cur)
                if old_record is None:
                    raise ValueError(
                        f"cannot supersede confirmation {supersedes_confirmation_id}: not found"
                    )
                # Validate old is still confirmed (under lock).
                if old_record.get("status") != "confirmed":
                    raise ValueError(
                        f"cannot supersede confirmation {supersedes_confirmation_id}: "
                        f"status is '{old_record.get('status')}', not 'confirmed'"
                    )
                # Validate same subject.
                if old_record.get("project_id") != project_id:
                    raise ValueError(
                        f"supersession subject mismatch: new confirmation project_id={project_id} "
                        f"does not match superseded confirmation project_id={old_record.get('project_id')} — "
                        "rc9: supersession can only target the same financial/work subject"
                    )
                if old_record.get("sov_item_id") != sov_item_id:
                    raise ValueError(
                        f"supersession subject mismatch: new confirmation sov_item_id={sov_item_id} "
                        f"does not match superseded confirmation sov_item_id={old_record.get('sov_item_id')} — "
                        "rc9: supersession can only target the same financial/work subject"
                    )
                if old_record.get("invoice_id") != invoice_id:
                    raise ValueError(
                        f"supersession subject mismatch: new confirmation invoice_id={invoice_id} "
                        f"does not match superseded confirmation invoice_id={old_record.get('invoice_id')} — "
                        "rc9: supersession can only target the same financial/work subject"
                    )

            # Insert the new confirmation.
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

            # Mark old as superseded in the SAME transaction.
            # rc9: Check rowcount — if 0 rows affected, the target was modified
            # between our lock and this update (shouldn't happen with FOR UPDATE,
            # but be defensive). Raise so the insert rolls back.
            if supersedes_confirmation_id is not None:
                cur.execute(
                    """UPDATE work_confirmations
                       SET status = 'superseded', updated_at = now()
                       WHERE organization_id = %s AND confirmation_id = %s AND status = 'confirmed'""",
                    (scope.organization_id, supersedes_confirmation_id),
                )
                if cur.rowcount != 1:
                    raise ValueError(
                        f"supersession conflict: confirmation {supersedes_confirmation_id} "
                        "was no longer 'confirmed' when marking it superseded — "
                        "the successor insert will be rolled back"
                    )
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

    def revoke(self, *, scope: Scope, confirmation_id: UUID, reason: str | None = None) -> bool:
        """rc7 Phase 17: Revoke a confirmation.

        Only ACTIVE (confirmed) confirmations can be revoked.
        SUPERSEDED → ACTIVE is not allowed without a dedicated restoration.
        """
        with self.db.scoped(scope) as cur:
            cur.execute(
                """UPDATE work_confirmations
                   SET status = 'revoked', updated_at = now()
                   WHERE organization_id = %s AND confirmation_id = %s AND status = 'confirmed'""",
                (scope.organization_id, confirmation_id),
            )
            return cur.rowcount > 0

    def retract(self, *, scope: Scope, confirmation_id: UUID, reason: str | None = None) -> bool:
        """rc7 Phase 17: Retract a confirmation.

        Only ACTIVE (confirmed) confirmations can be retracted.
        This is distinct from revoke: retraction is initiated by the confirmer,
        while revocation is an administrative action.
        """
        with self.db.scoped(scope) as cur:
            cur.execute(
                """UPDATE work_confirmations
                   SET status = 'retracted', updated_at = now()
                   WHERE organization_id = %s AND confirmation_id = %s AND status = 'confirmed'""",
                (scope.organization_id, confirmation_id),
            )
            return cur.rowcount > 0

    def _validate_same_subject_before_supersede_raw(
        self, *, scope: Scope, project_id: UUID | None, sov_item_id: UUID | None,
        invoice_id: UUID | None, superseded_id: UUID,
    ) -> None:
        """rc8: Validate supersession subject BEFORE inserting the new confirmation.

        This is the atomic version that uses raw parameters instead of a
        WorkConfirmation object. It must be called BEFORE the INSERT so that
        if validation fails, no partial write is persisted.

        The invariant is:
          new.project_id == old.project_id
          AND new.sov_item_id == old.sov_item_id
          AND new.invoice_id == old.invoice_id
        """
        with self.db.scoped(scope) as cur:
            cur.execute(
                """SELECT project_id, scope_id, invoice_id, sov_item_id, status
                   FROM work_confirmations
                   WHERE organization_id = %s AND confirmation_id = %s""",
                (scope.organization_id, superseded_id),
            )
            old = row_to_dict(cur)
            if old is None:
                raise ValueError(
                    f"cannot supersede confirmation {superseded_id}: not found"
                )

            # rc8: Check that the target is still active (confirmed).
            if old.get("status") != "confirmed":
                raise ValueError(
                    f"cannot supersede confirmation {superseded_id}: "
                    f"status is '{old.get('status')}', not 'confirmed'"
                )

        # Check project_id.
        old_project = old.get("project_id")
        if old_project != project_id:
            raise ValueError(
                f"supersession subject mismatch: new confirmation project_id={project_id} "
                f"does not match superseded confirmation project_id={old_project} — "
                "rc8: supersession can only target the same financial/work subject"
            )

        # Check SOV item.
        old_sov = old.get("sov_item_id")
        if old_sov != sov_item_id:
            raise ValueError(
                f"supersession subject mismatch: new confirmation sov_item_id={sov_item_id} "
                f"does not match superseded confirmation sov_item_id={old_sov} — "
                "rc8: supersession can only target the same financial/work subject"
            )

        # Check invoice_id.
        old_invoice = old.get("invoice_id")
        if old_invoice != invoice_id:
            raise ValueError(
                f"supersession subject mismatch: new confirmation invoice_id={invoice_id} "
                f"does not match superseded confirmation invoice_id={old_invoice} — "
                "rc8: supersession can only target the same financial/work subject"
            )

    def _validate_same_subject_before_supersede(
        self, *, scope: Scope, new_confirmation: WorkConfirmation, superseded_id: UUID,
    ) -> None:
        """rc7: Validate that the new and superseded confirmations share the
        same financial/work subject.

        The invariant is:
          new.project_id == old.project_id
          AND new.sov_item_id == old.sov_item_id
          AND new.invoice_id == old.invoice_id

        A confirmation for roofing SOV must not supersede a confirmation for
        electrical SOV, even if the ID is known. This prevents accidental
        removal of valid evidence from another scope.
        """
        with self.db.scoped(scope) as cur:
            cur.execute(
                """SELECT project_id, scope_id, invoice_id, sov_item_id
                   FROM work_confirmations
                   WHERE organization_id = %s AND confirmation_id = %s""",
                (scope.organization_id, superseded_id),
            )
            from construction_ai.persistence.db import row_to_dict
            old = row_to_dict(cur)
            if old is None:
                raise ValueError(
                    f"cannot supersede confirmation {superseded_id}: not found"
                )

        # Check project_id.
        old_project = old.get("project_id")
        new_project = new_confirmation.project_id
        if old_project != new_project:
            raise ValueError(
                f"supersession subject mismatch: new confirmation project_id={new_project} "
                f"does not match superseded confirmation project_id={old_project} — "
                "rc7: supersession can only target the same financial/work subject"
            )

        # Check SOV item.
        old_sov = old.get("sov_item_id")
        new_sov = new_confirmation.sov_item_id
        if old_sov != new_sov:
            raise ValueError(
                f"supersession subject mismatch: new confirmation sov_item_id={new_sov} "
                f"does not match superseded confirmation sov_item_id={old_sov} — "
                "rc7: supersession can only target the same financial/work subject"
            )

        # Check invoice_id.
        old_invoice = old.get("invoice_id")
        new_invoice = new_confirmation.invoice_id
        if old_invoice != new_invoice:
            raise ValueError(
                f"supersession subject mismatch: new confirmation invoice_id={new_invoice} "
                f"does not match superseded confirmation invoice_id={old_invoice} — "
                "rc7: supersession can only target the same financial/work subject"
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
