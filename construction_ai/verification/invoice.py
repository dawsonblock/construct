"""Invoice verification — tri-state, Decimal-precise, currency-aware.

v0.4.2 replaces the boolean verifier. The invariant it enforces:

    MissingRequiredEvidence !=> PASS

A check that cannot be evaluated is `UNAVAILABLE`, never `True`. A check that
runs and disagrees is `FAIL`. Only a check with enough evidence that agrees is
`PASS`. `passed` is true only when every required check is `PASS`.

Money is compared with `Decimal` at currency-specific precision (item 10), and
the invoice/PO/quote currency must agree unless a formal conversion workflow is
active (item 11) — there is no automatic FX conversion.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from construction_ai.domain.models import CheckStatus, Invoice, PurchaseOrder, Quote, VerificationResult

# Phase 18: tri-state duplicate status values.
NOT_DUPLICATE = "NOT_DUPLICATE"
POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"
CONFIRMED_DUPLICATE = "CONFIRMED_DUPLICATE"

VERIFIER_VERSION = "2"
#: Per-currency comparison precision. CAD/USD cents; extend as currencies are added.
CURRENCY_PRECISION = {"CAD": Decimal("0.01"), "USD": Decimal("0.01"), "EUR": Decimal("0.01")}
DEFAULT_PRECISION = Decimal("0.01")
_CHECK_EXCEPTION = {
    "vendor_match": "UNKNOWN_OR_MISMATCHED_VENDOR",
    "project_match": "INVALID_PROJECT",
    "po_match": "NO_OR_MISMATCHED_PO",
    "quote_match": "NO_OR_UNAPPROVED_QUOTE",
    "amount_match": "AMOUNT_MISMATCH",
    "tax_math": "TAX_MISMATCH",
    "currency_match": "CURRENCY_MISMATCH",
    "not_duplicate": "DUPLICATE_INVOICE",
    "work_confirmed": "WORK_NOT_CONFIRMED",
    "work_scope_match": "WORK_SCOPE_MISMATCH",  # Phase 15
    "progress_billing": "OVERBILLING",  # Phase 16
}


def _precision(currency: str | None) -> Decimal:
    return CURRENCY_PRECISION.get(currency or "", DEFAULT_PRECISION)


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _amounts_equal(a: Any, b: Any, currency: str | None) -> bool:
    da, db = _to_decimal(a), _to_decimal(b)
    if da is None or db is None:
        return False
    return abs(da - db) <= _precision(currency)


def _check(status: CheckStatus, *, observed: Any = None, expected: Any = None, evidence_ids: list[str] | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "observed": observed,
        "expected": expected,
        "evidence_ids": list(evidence_ids or []),
        "verifier_version": VERIFIER_VERSION,
    }


def verify_invoice(
    invoice: Invoice,
    po: PurchaseOrder | None,
    quote: Quote | None,
    *,
    duplicate: bool,
    work_confirmed: bool,
    require_vendor_identity: bool = False,
    work_scope_confirmed: bool | None = None,
    progress_billing_overbilled: bool | None = None,
    duplicate_status: str | None = None,
) -> VerificationResult:
    """Run every required check and return a tri-state result.

    `work_confirmed` is retained as a parameter for v0.4.2; v0.4.2 sub-step 4
    replaces it with `work_confirmations` records so a caller cannot declare
    physical completion. Until then, an unconfirmed job reads UNAVAILABLE, not
    PASS.

    v0.5.0-rc3 (Phase 15/16): two new checks are evaluated when the caller
    supplies SOV-derived inputs (the pipeline computes them from persisted
    records; pure unit callers may omit them):

      - work_scope_match: when an invoice allocates to SOV items, work must be
        confirmed for those specific scopes. UNAVAILABLE when no SOV allocations.
      - progress_billing: quantitative overbilling check. PASS when within the
        earned-value ceiling; REVIEW_REQUIRED when overbilled; UNAVAILABLE when
        no SOV allocations exist to evaluate against.
    """
    checks: dict[str, CheckStatus] = {}
    details: dict[str, dict[str, Any]] = {}

    # vendor_match — independent resolution (item 6). The invoice's resolved
    # company against the PO's independently-resolved company.
    if po is None:
        status = CheckStatus.UNAVAILABLE
    elif require_vendor_identity:
        if invoice.vendor_company_id is None:
            status = CheckStatus.UNAVAILABLE  # invoice vendor not resolved
        elif po.vendor_company_id is None:
            status = CheckStatus.FAIL  # ERP supplier did not resolve locally
        elif str(invoice.vendor_company_id) == str(po.vendor_company_id):
            status = CheckStatus.PASS
        else:
            status = CheckStatus.FAIL
    else:
        status = CheckStatus.PASS if (not invoice.vendor_company_id or str(invoice.vendor_company_id) == str(po.vendor_company_id)) else CheckStatus.FAIL
    checks["vendor_match"] = status
    details["vendor_match"] = _check(status, observed=invoice.vendor_company_id, expected=po.vendor_company_id if po else None)

    # project_match
    if po is None or invoice.project_id is None:
        status = CheckStatus.UNAVAILABLE
    else:
        status = CheckStatus.PASS if str(po.project_id) == str(invoice.project_id) else CheckStatus.FAIL
    checks["project_match"] = status
    details["project_match"] = _check(status, observed=invoice.project_id, expected=po.project_id if po else None)

    # po_match
    if not invoice.po_number:
        status = CheckStatus.UNAVAILABLE
    elif po is None:
        status = CheckStatus.FAIL
    else:
        status = CheckStatus.PASS if invoice.po_number == po.po_number else CheckStatus.FAIL
    checks["po_match"] = status
    details["po_match"] = _check(status, observed=invoice.po_number, expected=po.po_number if po else None)

    # quote_match
    if not invoice.quote_number:
        status = CheckStatus.UNAVAILABLE
    elif quote is None:
        status = CheckStatus.FAIL
    else:
        status = CheckStatus.PASS if (invoice.quote_number == quote.quote_number and quote.approved) else CheckStatus.FAIL
    checks["quote_match"] = status
    details["quote_match"] = _check(status, observed=invoice.quote_number, expected=quote.quote_number if quote else None)

    # currency_match (item 11) — invoice currency must equal PO currency when a
    # PO exists. No automatic FX conversion.
    currency = invoice.currency or "CAD"
    if po is None:
        status = CheckStatus.UNAVAILABLE
    elif po.currency and po.currency != currency:
        status = CheckStatus.FAIL
    else:
        status = CheckStatus.PASS
    checks["currency_match"] = status
    details["currency_match"] = _check(status, observed=currency, expected=getattr(po, "currency", None) if po else None)

    # amount_match — Decimal, currency-precise. invoice total vs PO amount (and
    # quote amount when a quote is present).
    if po is None or invoice.total is None or po.amount is None:
        status = CheckStatus.UNAVAILABLE
    elif not _amounts_equal(invoice.total, po.amount, currency):
        status = CheckStatus.FAIL
    elif quote is not None and quote.amount is not None and not _amounts_equal(invoice.total, quote.amount, currency):
        status = CheckStatus.FAIL
    else:
        status = CheckStatus.PASS
    checks["amount_match"] = status
    details["amount_match"] = _check(status, observed=invoice.total, expected=po.amount if po else None)

    # tax_math (item 10) — Decimal. Missing subtotal/tax is UNAVAILABLE, not PASS.
    subtotal, tax, total = _to_decimal(invoice.subtotal), _to_decimal(invoice.tax), _to_decimal(invoice.total)
    if subtotal is None or tax is None or total is None:
        status = CheckStatus.UNAVAILABLE
    else:
        # CalculatedTotal = Subtotal + Tax + Shipping + OtherCharges - Discount.
        # Shipping/discount are not modeled yet; subtotal + tax is the current basis.
        status = CheckStatus.PASS if (subtotal + tax) == total else CheckStatus.FAIL
    checks["tax_math"] = status
    details["tax_math"] = _check(status, observed=(invoice.subtotal, invoice.tax), expected=invoice.total)

    # not_duplicate (Phase 18) — tri-state. A caller may pass duplicate_status
    # (NOT_DUPLICATE / POSSIBLE_DUPLICATE / CONFIRMED_DUPLICATE) computed by the
    # duplicate detection service; otherwise the legacy boolean is mapped:
    # True → CONFIRMED_DUPLICATE, False → NOT_DUPLICATE. A possible duplicate is
    # REVIEW_REQUIRED (HOLD), not an automatic rejection; a confirmed duplicate
    # is a hard FAIL.
    if duplicate_status is None:
        duplicate_status = CONFIRMED_DUPLICATE if duplicate else NOT_DUPLICATE
    if duplicate_status == NOT_DUPLICATE:
        checks["not_duplicate"] = CheckStatus.PASS
    elif duplicate_status == POSSIBLE_DUPLICATE:
        checks["not_duplicate"] = CheckStatus.REVIEW_REQUIRED
    else:
        checks["not_duplicate"] = CheckStatus.FAIL
    details["not_duplicate"] = _check(checks["not_duplicate"], observed=duplicate_status, expected=NOT_DUPLICATE)

    # work_confirmed — UNAVAILABLE when not confirmed (item 12 will source this
    # from work_confirmations records; a caller cannot declare completion).
    checks["work_confirmed"] = CheckStatus.PASS if work_confirmed else CheckStatus.UNAVAILABLE
    details["work_confirmed"] = _check(checks["work_confirmed"], observed=work_confirmed, expected=True)

    # work_scope_match (Phase 15) — scope-specific work confirmation. Only
    # evaluated when the caller supplies SOV-derived state (None = no SOV
    # allocations, so this check does not apply and the project-level
    # work_confirmed check above governs). When the invoice bills a specific
    # scope, work must be confirmed for that exact scope.
    if work_scope_confirmed is not None:
        checks["work_scope_match"] = CheckStatus.PASS if work_scope_confirmed else CheckStatus.FAIL
        details["work_scope_match"] = _check(
            checks["work_scope_match"], observed=work_scope_confirmed, expected=True,
        )

    # progress_billing (Phase 16) — quantitative overbilling. Only evaluated
    # when SOV allocations exist (None = not applicable). True = overbilled.
    if progress_billing_overbilled is not None:
        checks["progress_billing"] = (
            CheckStatus.REVIEW_REQUIRED if progress_billing_overbilled else CheckStatus.PASS
        )
        details["progress_billing"] = _check(
            checks["progress_billing"], observed=progress_billing_overbilled, expected=False,
        )

    exceptions = [_CHECK_EXCEPTION[name] for name, status in checks.items() if status != CheckStatus.PASS]
    return VerificationResult(invoice.invoice_id, checks, exceptions, [], details)
