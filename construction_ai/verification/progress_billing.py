"""Quantitative progress billing (Phase 16).

Replaces the boolean "percent_complete → True/False" reduction with real
earned-value math, per SOV item:

    AdjustedContractValue = BaseContract + ApprovedChangeOrders
    EarnedValue           = AdjustedContractValue × VerifiedPercentComplete
    CurrentBillable       = EarnedValue - PreviouslyApprovedBilling - Retainage
    InvoiceCurrentAmount  ≤ CurrentBillable   else OVERBILLING / REVIEW_REQUIRED

All arithmetic is Decimal end to end. The service reads only persisted
authoritative records (contracts, SOV items, change orders, invoice allocations,
work confirmations) — never caller-supplied amounts.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from construction_ai.domain.models import ProgressBillingResult
from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories import Repositories
from construction_ai.work.confirmation import WorkConfirmationService

#: Default retainage percentage when policy does not specify one.
DEFAULT_RETAINAGE_PCT = Decimal("10")


@dataclass(frozen=True)
class ProgressBillingOutcome:
    """Aggregate outcome across every SOV item an invoice allocates to."""
    per_item: list[ProgressBillingResult]
    overbilled: bool  # True if any item is overbilled
    evaluated: bool  # False when the invoice has no SOV allocations


def evaluate_progress_billing(
    repos: Repositories,
    *,
    scope: Scope,
    invoice_id: UUID,
    retainage_pct: Decimal = DEFAULT_RETAINAGE_PCT,
) -> ProgressBillingOutcome:
    """Evaluate overbilling for every SOV item an invoice allocates to.

    Returns evaluated=False when the invoice has no SOV allocations (the
    progress-billing check is then UNAVAILABLE, not PASS — see verify_invoice).
    """
    allocations = repos.invoice_allocations.for_invoice(scope=scope, invoice_id=invoice_id)
    if not allocations:
        return ProgressBillingOutcome(per_item=[], overbilled=False, evaluated=False)

    work_service = WorkConfirmationService(repos.work_confirmations)
    per_item: list[ProgressBillingResult] = []
    overbilled = False

    for alloc in allocations:
        sov_item_id = UUID(alloc.sov_item_id)
        sov_item = repos.sov_items.get(scope=scope, sov_item_id=sov_item_id)
        if sov_item is None:
            # Allocation points at a missing SOV item — data integrity failure.
            # Treat as overbilled/unverifiable so the invoice cannot auto-approve.
            per_item.append(ProgressBillingResult(
                sov_item_id=str(sov_item_id), adjusted_contract_value=Decimal("0"),
                verified_percent_complete=None, earned_value=Decimal("0"),
                previously_approved_billing=Decimal("0"), retainage=Decimal("0"),
                current_billable=Decimal("0"), invoice_amount=_dec(alloc.amount) or Decimal("0"),
                overbilled=True, currency=alloc.currency,
            ))
            overbilled = True
            continue

        contract = repos.contracts.get(scope=scope, contract_id=UUID(sov_item.contract_id))
        base = _dec(sov_item.base_value) or Decimal("0")
        currency = sov_item.currency or (contract.currency if contract else "CAD")

        # rc5 Phase 2 / rc6: Explicit ChangeOrder allocations to SOV items.
        # AdjustedContractValue = base + approved change orders allocated to
        # this specific SOV item. Proportional spreading across unrelated SOV
        # items is strictly disallowed.
        #
        # rc6: The previous single-SOV fallback (auto-applying all approved
        # contract COs when the contract has exactly one SOV item) is removed.
        # For a financial-control system, implied allocation hides the
        # allocation decision inside arithmetic. Change orders must be
        # allocated explicitly via change_order_allocations; unallocated COs
        # do NOT adjust the SOV item's value, even for single-item contracts.
        # If a caller wants the single-SOV convenience, they must materialize
        # it as an explicit rule-generated allocation record.
        adjusted = base
        if contract is not None:
            allocated_co = repos.change_orders.approved_allocated_amount_for_sov_item(
                scope=scope, sov_item_id=sov_item_id
            )
            if allocated_co > 0:
                adjusted = base + allocated_co

        # VerifiedPercentComplete — UNAVAILABLE (None) when no confirmation exists.
        pct = work_service.verified_percent_complete(scope=scope, sov_item_id=sov_item_id)
        if pct is None:
            earned = None
        else:
            pct_dec = Decimal(str(pct))
            earned = (adjusted * pct_dec) / Decimal("100")

        prior = repos.invoice_allocations.prior_approved_for_sov_item(
            scope=scope, sov_item_id=sov_item_id, exclude_invoice_id=invoice_id,
        )
        invoice_amount = _dec(alloc.amount) or Decimal("0")

        if earned is None:
            # No verified completion → cannot establish a billable ceiling.
            current_billable = None
            retainage = None
            item_overbilled = True  # cannot prove the invoice is within earned value
        else:
            retainage = (earned * retainage_pct) / Decimal("100")
            current_billable = earned - prior - retainage
            item_overbilled = invoice_amount > current_billable

        if item_overbilled:
            overbilled = True

        per_item.append(ProgressBillingResult(
            sov_item_id=str(sov_item_id), adjusted_contract_value=adjusted,
            verified_percent_complete=(Decimal(str(pct)) if pct is not None else None),
            earned_value=(earned if earned is not None else Decimal("0")),
            previously_approved_billing=prior,
            retainage=(retainage if retainage is not None else Decimal("0")),
            current_billable=(current_billable if current_billable is not None else Decimal("0")),
            invoice_amount=invoice_amount, overbilled=item_overbilled, currency=currency,
        ))

    return ProgressBillingOutcome(per_item=per_item, overbilled=overbilled, evaluated=True)


def _dec(value) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))
