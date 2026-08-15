"""Phases 15, 16, 17 — schedule of values, scope-bound work confirmation, and
quantitative progress billing.

These prove the invariants:

    InvoicePayment ⇒ WorkEvidenceMatchesBilledScope
    (electrical work confirmed does NOT satisfy a roofing invoice)

    InvoiceCurrentAmount > CurrentBillable ⇒ OVERBILLING / REVIEW_REQUIRED

where CurrentBillable = EarnedValue - PreviouslyApprovedBilling - Retainage and
EarnedValue = AdjustedContractValue × VerifiedPercentComplete.

Needs a real PostgreSQL as the non-superuser app role.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from construction_ai.domain.models import CheckStatus
from construction_ai.verification.invoice import verify_invoice
from construction_ai.verification.progress_billing import evaluate_progress_billing
from construction_ai.work.confirmation import WorkConfirmationService


# --------------------------------------------------------------------------
# Helpers — build a contract → SOV item → allocation hierarchy in one tenant.
# --------------------------------------------------------------------------

def _make_contract(repos, scope, project_id, company_id, *, base=10000, reference="CON-001"):
    return repos.contracts.create(
        scope=scope, project_id=project_id, company_id=company_id,
        reference=reference, name="Electrical Contract", base_contract_value=base, currency="CAD",
    )


def _make_sov_item(repos, scope, contract, *, base=10000, reference="SOV-001", name="Electrical rough-in"):
    return repos.sov_items.create(
        scope=scope, contract_id=UUID(contract.contract_id), reference=reference,
        name=name, base_value=base, currency="CAD", sort_order=1,
    )


def _make_invoice(repos, scope, *, total=4000, reference="INV-SOV-1", number="SOV-1"):
    return repos.invoices.create(
        scope=scope, reference=reference, invoice_number=number, vendor_name="ABC Electric",
        total=total, subtotal=total, tax=0, currency="CAD", created_by="test",
    )


# --------------------------------------------------------------------------
# Phase 17: schedule-of-values repository CRUD + tenant isolation
# --------------------------------------------------------------------------

def test_contract_and_sov_item_round_trip(repos, org_a):
    scope = org_a["scope"]
    project_id = UUID(org_a["project"].project_id)
    company_id = UUID(org_a["company"].company_id)
    contract = _make_contract(repos, scope, project_id, company_id, base=Decimal("12000"))
    sov = _make_sov_item(repos, scope, contract, base=Decimal("12000"))

    assert repos.contracts.get(scope=scope, contract_id=UUID(contract.contract_id)) is not None
    assert repos.sov_items.get(scope=scope, sov_item_id=UUID(sov.sov_item_id)) is not None
    assert Decimal(sov.base_value) == Decimal("12000")
    # for_contract lists the item.
    items = repos.sov_items.for_contract(scope=scope, contract_id=UUID(contract.contract_id))
    assert len(items) == 1


def test_sov_records_are_tenant_isolated(repos, org_a, org_b):
    scope_a = org_a["scope"]
    project_id_a = UUID(org_a["project"].project_id)
    company_id_a = UUID(org_a["company"].company_id)
    _make_contract(repos, scope_a, project_id_a, company_id_a)

    # Tenant B cannot see tenant A's contract (RLS).
    assert repos.contracts.for_project(scope=org_b["scope"], project_id=UUID(org_b["project"].project_id)) == []


def test_change_orders_adjust_contract_value(repos, org_a):
    scope = org_a["scope"]
    project_id = UUID(org_a["project"].project_id)
    company_id = UUID(org_a["company"].company_id)
    contract = _make_contract(repos, scope, project_id, company_id, base=Decimal("10000"))
    repos.change_orders.create(
        scope=scope, contract_id=UUID(contract.contract_id), reference="CO-1",
        amount=Decimal("2500"), currency="CAD", status="approved",
    )
    repos.change_orders.create(
        scope=scope, contract_id=UUID(contract.contract_id), reference="CO-2",
        amount=Decimal("-500"), currency="CAD", status="approved",
    )
    cos = repos.change_orders.approved_for_contract(scope=scope, contract_id=UUID(contract.contract_id))
    amounts = sorted(Decimal(co.amount) for co in cos)
    assert amounts == [Decimal("-500"), Decimal("2500")]


# --------------------------------------------------------------------------
# Phase 15: scope-specific work confirmation
# --------------------------------------------------------------------------

def test_electrical_confirmation_does_not_satisfy_roofing_scope(repos, org_a):
    """The core Phase 15 invariant: confirming electrical work must not satisfy
    a roofing invoice's scope-bound work check."""
    scope = org_a["scope"]
    project_id = UUID(org_a["project"].project_id)
    company_id = UUID(org_a["company"].company_id)
    contract = _make_contract(repos, scope, project_id, company_id, base=Decimal("20000"))
    electrical = _make_sov_item(repos, scope, contract, base=Decimal("12000"), reference="SOV-ELEC", name="Electrical")
    roofing = repos.sov_items.create(
        scope=scope, contract_id=UUID(contract.contract_id), reference="SOV-ROOF",
        name="Roofing", base_value=Decimal("8000"), currency="CAD", sort_order=2,
    )

    # Confirm only the electrical scope.
    repos.work_confirmations.record(
        scope=scope, project_id=project_id, sov_item_id=UUID(electrical.sov_item_id),
        confirmation_type="superintendent", percent_complete=100.0,
    )

    svc = WorkConfirmationService(repos.work_confirmations)
    # Electrical scope is confirmed.
    assert svc.is_work_confirmed_for_sov_items(scope=scope, sov_item_ids=[UUID(electrical.sov_item_id)]) is True
    # Roofing scope is NOT confirmed.
    assert svc.is_work_confirmed_for_sov_items(scope=scope, sov_item_ids=[UUID(roofing.sov_item_id)]) is False
    # An invoice billing roofing must fail the scope check even though electrical
    # is confirmed.
    assert svc.is_work_confirmed_for_sov_items(
        scope=scope, sov_item_ids=[UUID(roofing.sov_item_id)]
    ) is False


def test_work_scope_match_check_fails_when_scope_unconfirmed(repos, org_a):
    """verify_invoice: an invoice billing a SOV scope with no confirmation fails
    work_scope_match, even if project-level work_confirmed is True."""
    scope = org_a["scope"]
    project_id = UUID(org_a["project"].project_id)
    company_id = UUID(org_a["company"].company_id)
    contract = _make_contract(repos, scope, project_id, company_id, base=Decimal("10000"))
    sov = _make_sov_item(repos, scope, contract, base=Decimal("10000"))
    invoice = _make_invoice(repos, scope, total=4000)
    repos.invoice_allocations.create(
        scope=scope, invoice_id=UUID(invoice.invoice_id), sov_item_id=UUID(sov.sov_item_id),
        amount=Decimal("4000"), currency="CAD",
    )

    result = verify_invoice(
        invoice, po=None, quote=None, duplicate=False, work_confirmed=True,
        work_scope_confirmed=False, progress_billing_overbilled=False,
    )
    assert result.checks["work_scope_match"] == CheckStatus.FAIL
    assert "WORK_SCOPE_MISMATCH" in result.exceptions
    assert not result.passed


def test_work_scope_match_passes_when_scope_confirmed(repos, org_a):
    scope = org_a["scope"]
    project_id = UUID(org_a["project"].project_id)
    company_id = UUID(org_a["company"].company_id)
    contract = _make_contract(repos, scope, project_id, company_id, base=Decimal("10000"))
    sov = _make_sov_item(repos, scope, contract, base=Decimal("10000"))
    invoice = _make_invoice(repos, scope, total=4000)
    repos.invoice_allocations.create(
        scope=scope, invoice_id=UUID(invoice.invoice_id), sov_item_id=UUID(sov.sov_item_id),
        amount=Decimal("4000"), currency="CAD",
    )

    result = verify_invoice(
        invoice, po=None, quote=None, duplicate=False, work_confirmed=True,
        work_scope_confirmed=True, progress_billing_overbilled=False,
    )
    assert result.checks["work_scope_match"] == CheckStatus.PASS
    assert "WORK_SCOPE_MISMATCH" not in result.exceptions


# --------------------------------------------------------------------------
# Phase 16: quantitative progress billing / overbilling
# --------------------------------------------------------------------------

def test_progress_billing_within_earned_value_passes(repos, org_a):
    scope = org_a["scope"]
    project_id = UUID(org_a["project"].project_id)
    company_id = UUID(org_a["company"].company_id)
    contract = _make_contract(repos, scope, project_id, company_id, base=Decimal("10000"))
    sov = _make_sov_item(repos, scope, contract, base=Decimal("10000"))
    invoice = _make_invoice(repos, scope, total=Decimal("4500"))
    repos.invoice_allocations.create(
        scope=scope, invoice_id=UUID(invoice.invoice_id), sov_item_id=UUID(sov.sov_item_id),
        amount=Decimal("4500"), currency="CAD",
    )
    # 50% complete → earned 5000, retainage 10% → 500, current billable 4500.
    repos.work_confirmations.record(
        scope=scope, project_id=project_id, sov_item_id=UUID(sov.sov_item_id),
        confirmation_type="superintendent", percent_complete=50.0,
    )

    outcome = evaluate_progress_billing(repos, scope=scope, invoice_id=UUID(invoice.invoice_id))
    assert outcome.evaluated is True
    item = outcome.per_item[0]
    assert item.earned_value == Decimal("5000")
    assert item.retainage == Decimal("500")
    assert item.current_billable == Decimal("4500")
    assert item.overbilled is False
    assert outcome.overbilled is False


def test_progress_billing_overbilled_triggers_review(repos, org_a):
    scope = org_a["scope"]
    project_id = UUID(org_a["project"].project_id)
    company_id = UUID(org_a["company"].company_id)
    contract = _make_contract(repos, scope, project_id, company_id, base=Decimal("10000"))
    sov = _make_sov_item(repos, scope, contract, base=Decimal("10000"))
    invoice = _make_invoice(repos, scope, total=Decimal("5000"))
    repos.invoice_allocations.create(
        scope=scope, invoice_id=UUID(invoice.invoice_id), sov_item_id=UUID(sov.sov_item_id),
        amount=Decimal("5000"), currency="CAD",
    )
    # 50% complete → earned 5000, retainage 500 → current billable 4500.
    # Invoicing 5000 > 4500 → overbilled.
    repos.work_confirmations.record(
        scope=scope, project_id=project_id, sov_item_id=UUID(sov.sov_item_id),
        confirmation_type="superintendent", percent_complete=50.0,
    )

    outcome = evaluate_progress_billing(repos, scope=scope, invoice_id=UUID(invoice.invoice_id))
    assert outcome.overbilled is True
    assert outcome.per_item[0].overbilled is True

    result = verify_invoice(
        invoice, po=None, quote=None, duplicate=False, work_confirmed=True,
        work_scope_confirmed=True, progress_billing_overbilled=True,
    )
    assert result.checks["progress_billing"] == CheckStatus.REVIEW_REQUIRED
    assert "OVERBILLING" in result.exceptions
    assert not result.passed


def test_progress_billing_subtracts_previously_approved_billing(repos, org_a):
    """CurrentBillable = EarnedValue - PreviouslyApprovedBilling - Retainage."""
    scope = org_a["scope"]
    project_id = UUID(org_a["project"].project_id)
    company_id = UUID(org_a["company"].company_id)
    contract = _make_contract(repos, scope, project_id, company_id, base=Decimal("10000"))
    sov = _make_sov_item(repos, scope, contract, base=Decimal("10000"))
    # A prior invoice already billed 3000 against this SOV item.
    prior = _make_invoice(repos, scope, total=Decimal("3000"), reference="INV-SOV-PRIOR", number="SOV-PRIOR")
    repos.invoice_allocations.create(
        scope=scope, invoice_id=UUID(prior.invoice_id), sov_item_id=UUID(sov.sov_item_id),
        amount=Decimal("3000"), currency="CAD",
    )
    # Current invoice bills another 1500.
    invoice = _make_invoice(repos, scope, total=Decimal("1500"), reference="INV-SOV-CUR", number="SOV-CUR")
    repos.invoice_allocations.create(
        scope=scope, invoice_id=UUID(invoice.invoice_id), sov_item_id=UUID(sov.sov_item_id),
        amount=Decimal("1500"), currency="CAD",
    )
    # 50% complete → earned 5000, retainage 500, prior 3000 → billable 1500.
    repos.work_confirmations.record(
        scope=scope, project_id=project_id, sov_item_id=UUID(sov.sov_item_id),
        confirmation_type="superintendent", percent_complete=50.0,
    )

    outcome = evaluate_progress_billing(repos, scope=scope, invoice_id=UUID(invoice.invoice_id))
    item = outcome.per_item[0]
    assert item.previously_approved_billing == Decimal("3000")
    assert item.current_billable == Decimal("1500")
    assert item.overbilled is False


def test_progress_billing_change_order_increases_adjusted_contract_value(repos, org_a):
    """AdjustedContractValue = BaseContract + ApprovedChangeOrders (explicitly allocated).

    rc6: The implicit single-SOV fallback is removed. Change orders must be
    explicitly allocated to a SOV item via change_order_allocations for them
    to affect the adjusted contract value. This test now creates an explicit
    allocation.
    """
    scope = org_a["scope"]
    project_id = UUID(org_a["project"].project_id)
    company_id = UUID(org_a["company"].company_id)
    contract = _make_contract(repos, scope, project_id, company_id, base=Decimal("10000"))
    sov = _make_sov_item(repos, scope, contract, base=Decimal("10000"))
    # +2000 approved change order on the contract, explicitly allocated to the SOV item.
    co = repos.change_orders.create(
        scope=scope, contract_id=UUID(contract.contract_id), reference="CO-1",
        amount=Decimal("2000"), currency="CAD", status="approved",
    )
    # rc6: explicit allocation to the SOV item.
    repos.change_orders.allocate(
        scope=scope, change_order_id=UUID(co.change_order_id),
        sov_item_id=UUID(sov.sov_item_id), amount=Decimal("2000"), currency="CAD",
    )
    invoice = _make_invoice(repos, scope, total=Decimal("5400"))
    repos.invoice_allocations.create(
        scope=scope, invoice_id=UUID(invoice.invoice_id), sov_item_id=UUID(sov.sov_item_id),
        amount=Decimal("5400"), currency="CAD",
    )
    # 50% complete → adjusted 12000, earned 6000, retainage 600 → billable 5400.
    repos.work_confirmations.record(
        scope=scope, project_id=project_id, sov_item_id=UUID(sov.sov_item_id),
        confirmation_type="superintendent", percent_complete=50.0,
    )

    outcome = evaluate_progress_billing(repos, scope=scope, invoice_id=UUID(invoice.invoice_id))
    item = outcome.per_item[0]
    assert item.adjusted_contract_value == Decimal("12000")
    assert item.earned_value == Decimal("6000")
    assert item.current_billable == Decimal("5400")
    assert item.overbilled is False


def test_progress_billing_no_allocations_is_not_evaluated(repos, org_a):
    """An invoice with no SOV allocations: progress billing is UNAVAILABLE
    (not PASS), so it cannot falsely green-light the invoice."""
    scope = org_a["scope"]
    invoice = _make_invoice(repos, scope, total=Decimal("4000"))
    outcome = evaluate_progress_billing(repos, scope=scope, invoice_id=UUID(invoice.invoice_id))
    assert outcome.evaluated is False
    assert outcome.per_item == []


def test_progress_billing_no_completion_is_overbilled(repos, org_a):
    """No verified percent complete → cannot prove the invoice is within earned
    value → treated as overbilled (fail closed)."""
    scope = org_a["scope"]
    project_id = UUID(org_a["project"].project_id)
    company_id = UUID(org_a["company"].company_id)
    contract = _make_contract(repos, scope, project_id, company_id, base=Decimal("10000"))
    sov = _make_sov_item(repos, scope, contract, base=Decimal("10000"))
    invoice = _make_invoice(repos, scope, total=Decimal("4000"))
    repos.invoice_allocations.create(
        scope=scope, invoice_id=UUID(invoice.invoice_id), sov_item_id=UUID(sov.sov_item_id),
        amount=Decimal("4000"), currency="CAD",
    )
    # No work confirmation recorded.
    outcome = evaluate_progress_billing(repos, scope=scope, invoice_id=UUID(invoice.invoice_id))
    assert outcome.overbilled is True
    assert outcome.per_item[0].verified_percent_complete is None
