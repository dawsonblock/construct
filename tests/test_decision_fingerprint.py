"""Phase 6 — decision fingerprint composition.

Proves the invariant:

    F = H(InvoiceSnapshot ∥ VerificationPacket ∥ EvidenceSet
           ∥ PolicyVersion ∥ ApprovalRequirements)

The decision fingerprint binds the EXACT state used for one approval decision,
so unrelated project changes (another invoice's evidence) do NOT produce a false
stale positive, while a change to THIS invoice's state DOES.

Needs a real PostgreSQL as the non-superuser app role.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from construction_ai.approvals.decision_fingerprint import (
    compute_decision_fingerprint,
    compute_decision_fingerprint_for_approval,
    invoice_snapshot,
)
from construction_ai.approvals.service import decide_approval
from construction_ai.auth.identity import DevIdentityProvider
from construction_ai.auth.provisioning import provision_approver
from construction_ai.auth.sessions import actor_from_session, login
from construction_ai.domain.models import Approval, Evidence, Invoice
from construction_ai.executive.executor import ApprovalStale, check_approval_staleness
from construction_ai.executive.invoice_pipeline import InvoicePipeline


def _approve_invoice(repos, org_a):
    """Create an invoice with a project, approve it, return (scope, approval)."""
    scope = org_a["scope"]
    project = repos.projects.create(scope=scope, reference="P-DFP-01", name="DFP Test")
    invoice = Invoice(
        invoice_id="", organization_id="", reference="", invoice_number="INV-DFP-001",
        vendor_name="Vendor", total=Decimal("100.00"), subtotal=Decimal("87.00"),
        tax=Decimal("13.00"), currency="CAD",
    )
    pipeline = InvoicePipeline(repositories=repos, erp_resolver=None)
    result = pipeline.process(scope=scope, extracted=invoice, signals={"project_id": project.project_id})
    approval_id = UUID(result["approval_id"])

    provision_approver(
        repos, scope.organization_only, subject="approver@dfp.com", display_name="Approver",
        role="approver",
        permissions=["invoice.read", "invoice.review", "invoice.approve", "invoice.hold", "invoice.reject"],
        maximum_amount=10000.0,
    )
    session = login(repos, provider=DevIdentityProvider(), credential="approver@dfp.com")
    actor = actor_from_session(repos, session)
    decide_approval(repos, actor=actor, approval_id=approval_id, decision="approved", reason="test")
    approval = repos.approvals.get(scope=scope, approval_id=approval_id)
    return scope, approval


# --------------------------------------------------------------------------
# Composition: the five components
# --------------------------------------------------------------------------

def test_invoice_snapshot_captures_financial_fields():
    inv = Invoice(
        invoice_id="I1", organization_id="O1", reference="R1", invoice_number="INV-1",
        vendor_name="ABC", total=Decimal("100.00"), subtotal=Decimal("87.00"),
        tax=Decimal("13.00"), currency="CAD", po_number="PO-1",
    )
    snap = invoice_snapshot(inv)
    assert snap["invoice_number"] == "INV-1"
    assert snap["total"] == "100.00"
    assert snap["currency"] == "CAD"
    assert snap["po_number"] == "PO-1"


def test_decision_fingerprint_is_deterministic():
    inv = Invoice(
        invoice_id="I1", organization_id="O1", reference="R1", invoice_number="INV-1",
        vendor_name="ABC", total=Decimal("100.00"), subtotal=Decimal("87.00"),
        tax=Decimal("13.00"), currency="CAD",
    )
    approval = Approval(
        approval_id="A1", organization_id="O1", type="PURCHASE_INVOICE",
        subject_id="I1", recommended_action="APPROVE", amount=100.0, currency="CAD",
        quorum_threshold=1,
    )
    evidence = [Evidence(
        evidence_id="E1", source_type="erpnext", source_id="S1", field="ERP_PO",
        value={}, confidence=1.0, observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )]
    fp1 = compute_decision_fingerprint(
        invoice=inv, evidence=evidence, approval=approval, verification_packet_hash="hash123",
    )
    fp2 = compute_decision_fingerprint(
        invoice=inv, evidence=evidence, approval=approval, verification_packet_hash="hash123",
    )
    assert fp1 == fp2
    assert len(fp1) == 64


def test_decision_fingerprint_changes_when_invoice_amount_changes():
    inv1 = Invoice(
        invoice_id="I1", organization_id="O1", reference="R1", invoice_number="INV-1",
        vendor_name="ABC", total=Decimal("100.00"), subtotal=Decimal("87.00"),
        tax=Decimal("13.00"), currency="CAD",
    )
    inv2 = Invoice(
        invoice_id="I1", organization_id="O1", reference="R1", invoice_number="INV-1",
        vendor_name="ABC", total=Decimal("200.00"), subtotal=Decimal("174.00"),
        tax=Decimal("26.00"), currency="CAD",
    )
    approval = Approval(
        approval_id="A1", organization_id="O1", type="PURCHASE_INVOICE",
        subject_id="I1", recommended_action="APPROVE", amount=100.0, currency="CAD",
    )
    fp1 = compute_decision_fingerprint(invoice=inv1, evidence=[], approval=approval, verification_packet_hash=None)
    fp2 = compute_decision_fingerprint(invoice=inv2, evidence=[], approval=approval, verification_packet_hash=None)
    assert fp1 != fp2


def test_decision_fingerprint_changes_when_evidence_set_changes():
    inv = Invoice(
        invoice_id="I1", organization_id="O1", reference="R1", invoice_number="INV-1",
        vendor_name="ABC", total=Decimal("100.00"), subtotal=Decimal("87.00"),
        tax=Decimal("13.00"), currency="CAD",
    )
    approval = Approval(
        approval_id="A1", organization_id="O1", type="PURCHASE_INVOICE",
        subject_id="I1", recommended_action="APPROVE", amount=100.0, currency="CAD",
    )
    ev1 = [Evidence(evidence_id="E1", source_type="erpnext", source_id="S1", field="ERP_PO", value={}, confidence=1.0, observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc))]
    ev2 = [Evidence(evidence_id="E2", source_type="erpnext", source_id="S2", field="ERP_PO", value={}, confidence=1.0, observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc))]
    fp1 = compute_decision_fingerprint(invoice=inv, evidence=ev1, approval=approval, verification_packet_hash=None)
    fp2 = compute_decision_fingerprint(invoice=inv, evidence=ev2, approval=approval, verification_packet_hash=None)
    assert fp1 != fp2


def test_decision_fingerprint_changes_when_verification_packet_hash_changes():
    inv = Invoice(
        invoice_id="I1", organization_id="O1", reference="R1", invoice_number="INV-1",
        vendor_name="ABC", total=Decimal("100.00"), subtotal=Decimal("87.00"),
        tax=Decimal("13.00"), currency="CAD",
    )
    approval = Approval(
        approval_id="A1", organization_id="O1", type="PURCHASE_INVOICE",
        subject_id="I1", recommended_action="APPROVE", amount=100.0, currency="CAD",
    )
    fp1 = compute_decision_fingerprint(invoice=inv, evidence=[], approval=approval, verification_packet_hash="hash_a")
    fp2 = compute_decision_fingerprint(invoice=inv, evidence=[], approval=approval, verification_packet_hash="hash_b")
    assert fp1 != fp2


def test_decision_fingerprint_includes_policy_version():
    inv = Invoice(
        invoice_id="I1", organization_id="O1", reference="R1", invoice_number="INV-1",
        vendor_name="ABC", total=Decimal("100.00"), subtotal=Decimal("87.00"),
        tax=Decimal("13.00"), currency="CAD",
    )
    approval = Approval(
        approval_id="A1", organization_id="O1", type="PURCHASE_INVOICE",
        subject_id="I1", recommended_action="APPROVE", amount=100.0, currency="CAD",
    )
    fp1 = compute_decision_fingerprint(invoice=inv, evidence=[], approval=approval, verification_packet_hash=None, policy_version="v1")
    fp2 = compute_decision_fingerprint(invoice=inv, evidence=[], approval=approval, verification_packet_hash=None, policy_version="v2")
    assert fp1 != fp2


# --------------------------------------------------------------------------
# Integration: the approval service stores the decision fingerprint atomically
# --------------------------------------------------------------------------

def test_decision_fingerprint_is_stored_at_approval_time(repos, org_a):
    scope, approval = _approve_invoice(repos, org_a)
    assert approval.decision_fingerprint is not None
    assert len(approval.decision_fingerprint) == 64


def test_decision_fingerprint_recomputed_matches_stored(repos, org_a):
    """Recomputing the decision fingerprint from current state matches the stored
    one when nothing has changed."""
    scope, approval = _approve_invoice(repos, org_a)
    current = compute_decision_fingerprint_for_approval(repos, scope=scope, approval=approval)
    assert current == approval.decision_fingerprint


def test_decision_fingerprint_detects_invoice_amount_drift(repos, org_a):
    """Changing the invoice's total after approval makes the decision fingerprint
    stale — the human approved a different amount than we'd execute against.

    Both the broad state fingerprint and the decision fingerprint detect this;
    the broad check fires first in check_approval_staleness. We verify the
    decision fingerprint specifically via direct recomputation."""
    scope, approval = _approve_invoice(repos, org_a)
    # Change the invoice total.
    with repos.db.scoped(scope) as cur:
        cur.execute(
            "UPDATE invoices SET total = 999.99 WHERE organization_id = %s AND invoice_id = %s",
            (scope.organization_id, UUID(approval.subject_id)),
        )
    approval = repos.approvals.get(scope=scope, approval_id=UUID(approval.approval_id))
    # The decision fingerprint recomputation detects the drift directly.
    current = compute_decision_fingerprint_for_approval(repos, scope=scope, approval=approval)
    assert current != approval.decision_fingerprint
    # The executor refuses (either check fires).
    with pytest.raises(ApprovalStale):
        check_approval_staleness(repos, scope=scope, approval=approval)


def test_decision_fingerprint_detects_evidence_drift(repos, org_a):
    """Adding new evidence to the approval's evidence set makes the decision
    fingerprint stale."""
    scope, approval = _approve_invoice(repos, org_a)
    # Add a new evidence record and attach it to the approval.
    project_scope = scope.for_project(UUID(approval.project_id))
    new_ev = repos.evidence.record(
        scope=project_scope, field="total", value="999.00", confidence=0.9, authority=0.9,
        source_type="erp", source_id="erp_drift",
    )
    with repos.db.scoped(scope) as cur:
        cur.execute(
            "UPDATE approvals SET evidence_ids = evidence_ids || %s::uuid[] "
            "WHERE organization_id = %s AND approval_id = %s",
            ([UUID(new_ev.evidence_id)], scope.organization_id, UUID(approval.approval_id)),
        )
    approval = repos.approvals.get(scope=scope, approval_id=UUID(approval.approval_id))
    current = compute_decision_fingerprint_for_approval(repos, scope=scope, approval=approval)
    assert current != approval.decision_fingerprint
    with pytest.raises(ApprovalStale):
        check_approval_staleness(repos, scope=scope, approval=approval)


def test_unrelated_project_change_does_not_false_positive_decision_fingerprint(repos, org_a):
    """Adding evidence to a DIFFERENT invoice in the same project does NOT change
    THIS approval's decision fingerprint (the key advantage over the project-wide
    state_fingerprint, which DOES change). The decision fingerprint is scoped to
    the approval's own invoice + evidence + packet."""
    scope, approval = _approve_invoice(repos, org_a)
    # Create a second, unrelated invoice in the same project and add evidence to it.
    other_invoice = repos.invoices.create(
        scope=scope, reference="INV-DFP-OTHER", invoice_number="DFP-OTHER",
        vendor_name="Other Vendor", total=500, subtotal=500, tax=0, currency="CAD",
    )
    repos.invoices.assign_project(
        scope=scope, invoice_id=UUID(other_invoice.invoice_id), project_id=UUID(approval.project_id)
    )
    project_scope = scope.for_project(UUID(approval.project_id))
    repos.evidence.record(
        scope=project_scope, field="total", value="999.00", confidence=0.9, authority=0.9,
        source_type="erp", source_id="erp_other",
    )
    # The decision fingerprint for THIS approval should be unchanged.
    current = compute_decision_fingerprint_for_approval(repos, scope=scope, approval=approval)
    assert current == approval.decision_fingerprint
    # NOTE: the project-wide state_fingerprint WOULD change here (and
    # check_approval_staleness would raise the broad stale check). The decision
    # fingerprint itself is stable — that's the point.
