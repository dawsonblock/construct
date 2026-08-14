"""WorkConfirmationService — does the evidence support a 'work confirmed' check?

The verifier asks a yes/no question with an evidence-backed answer. This service
reads work_confirmations records and reports whether confirmed work exists for a
project/invoice/scope. It never accepts a caller's assertion.

v0.5.0-rc3 (Phase 15): Work confirmation is now scope-specific. When an invoice
allocates to specific schedule-of-values items, the service checks that work is
confirmed for *those* scopes — "electrical work confirmed" no longer satisfies a
"roofing invoice." Project-level confirmation remains as a fallback only when no
SOV allocations exist (policy may tighten this).
"""
from __future__ import annotations

from uuid import UUID

from construction_ai.persistence.db import Scope
from construction_ai.work.repository import WorkConfirmationRepository


class WorkConfirmationService:
    def __init__(self, repository: WorkConfirmationRepository):
        self.repository = repository

    def is_work_confirmed(self, *, scope: Scope, project_id: UUID | None, invoice_id: UUID | None = None) -> bool:
        """True only when an active, confirmed work record exists."""
        if invoice_id is not None:
            if self.repository.for_invoice(scope=scope, invoice_id=invoice_id):
                return True
        if project_id is not None:
            if self.repository.for_project(scope=scope, project_id=project_id):
                return True
        return False

    def is_work_confirmed_for_sov_items(self, *, scope: Scope, sov_item_ids: list[UUID]) -> bool:
        """Phase 15: every billed SOV item must have an active confirmation.

        Returns True only when each named scope has at least one confirmed
        work_confirmations record. An empty list means nothing is being billed
        under SOV scope — the caller should fall back to project-level logic.
        """
        if not sov_item_ids:
            return False
        for sov_item_id in sov_item_ids:
            if not self.repository.for_sov_item(scope=scope, sov_item_id=sov_item_id):
                return False
        return True

    def verified_percent_complete(self, *, scope: Scope, sov_item_id: UUID) -> float | None:
        """The maximum percent_complete among active confirmations for a scope.

        Used by progress billing (Phase 16) as VerifiedPercentComplete. Returns
        None when no confirmation exists — UNAVAILABLE, not 0.
        """
        confirmations = self.repository.for_sov_item(scope=scope, sov_item_id=sov_item_id)
        percents = [c.percent_complete for c in confirmations if c.percent_complete is not None]
        return max(percents) if percents else None
