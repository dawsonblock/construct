"""v0.4.2 — verification integrity: tri-state, tax arithmetic, currency (items 9, 10, 11).

Pure unit tests over `verify_invoice` — no database. The invariant they encode:

    MissingRequiredEvidence !=> PASS

A check that cannot be evaluated is UNAVAILABLE, never a silent pass. Money is
compared with Decimal precision; currency must match.
"""
from __future__ import annotations

from construction_ai.domain.models import CheckStatus, Invoice, PurchaseOrder, Quote
from construction_ai.verification.invoice import verify_invoice

ORG = "ORG-1"


def _invoice(**over) -> Invoice:
    base = dict(invoice_number="8831", vendor_name="ABC Electric", total=4760, subtotal=4533.33, tax=226.67,
                po_number="1042-17", quote_number="Q-8821", project_id="PRJ-0042", vendor_company_id="COMP-9")
    base.update(over)
    return Invoice("INV-1", ORG, **base)


def _po(**over) -> PurchaseOrder:
    base = dict(po_number="1042-17", project_id="PRJ-0042", vendor_company_id="COMP-9", amount=4760)
    base.update(over)
    return PurchaseOrder("PO-1", ORG, **base)


def _quote(**over) -> Quote:
    base = dict(quote_number="Q-8821", project_id="PRJ-0042", vendor_company_id="COMP-9", amount=4760, approved=True)
    base.update(over)
    return Quote("Q-1", ORG, **base)


# --------------------------------------------------------------------------
# Tri-state: checks are statuses, not booleans; missing data never passes
# --------------------------------------------------------------------------


def test_a_fully_corroborated_invoice_passes_every_check():
    result = verify_invoice(_invoice(), _po(), _quote(), duplicate=False, work_confirmed=True, require_vendor_identity=True)
    assert result.passed
    assert all(status == CheckStatus.PASS for status in result.checks.values())
    assert "currency_match" in result.checks


def test_missing_subtotal_or_tax_is_unavailable_not_pass():
    result = verify_invoice(_invoice(subtotal=None, tax=None), _po(), _quote(), duplicate=False, work_confirmed=True)
    assert result.checks["tax_math"] == CheckStatus.UNAVAILABLE
    assert not result.passed
    assert "TAX_MISMATCH" in result.exceptions  # non-PASS contributes an exception


def test_wrong_tax_math_fails():
    result = verify_invoice(_invoice(subtotal=4000, tax=100, total=4760), _po(), _quote(), duplicate=False, work_confirmed=True)
    assert result.checks["tax_math"] == CheckStatus.FAIL
    assert not result.passed


def test_unconfirmed_work_is_unavailable_not_pass():
    result = verify_invoice(_invoice(), _po(), _quote(), duplicate=False, work_confirmed=False)
    assert result.checks["work_confirmed"] == CheckStatus.UNAVAILABLE
    assert not result.passed
    assert "WORK_NOT_CONFIRMED" in result.exceptions


def test_no_purchase_order_makes_dependent_checks_unavailable():
    result = verify_invoice(_invoice(), None, None, duplicate=False, work_confirmed=True, require_vendor_identity=True)
    assert result.checks["vendor_match"] == CheckStatus.UNAVAILABLE
    assert result.checks["amount_match"] == CheckStatus.UNAVAILABLE
    assert result.checks["currency_match"] == CheckStatus.UNAVAILABLE
    assert not result.passed


def test_vendor_match_fails_when_invoice_resolved_but_po_supplier_did_not():
    """require_vendor_identity: invoice has a vendor, the PO's supplier did not resolve locally."""
    result = verify_invoice(_invoice(), _po(vendor_company_id=None), _quote(), duplicate=False, work_confirmed=True, require_vendor_identity=True)
    assert result.checks["vendor_match"] == CheckStatus.FAIL
    assert not result.passed


# --------------------------------------------------------------------------
# Item 11: currency is an explicit invariant
# --------------------------------------------------------------------------


def test_currency_mismatch_fails_even_for_matching_amounts():
    """100 CAD is not 100 USD; a currency mismatch fails closed."""
    result = verify_invoice(_invoice(currency="USD", total=100, subtotal=90, tax=10),
                            _po(amount=100, currency="CAD"), None, duplicate=False, work_confirmed=True)
    assert result.checks["currency_match"] == CheckStatus.FAIL
    assert "CURRENCY_MISMATCH" in result.exceptions
    assert not result.passed


def test_currency_match_passes_when_both_cad():
    result = verify_invoice(_invoice(currency="CAD"), _po(currency="CAD"), _quote(currency="CAD"), duplicate=False, work_confirmed=True, require_vendor_identity=True)
    assert result.checks["currency_match"] == CheckStatus.PASS


# --------------------------------------------------------------------------
# Item 9: provenance — every check carries observed/expected/verifier_version
# --------------------------------------------------------------------------


def test_every_check_carries_observed_expected_and_verifier_version():
    result = verify_invoice(_invoice(), _po(), _quote(), duplicate=False, work_confirmed=True, require_vendor_identity=True)
    for name, detail in result.check_details.items():
        assert detail["verifier_version"] == "2"
        assert "observed" in detail and "expected" in detail
        assert detail["status"] == result.checks[name].value


def test_amount_match_is_unavailable_when_total_missing():
    result = verify_invoice(_invoice(total=None), _po(), _quote(), duplicate=False, work_confirmed=True)
    assert result.checks["amount_match"] == CheckStatus.UNAVAILABLE
    assert result.checks["tax_math"] == CheckStatus.UNAVAILABLE
    assert not result.passed
