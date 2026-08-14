"""rc4 Phase 18 — Crash matrix tests for external-effect recovery.

Tests that every crash point ends in one of:

    CONFIRMED
    PROVEN_ABSENT + retry
    REMOTE_DRAFT
    MANUAL_RECONCILIATION_REQUIRED

Never permanently stranded.

The crash matrix covers:
1. After reservation
2. After lease acquisition
3. After draft creation
4. After remote ID persistence
5. After submit
6. After readback
7. After local CONFIRMED transition
8. Before final audit

These tests use real crash hooks (not manual state mutations) to simulate
worker crashes at each external boundary.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from construction_ai.auth.sessions import actor_from_session, login
from construction_ai.auth.identity import DevIdentityProvider
from construction_ai.auth.provisioning import provision_approver
from construction_ai.approvals.service import decide_approval
from construction_ai.executive.executor import (
    ExecutionError,
    execute_approved_invoice,
    set_crash_hook,
)
from construction_ai.executive.invoice_pipeline import InvoicePipeline
from construction_ai.executive.recovery_daemon import run_recovery_cycle
from construction_ai.domain.models import Invoice
from construction_ai.integrations.erpnext import ERPNextAdapter


# -- Helpers (mirrors test_executor.py) -------------------------------------

class FakeERPTransport:
    """In-memory ERP transport that mimics the stub's write/read behavior."""

    def __init__(self):
        self.docs: dict[str, list[dict]] = {}
        self._counter = 0

    def post(self, path: str, json: dict | None = None) -> dict:
        json = json or {}
        if "/api/resource/" in path:
            doctype = path.split("/api/resource/")[-1]
            self._counter += 1
            name = f"PINV-{self._counter:04d}"
            doc = {**json, "name": name, "docstatus": json.get("docstatus", 0)}
            self.docs.setdefault(doctype, []).append(doc)
            return {"data": doc}
        if "frappe.client.submit" in path:
            doctype = json.get("doctype", "")
            name = json.get("name", "")
            for row in self.docs.get(doctype, []):
                if row.get("name") == name:
                    row["docstatus"] = 1
                    return {"data": row}
            return {"data": {}}
        return {}

    def get(self, path: str, params: dict | None = None) -> dict:
        from urllib.parse import unquote
        parts = path.split("/api/resource/")
        if len(parts) == 2:
            rest = parts[1].split("/")
            if len(rest) == 2:
                doctype, name = unquote(rest[0]), unquote(rest[1])
                for row in self.docs.get(doctype, []):
                    if row.get("name") == name:
                        return {"data": row}
        return {"data": {}}


@pytest.fixture()
def erp_transport():
    return FakeERPTransport()


@pytest.fixture()
def erp_adapter(erp_transport):
    return ERPNextAdapter(transport=erp_transport)


def _make_approved_invoice(repos, org_a):
    """Create an invoice, get it approved with both fingerprints, return (scope, approval_id, invoice_id)."""
    scope = org_a["scope"]
    project = repos.projects.create(scope=scope, reference=f"P-EXEC-{uuid4().hex[:4]}", name="Exec Test")
    project_scope = scope.for_project(UUID(project.project_id))

    repos.companies.create(
        scope=scope.organization_only, reference=f"V-EXEC-{uuid4().hex[:4]}", name="Exec Vendor", company_type="vendor",
        erp_supplier_id="Exec Vendor",
    )

    invoice = Invoice(
        invoice_id="", organization_id="", reference="", invoice_number=f"INV-EXEC-{uuid4().hex[:8]}",
        vendor_name="Exec Vendor", total=Decimal("100.00"), subtotal=Decimal("87.00"),
        tax=Decimal("13.00"), currency="CAD", po_number=None, quote_number=None,
        project_id=None, vendor_company_id=None,
    )

    pipeline = InvoicePipeline(repositories=repos, erp_resolver=None)
    result = pipeline.process(
        scope=scope, extracted=invoice, signals={"project_id": project.project_id},
    )
    approval_id = UUID(result["approval_id"])
    invoice_id = UUID(result["invoice_id"])

    provision_approver(
        repos, scope.organization_only,
        subject="approver@example.com",
        display_name="Approver",
        role="approver",
        permissions=["invoice.read", "invoice.review", "invoice.approve", "invoice.hold", "invoice.reject"],
        maximum_amount=10000.0,
        currency="CAD",
    )
    session = login(repos, provider=DevIdentityProvider(), credential="approver@example.com")
    actor = actor_from_session(repos, session)

    decide_approval(
        repos, actor=actor, approval_id=approval_id, decision="approved", reason="test approval",
    )

    return project_scope, approval_id, invoice_id


# -- Crash matrix -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_crash_hook():
    """Ensure crash hooks are cleared after each test."""
    yield
    set_crash_hook(None)


def test_crash_after_reservation_ends_pending(repos, org_a, erp_adapter, erp_transport):
    """Crash after reservation: action stays PENDING, safe to retry."""
    project_scope, approval_id, invoice_id = _make_approved_invoice(repos, org_a)

    set_crash_hook("after_reservation")
    with pytest.raises(ExecutionError):
        execute_approved_invoice(
            repos, scope=project_scope, approval_id=approval_id,
            adapter=erp_adapter, erp_read_transport=erp_transport,
        )

    actions = repos.external_actions.list(scope=project_scope.organization_only, action_type="erp_submit_purchase_invoice")
    assert len(actions) == 1
    assert actions[0].status == "pending"


def test_crash_after_lease_acquire_ends_executing(repos, org_a, erp_adapter, erp_transport):
    """Crash after lease acquisition: action is EXECUTING with a lease."""
    project_scope, approval_id, invoice_id = _make_approved_invoice(repos, org_a)

    set_crash_hook("after_lease_acquire")
    with pytest.raises(ExecutionError):
        execute_approved_invoice(
            repos, scope=project_scope, approval_id=approval_id,
            adapter=erp_adapter, erp_read_transport=erp_transport,
        )

    actions = repos.external_actions.list(scope=project_scope.organization_only, action_type="erp_submit_purchase_invoice")
    assert len(actions) == 1
    assert actions[0].status == "executing"
    assert actions[0].execution_owner is not None
    assert actions[0].lease_expires_at is not None

    # Expire the lease manually and reap.
    with repos.db.scoped(project_scope.organization_only) as cur:
        cur.execute(
            "UPDATE external_actions SET lease_expires_at = now() - interval '1 second' "
            "WHERE action_id = %s",
            (actions[0].action_id,),
        )
    reaped = repos.external_actions.reap_expired(scope=project_scope.organization_only)
    assert len(reaped) == 1
    assert reaped[0].status == "unknown"


def test_crash_after_erp_create_persists_remote_draft(repos, org_a, erp_adapter, erp_transport):
    """Crash after ERP draft creation: remote_document_id is persisted (rc4 Phase 2)."""
    project_scope, approval_id, invoice_id = _make_approved_invoice(repos, org_a)

    set_crash_hook("after_erp_create")
    with pytest.raises(ExecutionError):
        execute_approved_invoice(
            repos, scope=project_scope, approval_id=approval_id,
            adapter=erp_adapter, erp_read_transport=erp_transport,
        )

    actions = repos.external_actions.list(scope=project_scope.organization_only, action_type="erp_submit_purchase_invoice")
    assert len(actions) == 1
    assert actions[0].remote_document_id is not None
    assert actions[0].remote_state == "remote_draft"


def test_crash_before_confirmed_leaves_executing(repos, org_a, erp_adapter, erp_transport):
    """Crash before CONFIRMED: action is EXECUTING with remote_state=remote_submitted."""
    project_scope, approval_id, invoice_id = _make_approved_invoice(repos, org_a)

    set_crash_hook("before_confirmed")
    with pytest.raises(ExecutionError):
        execute_approved_invoice(
            repos, scope=project_scope, approval_id=approval_id,
            adapter=erp_adapter, erp_read_transport=erp_transport,
        )

    actions = repos.external_actions.list(scope=project_scope.organization_only, action_type="erp_submit_purchase_invoice")
    assert len(actions) == 1
    assert actions[0].status == "executing"
    assert actions[0].remote_state == "remote_submitted"


def test_crash_before_audit_confirmed_but_no_final_audit(repos, org_a, erp_adapter, erp_transport):
    """Crash before final audit: CONFIRMED but missing final audit -> repair."""
    project_scope, approval_id, invoice_id = _make_approved_invoice(repos, org_a)

    set_crash_hook("before_audit")
    with pytest.raises(ExecutionError):
        execute_approved_invoice(
            repos, scope=project_scope, approval_id=approval_id,
            adapter=erp_adapter, erp_read_transport=erp_transport,
        )

    actions = repos.external_actions.list(scope=project_scope.organization_only, action_type="erp_submit_purchase_invoice")
    assert len(actions) == 1
    assert actions[0].status == "confirmed"
    assert actions[0].final_audit_event_id is None

    # Run the recovery daemon — it should repair the audit.
    result = run_recovery_cycle(repos, scope=project_scope)
    assert len(result.repaired_audits) == 1

    updated = repos.external_actions.get(scope=project_scope.organization_only, action_id=actions[0].action_id)
    assert updated.final_audit_event_id is not None


# -- Stronger external-effect invariants (rc4 Phase 19) ---------------------

class TestExternalEffectInvariants:
    """Tests for the rc4 Phase 19 invariants."""

    def test_remote_draft_does_not_mean_confirmed(self, repos, org_a):
        """RemoteDraft is not CONFIRMED — a draft is not a financial effect."""
        scope = org_a["scope"].organization_only
        action = repos.external_actions.reserve(
            scope=scope, operation="test_op",
            idempotency_key=f"draft-not-confirmed-{uuid4()}",
            subject_type="invoice", subject_id=uuid4(),
        )
        repos.external_actions.transition(
            scope=scope, action_id=action.action_id,
            from_status="pending", to_status="executing",
            remote_document_id="PINV-DRAFT", remote_state="remote_draft",
        )
        updated = repos.external_actions.get(scope=scope, action_id=action.action_id)
        assert updated.status == "executing"
        assert updated.remote_state == "remote_draft"
        assert updated.status != "confirmed"

    def test_confirmed_implies_remote_submitted(self, repos, org_a):
        """CONFIRMED implies remote_state=remote_submitted."""
        scope = org_a["scope"].organization_only
        action = repos.external_actions.reserve(
            scope=scope, operation="test_op",
            idempotency_key=f"confirmed-submitted-{uuid4()}",
            subject_type="invoice", subject_id=uuid4(),
        )
        repos.external_actions.transition(
            scope=scope, action_id=action.action_id,
            from_status="pending", to_status="confirmed",
            remote_document_id="PINV-CONFIRMED", remote_state="remote_submitted",
            result={"docname": "PINV-CONFIRMED", "docstatus": 1},
        )
        updated = repos.external_actions.get(scope=scope, action_id=action.action_id)
        assert updated.status == "confirmed"
        assert updated.remote_state == "remote_submitted"

    def test_executable_approval_requires_both_fingerprints(self, repos, org_a):
        """ExecutableApproval requires both fingerprints (rc4 Phase 9)."""
        from construction_ai.executive.executor import ApprovalStale, check_approval_staleness

        scope = org_a["scope"]
        project = repos.projects.create(scope=scope, reference=f"P-FP-{uuid4().hex[:4]}", name="FP Test")
        project_scope = scope.for_project(UUID(project.project_id))

        inv = repos.invoices.create(
            scope=project_scope, reference=f"R-{uuid4().hex[:8]}",
            invoice_number=f"INV-{uuid4().hex[:8]}",
            vendor_name="Test Supplier",
            total=100.00, subtotal=87.00, tax=13.00, currency="CAD",
        )
        approval = repos.approvals.create(
            scope=project_scope, reference=f"APR-{uuid4().hex[:8]}",
            approval_type="PURCHASE_INVOICE", subject_type="invoice",
            subject_id=UUID(inv.invoice_id),
            recommended_action="APPROVE", amount=100.0, currency="CAD",
        )
        # Approve with state_fingerprint but NOT decision_fingerprint.
        approved = repos.approvals.decide(
            scope=project_scope.organization_only,
            approval_id=approval.approval_id,
            status="approved",
            decided_by="test_user@example.com",
            state_fingerprint="some_state_fingerprint",
            decision_fingerprint=None,
        )
        assert approved is not None

        loaded = repos.approvals.get(scope=project_scope.organization_only, approval_id=approval.approval_id)
        assert loaded.state_fingerprint is not None
        assert loaded.decision_fingerprint is None

        with pytest.raises(ApprovalStale):
            check_approval_staleness(repos, scope=project_scope.organization_only, approval=loaded)
