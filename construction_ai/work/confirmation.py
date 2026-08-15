"""WorkConfirmationService — does the evidence support a 'work confirmed' check?

The verifier asks a yes/no question with an evidence-backed answer. This service
reads work_confirmations records and reports whether confirmed work exists for a
project/invoice/scope. It never accepts a caller's assertion.

v0.5.0-rc3 (Phase 15): Work confirmation is now scope-specific. When an invoice
allocates to specific schedule-of-values items, the service checks that work is
confirmed for *those* scopes — "electrical work confirmed" no longer satisfies a
"roofing invoice."

v0.5.0-rc5 (Phase 4): Eliminates max() percent complete reduction. Verified completion
is determined via authoritative supersession:
Authority tier (certified_inspection > architect > superintendent > photo) + recency.
"""
from __future__ import annotations

from uuid import UUID

from construction_ai.persistence.db import Scope
from construction_ai.work.repository import WorkConfirmationRepository

#: Hierarchy of confirmation authority tiers.
CONFIRMATION_AUTHORITY: dict[str, int] = {
    "signed_inspection": 100,
    "progress_certification": 90,
    "erp_goods_receipt": 80,
    "completed_work_order": 70,
    "superintendent": 50,
    "daily_field_report": 40,
    "delivery_receipt": 30,
    "manual": 20,
    "system": 10,
}


class WorkConfirmationService:
    def __init__(self, repository: WorkConfirmationRepository):
        self.repository = repository

    def is_work_confirmed(
        self,
        *,
        scope: Scope,
        project_id: UUID | None,
        invoice_id: UUID | None = None,
        allow_project_fallback: bool = True,
    ) -> bool:
        """True only when an active, confirmed work record exists.

        rc5 Phase 5: When verifying invoices, invoice-specific confirmations are checked.
        Project-level confirmation is only a fallback when project fallback is permitted.
        """
        if invoice_id is not None:
            if self.repository.for_invoice(scope=scope, invoice_id=invoice_id):
                return True
        if allow_project_fallback and project_id is not None:
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
        """The authoritative verified percent_complete for a scope.

        rc5 Phase 4: Uses authoritative supersession rather than max().
        Confirmations are ranked by authority level, then effective/occurred_at timestamp.

        rc6: Explicit supersession semantics. The repository query already
        filters to status='confirmed', so records marked 'superseded',
        'retracted', or 'revoked' are excluded. A higher-authority
        certification that explicitly supersedes an earlier one transitions
        the earlier record to 'superseded' at record time, so it cannot
        dominate the active set even if its authority tier is higher.
        """
        confirmations = self.repository.for_sov_item(scope=scope, sov_item_id=sov_item_id)
        # rc6: defensive double-filter — only 'confirmed' records are active.
        # 'superseded', 'retracted', and 'revoked' are excluded.
        active = [c for c in confirmations if c.status == "confirmed" and c.percent_complete is not None]
        if not active:
            return None

        def _rank_key(c):
            auth_rank = CONFIRMATION_AUTHORITY.get(c.confirmation_type, 10)
            ts = c.occurred_at.timestamp() if c.occurred_at else 0.0
            return (auth_rank, ts)

        authoritative = max(active, key=_rank_key)
        return authoritative.percent_complete
