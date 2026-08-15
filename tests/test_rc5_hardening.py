"""v0.5.0-rc5 Hardening Tests.

Tests the 10 production-readiness improvements:
1. Unified recovery reconciliation financial readback (P0).
2. Bounded negative-confirmation for PROVEN_ABSENT.
3. Deterministic REMOTE_DRAFT recovery pipeline.
4. Authoritative ERP supplier ID in recovery daemon.
5. Decision fingerprint binding exact active policy configuration hash.
6. Explicit change order allocations to specific SOV items (no proportional spreading).
7. Authoritative work confirmation supersession (elimination of max()).
8. Elimination of scope-free project-wide work confirmation fallback for payable scope.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from uuid import UUID, uuid4


from construction_ai.approvals.decision_fingerprint import (
    compute_decision_fingerprint,
)
from construction_ai.approvals.policy import ApprovalPolicy
from construction_ai.domain.models import Invoice, Approval
from construction_ai.executive.executor import (
    reconstruct_expected_erp_payload,
)
from construction_ai.executive.recovery_daemon import run_recovery_cycle
from construction_ai.verification.progress_billing import evaluate_progress_billing
from construction_ai.work.confirmation import WorkConfirmationService


class FakeERPTransport:
    """Mock ERP transport supporting read and submit operations."""
    def __init__(self):
        self.documents: dict[str, dict] = {}
        self.search_results: dict[str, list[dict]] = {}
        self.submitted_docs: list[str] = []

    def add_doc(self, name: str, **fields):
        doc = {"name": name, "doctype": "Purchase Invoice", **fields}
        self.documents[name] = doc
        return doc

    def get(self, path: str, params: dict | None = None) -> dict:
        from urllib.parse import unquote
        path = unquote(path)
        if "/api/resource/Purchase Invoice/" in path:
            docname = path.split("/api/resource/Purchase Invoice/")[-1]
            if docname in self.documents:
                return {"data": self.documents[docname]}
            return {"data": {}}

        if path == "/api/resource/Purchase Invoice" and params:
            import json
            filters_str = params.get("filters", "[]")
            try:
                filters = json.loads(filters_str) if isinstance(filters_str, str) else filters_str
                for f in filters:
                    if f[0] == "construct_idempotency_key":
                        key = f"idek:{f[2]}"
                        if key in self.search_results:
                            return {"data": self.search_results[key]}
                    elif f[0] == "bill_no":
                        bill_no = f[2]
                        for f2 in filters:
                            if f2[0] == "supplier":
                                key = f"inv:{bill_no}/{f2[2]}"
                                if key in self.search_results:
                                    return {"data": self.search_results[key]}
            except Exception:
                pass
            return {"data": []}
        return {"data": {}}

    def post(self, path: str, json: dict | None = None) -> dict:
        from urllib.parse import unquote
        path = unquote(path)
        if path == "/api/method/frappe.client.submit" and json:
            docname = json.get("doc", {}).get("name")
            if docname and docname in self.documents:
                self.documents[docname]["docstatus"] = 1
                self.submitted_docs.append(docname)
                return {"message": "submitted", "data": self.documents[docname]}
        return {"data": {}}

    def submit_purchase_invoice(self, docname: str) -> dict:
        if docname in self.documents:
            self.documents[docname]["docstatus"] = 1
            self.submitted_docs.append(docname)
            return {"message": "submitted", "data": self.documents[docname]}
        raise ValueError(f"document {docname} not found")


def _make_invoice(repos, scope, *, total=Decimal("5000"), subtotal=Decimal("4500"), tax=Decimal("500"), currency="CAD", vendor_company_id=None):
    ref = f"INV-REF-{uuid4().hex[:6]}"
    inv_num = f"INV-{uuid4().hex[:6]}"
    invoice = repos.invoices.create(
        scope=scope,
        vendor_name="Acme Roofing",
        invoice_number=inv_num,
        reference=ref,
        total=total,
        subtotal=subtotal,
        tax=tax,
        currency=currency,
    )
    if vendor_company_id:
        with repos.db.scoped(scope) as cur:
            cur.execute(
                "UPDATE invoices SET vendor_company_id = %s WHERE organization_id = %s AND invoice_id = %s",
                (vendor_company_id, scope.organization_id, invoice.invoice_id),
            )
        invoice = repos.invoices.get(scope=scope, invoice_id=invoice.invoice_id)
    return invoice


# ---------------------------------------------------------------------------
# 1. P0: Unified Financial Readback in Recovery Reconciliation
# ---------------------------------------------------------------------------

def test_recovery_reconciliation_catches_financial_mismatch(repos, org_a):
    """Recovery path enforces the exact same canonical financial comparison as the normal executor."""
    scope = org_a["scope"]
    invoice = _make_invoice(repos, scope, total=Decimal("5000.00"), currency="CAD")
    
    # Remote document has mismatched currency (USD instead of CAD)
    transport = FakeERPTransport()
    transport.add_doc(
        "PINV-MISMATCH-1",
        docstatus=1,
        supplier="Acme Roofing",
        bill_no=invoice.invoice_number,
        grand_total="5000.00",
        net_total="4500.00",
        total_taxes="500.00",
        currency="USD",  # Mismatched currency!
    )
    
    action = repos.external_actions.reserve(
        scope=scope,
        operation="erp_invoice_submit",
        idempotency_key=f"rec-test-{uuid4().hex[:6]}",
        subject_type="invoice",
        subject_id=UUID(invoice.invoice_id),
    )
    repos.external_actions.transition(
        scope=scope, action_id=action.action_id, from_status="pending", to_status="unknown",
        remote_document_id="PINV-MISMATCH-1", remote_state="remote_unknown",
    )
    
    # Reconcile with recovery daemon
    res = run_recovery_cycle(repos, scope=scope, erp_read_transport=transport)
    assert str(action.action_id) in res.reconciled_unknowns
    assert str(action.action_id) in res.flagged_ambiguous
    
    reloaded = repos.external_actions.get(scope=scope, action_id=action.action_id)
    assert reloaded.status == "failed_terminal"
    assert reloaded.remote_state == "remote_mismatch"
    assert "currency" in (reloaded.last_error or "")


# ---------------------------------------------------------------------------
# 2. Bounded Negative-Confirmation for PROVEN_ABSENT
# ---------------------------------------------------------------------------

def test_bounded_negative_confirmation_requires_observation_cycles(repos, org_a):
    """An unknown action with 0 search matches remains unknown during observation rounds before PROVEN_ABSENT."""
    scope = org_a["scope"]
    invoice = _make_invoice(repos, scope)
    transport = FakeERPTransport()  # Empty ERP — no matches

    action = repos.external_actions.reserve(
        scope=scope,
        operation="erp_invoice_submit",
        idempotency_key=f"obs-test-{uuid4().hex[:6]}",
        subject_type="invoice",
        subject_id=UUID(invoice.invoice_id),
    )
    repos.external_actions.transition(
        scope=scope, action_id=action.action_id, from_status="pending", to_status="unknown",
        remote_state="remote_unknown",
    )

    # Round 1: recovery daemon uses threshold=2 by default -> remains unknown / observation in progress
    # rc6: window=0 so the time bound does not block the attempt-bound test.
    run_recovery_cycle(repos, scope=scope, erp_read_transport=transport,
                       negative_confirmation_threshold=2, negative_confirmation_window_seconds=0)
    reloaded1 = repos.external_actions.get(scope=scope, action_id=action.action_id)
    assert reloaded1.status == "unknown"
    assert reloaded1.recovery_attempts == 1

    # Round 2: second cycle meets observation threshold -> transitions to failed_retryable / PROVEN_ABSENT
    run_recovery_cycle(repos, scope=scope, erp_read_transport=transport,
                       negative_confirmation_threshold=2, negative_confirmation_window_seconds=0)
    reloaded2 = repos.external_actions.get(scope=scope, action_id=action.action_id)
    assert reloaded2.status == "failed_retryable"
    assert reloaded2.remote_state == "no_remote_effect"
    assert reloaded2.recovery_attempts == 2


# ---------------------------------------------------------------------------
# 3. Deterministic REMOTE_DRAFT Recovery Pipeline
# ---------------------------------------------------------------------------

def test_remote_draft_resumption_and_confirmation(repos, org_a):
    """Recovery daemon verifies draft matches approved intent, submits it, reads back, and confirms."""
    scope = org_a["scope"]
    invoice = _make_invoice(repos, scope, total=Decimal("5000.00"), subtotal=Decimal("4500.00"), tax=Decimal("500.00"))
    
    action = repos.external_actions.reserve(
        scope=scope,
        operation="erp_invoice_submit",
        idempotency_key=f"draft-test-{uuid4().hex[:6]}",
        subject_type="invoice",
        subject_id=UUID(invoice.invoice_id),
    )
    expected = reconstruct_expected_erp_payload(repos, scope=scope, action=action)

    transport = FakeERPTransport()
    transport.add_doc(
        "PINV-DRAFT-RESUME",
        docstatus=0,  # remote draft
        supplier="Acme Roofing",
        bill_no=invoice.invoice_number,
        grand_total="5000.00",
        net_total="4500.00",
        total_taxes="500.00",
        currency="CAD",
        construct_idempotency_key=expected.get("construct_idempotency_key"),
    )

    repos.external_actions.transition(
        scope=scope, action_id=action.action_id, from_status="pending", to_status="unknown",
        remote_document_id="PINV-DRAFT-RESUME", remote_state="remote_draft",
    )

    res = run_recovery_cycle(
        repos, scope=scope, erp_read_transport=transport, erp_write_transport=transport,
    )
    assert str(action.action_id) in res.resolved_drafts

    reloaded = repos.external_actions.get(scope=scope, action_id=action.action_id)
    assert reloaded.status == "confirmed"
    assert reloaded.remote_state == "remote_submitted"
    assert "PINV-DRAFT-RESUME" in transport.submitted_docs


# ---------------------------------------------------------------------------
# 4. Authoritative ERP Supplier ID Resolution in Recovery
# ---------------------------------------------------------------------------

def test_recovery_daemon_uses_authoritative_erp_supplier_id(repos, org_a):
    """Recovery daemon uses company.erp_supplier_id rather than raw vendor name."""
    scope = org_a["scope"]
    company = org_a["company"]
    # Update company with an explicit ERP supplier ID
    with repos.db.scoped(scope) as cur:
        cur.execute(
            "UPDATE companies SET erp_supplier_id = 'SUPP-ERP-8899' WHERE organization_id = %s AND company_id = %s",
            (scope.organization_id, company.company_id),
        )
    invoice = _make_invoice(repos, scope, vendor_company_id=UUID(company.company_id))

    action = repos.external_actions.reserve(
        scope=scope,
        operation="erp_invoice_submit",
        idempotency_key=f"supp-test-{uuid4().hex[:6]}",
        subject_type="invoice",
        subject_id=UUID(invoice.invoice_id),
    )
    
    expected = reconstruct_expected_erp_payload(repos, scope=scope, action=action)
    assert expected["supplier"] == "SUPP-ERP-8899"


# ---------------------------------------------------------------------------
# 5. Bind Decision Fingerprints to Actual Serialized Policy Hash
# ---------------------------------------------------------------------------

def test_decision_fingerprint_changes_with_policy_configuration():
    """Altering approval policy thresholds or authentication strength changes the policy hash and decision fingerprint."""
    invoice = Invoice(
        invoice_id="inv-1", organization_id="org-1", vendor_name="Vendor A",
        invoice_number="INV-1", total=Decimal("10000"), currency="CAD",
    )
    approval = Approval(
        approval_id="app-1", organization_id="org-1", type="invoice",
        subject_id="inv-1", recommended_action="APPROVE", amount=Decimal("10000"), currency="CAD",
    )
    
    policy_standard = ApprovalPolicy(version="authority:v1", dual_approval_threshold=Decimal("50000"))
    policy_strict = ApprovalPolicy(version="authority:v1", dual_approval_threshold=Decimal("5000"))
    
    fp_standard = compute_decision_fingerprint(
        invoice=invoice, evidence=[], approval=approval, verification_packet_hash="hash1", policy=policy_standard,
    )
    fp_strict = compute_decision_fingerprint(
        invoice=invoice, evidence=[], approval=approval, verification_packet_hash="hash1", policy=policy_strict,
    )
    
    assert policy_standard.policy_hash() != policy_strict.policy_hash()
    assert fp_standard != fp_strict


# ---------------------------------------------------------------------------
# 6. Explicit Change Order Allocations (No Proportional Spreading)
# ---------------------------------------------------------------------------

def test_change_orders_are_not_proportionally_spread(repos, org_a):
    """A change order allocated to Item A does NOT increase the adjusted value of Item B."""
    scope = org_a["scope"]
    project_id = UUID(org_a["project"].project_id)
    company_id = UUID(org_a["company"].company_id)
    
    contract = repos.contracts.create(
        scope=scope, project_id=project_id, company_id=company_id,
        reference=f"CON-{uuid4().hex[:4]}", base_contract_value=Decimal("20000"),
    )
    item_electrical = repos.sov_items.create(
        scope=scope, contract_id=UUID(contract.contract_id), reference="SOV-ELEC",
        name="Electrical", base_value=Decimal("10000"),
    )
    item_roofing = repos.sov_items.create(
        scope=scope, contract_id=UUID(contract.contract_id), reference="SOV-ROOF",
        name="Roofing", base_value=Decimal("10000"),
    )
    
    # Create a +$5000 change order allocated EXCLUSIVELY to Electrical
    repos.change_orders.create(
        scope=scope, contract_id=UUID(contract.contract_id), reference="CO-ELEC-1",
        amount=Decimal("5000"), status="approved", sov_item_id=UUID(item_electrical.sov_item_id),
    )
    
    # Invoice allocating to Roofing for $9000 (100% complete)
    invoice_roof = _make_invoice(repos, scope, total=Decimal("9000"))
    repos.invoice_allocations.create(
        scope=scope, invoice_id=UUID(invoice_roof.invoice_id), sov_item_id=UUID(item_roofing.sov_item_id),
        amount=Decimal("9000"),
    )
    repos.work_confirmations.record(
        scope=scope, project_id=project_id, sov_item_id=UUID(item_roofing.sov_item_id),
        confirmation_type="superintendent", percent_complete=100.0,
    )
    
    outcome_roof = evaluate_progress_billing(repos, scope=scope, invoice_id=UUID(invoice_roof.invoice_id))
    roof_result = outcome_roof.per_item[0]
    # Roofing adjusted contract value MUST remain $10,000 (NOT $12,500 from proportional spreading!)
    assert roof_result.adjusted_contract_value == Decimal("10000")
    # Billable: 10000 - 1000 (10% retainage) = 9000
    assert roof_result.current_billable == Decimal("9000")
    assert roof_result.overbilled is False


# ---------------------------------------------------------------------------
# 7. Authoritative Work Confirmation Supersession (Elimination of max())
# ---------------------------------------------------------------------------

def test_signed_inspection_supersedes_higher_superintendent_estimate(repos, org_a):
    """A signed inspection at 55% supersedes a superintendent report of 80% rather than taking max(55, 80)."""
    scope = org_a["scope"]
    project_id = UUID(org_a["project"].project_id)
    company_id = UUID(org_a["company"].company_id)

    contract = repos.contracts.create(
        scope=scope, project_id=project_id, company_id=company_id,
        reference=f"CON-SUP-{uuid4().hex[:4]}", base_contract_value=Decimal("10000"),
    )
    item = repos.sov_items.create(
        scope=scope, contract_id=UUID(contract.contract_id), reference="SOV-SUP",
        name="Scope Supersede", base_value=Decimal("10000"),
    )
    sov_item_id = UUID(item.sov_item_id)

    now = datetime.now(timezone.utc)
    # Earlier superintendent report: 80%
    repos.work_confirmations.record(
        scope=scope, project_id=project_id, sov_item_id=sov_item_id,
        confirmation_type="superintendent", percent_complete=80.0,
        occurred_at=now - timedelta(hours=2),
    )
    # Later signed inspection: 55%
    repos.work_confirmations.record(
        scope=scope, project_id=project_id, sov_item_id=sov_item_id,
        confirmation_type="signed_inspection", percent_complete=55.0,
        occurred_at=now - timedelta(hours=1),
    )

    svc = WorkConfirmationService(repos.work_confirmations)
    verified = svc.verified_percent_complete(scope=scope, sov_item_id=sov_item_id)
    # The signed inspection is authoritative: 55.0, NOT 80.0!
    assert verified == 55.0


# ---------------------------------------------------------------------------
# 8. Elimination of Scope-Free Project-Wide Work Confirmation Fallback
# ---------------------------------------------------------------------------

def test_scope_free_project_confirmation_does_not_pass_strict_check(repos, org_a):
    """When allow_project_fallback=False, generic project confirmations do not satisfy work checks."""
    scope = org_a["scope"]
    project_id = UUID(org_a["project"].project_id)
    
    # Generic project confirmation
    repos.work_confirmations.record(
        scope=scope, project_id=project_id,
        confirmation_type="superintendent", percent_complete=100.0,
    )
    
    svc = WorkConfirmationService(repos.work_confirmations)
    # Strict check without project fallback:
    assert svc.is_work_confirmed(scope=scope, project_id=project_id, allow_project_fallback=False) is False
    # Only passes when project fallback is explicitly allowed:
    assert svc.is_work_confirmed(scope=scope, project_id=project_id, allow_project_fallback=True) is True
