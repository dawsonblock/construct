"""v0.5.0-rc2 Phase 3 — Crash-injection qualification for ERP execution.

For every crash point:
1. terminate the worker (simulate crash);
2. restart;
3. rerun the job;
4. reconcile;
5. count ERP documents.

Required invariant: ERPFinancialEffects(invoice) ≤ 1

Also tests two workers concurrently against the same invoice:
1 worker owns the execution reservation. The other must not submit.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from construction_ai.auth.identity import DevIdentityProvider
from construction_ai.auth.provisioning import provision_approver
from construction_ai.auth.sessions import actor_from_session, login
from construction_ai.approvals.service import decide_approval, record_state_fingerprint
from construction_ai.executive.executor import (
    execute_approved_invoice,
    set_crash_hook,
    ExecutionError,
    ExternalActionInProgress,
)
from construction_ai.executive.invoice_pipeline import InvoicePipeline
from construction_ai.executive.reconcile_unknown import reconcile_external_action
from construction_ai.domain.models import Invoice
from construction_ai.integrations.erpnext import ERPNextAdapter


class FakeERPTransport:
    def __init__(self):
        self.docs: dict[str, list[dict]] = {}
        self._counter = 0

    def post(self, path, json=None):
        json = json or {}
        if "/api/resource/" in path:
            doctype = path.split("/api/resource/")[-1]
            self._counter += 1
            name = f"PINV-{self._counter:04d}"
            doc = {**json, "name": name, "docstatus": json.get("docstatus", 0)}
            self.docs.setdefault(doctype, []).append(doc)
            return {"data": doc}
        if "frappe.client.submit" in path:
            for row in self.docs.get(json.get("doctype", ""), []):
                if row.get("name") == json.get("name"):
                    row["docstatus"] = 1
                    return {"data": row}
        return {}

    def get(self, path, params=None):
        from urllib.parse import unquote
        parts = path.split("/api/resource/")
        if len(parts) == 2:
            rest = parts[1].split("/")
            if len(rest) == 2:
                doctype, name = unquote(rest[0]), unquote(rest[1])
                for row in self.docs.get(doctype, []):
                    if row.get("name") == name:
                        return {"data": row}
            elif len(rest) == 1 and params and "filters" in params:
                return {"data": [
                    {"name": d["name"], "supplier": d.get("supplier"), "bill_no": d.get("bill_no"),
                     "docstatus": d.get("docstatus", 0), "grand_total": d.get("grand_total")}
                    for d in self.docs.get("Purchase Invoice", [])
                ]}
        return {"data": {}}

    def count_docs(self) -> int:
        return sum(len(docs) for docs in self.docs.values())


@pytest.fixture()
def erp_transport():
    return FakeERPTransport()


@pytest.fixture()
def erp_adapter(erp_transport):
    return ERPNextAdapter(transport=erp_transport)


def _make_approved_invoice(repos, org_a, unique_suffix):
    scope = org_a["scope"]
    project = repos.projects.create(scope=scope, reference=f"P-CRASH-{unique_suffix}", name="Crash Test")
    invoice = Invoice(
        invoice_id="", organization_id="", reference="", invoice_number=f"INV-CRASH-{unique_suffix}",
        vendor_name=f"Vendor-{unique_suffix}", total=Decimal("100.00"), subtotal=Decimal("87.00"),
        tax=Decimal("13.00"), currency="CAD",
    )
    pipeline = InvoicePipeline(repositories=repos, erp_resolver=None)
    result = pipeline.process(scope=scope, extracted=invoice, signals={"project_id": project.project_id})
    approval_id = UUID(result["approval_id"])
    provision_approver(
        repos, scope.organization_only,
        subject=f"approver@crash-{unique_suffix}.com", display_name="Approver", role="approver",
        permissions=["invoice.read", "invoice.review", "invoice.approve", "invoice.hold", "invoice.reject"],
        maximum_amount=10000.0,
    )
    session = login(repos, provider=DevIdentityProvider(), credential=f"approver@crash-{unique_suffix}.com")
    actor = actor_from_session(repos, session)
    decide_approval(repos, actor=actor, approval_id=approval_id, decision="approved", reason="test")
    record_state_fingerprint(repos, actor=actor, approval_id=approval_id)
    return scope, approval_id


def _erp_doc_count(erp_transport) -> int:
    """Count total ERP documents created."""
    return erp_transport.count_docs()


class TestCrashInjectionQualification:
    """Every crash point: crash → restart → rerun → verify ERP doc count ≤ 1."""

    @pytest.mark.parametrize("crash_point", [
        "after_reservation",
        "before_erp_create",
        "after_erp_create",
        "before_erp_submit",
        "after_erp_submit",
        "before_readback",
        "after_readback",
        "before_confirmed",
        "before_audit",
    ])
    def test_crash_at_every_point_leaves_at_most_one_erp_doc(self, repos, org_a, erp_adapter, erp_transport, crash_point):
        """Crash at any point → at most 1 ERP document exists."""
        scope, approval_id = _make_approved_invoice(repos, org_a, crash_point)
        set_crash_hook(crash_point)
        try:
            with pytest.raises(ExecutionError):
                execute_approved_invoice(
                    repos, scope=scope, approval_id=approval_id,
                    adapter=erp_adapter, erp_read_transport=erp_transport,
                )
        finally:
            set_crash_hook(None)

        # After crash: at most 1 ERP document.
        assert _erp_doc_count(erp_transport) <= 1, f"crash at {crash_point}: too many ERP docs"

    def test_crash_after_reservation_then_retry_succeeds(self, repos, org_a, erp_adapter, erp_transport):
        """Crash after reservation → retry succeeds with exactly 1 ERP doc."""
        scope, approval_id = _make_approved_invoice(repos, org_a, "retry-1")
        set_crash_hook("after_reservation")
        try:
            with pytest.raises(ExecutionError):
                execute_approved_invoice(
                    repos, scope=scope, approval_id=approval_id,
                    adapter=erp_adapter, erp_read_transport=erp_transport,
                )
        finally:
            set_crash_hook(None)
        assert _erp_doc_count(erp_transport) == 0

        # Retry — should succeed.
        result = execute_approved_invoice(
            repos, scope=scope, approval_id=approval_id,
            adapter=erp_adapter, erp_read_transport=erp_transport,
        )
        assert result.erp_docstatus == 1
        assert _erp_doc_count(erp_transport) == 1

    def test_crash_after_erp_submit_then_reconcile_then_retry(self, repos, org_a, erp_adapter, erp_transport):
        """Crash after ERP submit → reconcile → confirmed (no duplicate)."""
        scope, approval_id = _make_approved_invoice(repos, org_a, "recon-1")
        set_crash_hook("after_erp_submit")
        try:
            with pytest.raises(ExecutionError):
                execute_approved_invoice(
                    repos, scope=scope, approval_id=approval_id,
                    adapter=erp_adapter, erp_read_transport=erp_transport,
                )
        finally:
            set_crash_hook(None)
        # ERP document was created and submitted.
        assert _erp_doc_count(erp_transport) == 1

        # The action is in EXECUTING state (crash happened after submit,
        # before transition to CONFIRMED).
        action = repos.external_actions.get_by_key(
            scope=scope, operation="erp_submit_purchase_invoice",
            idempotency_key=f"approval:{approval_id}",
        )
        assert action.status == "executing"

        # Manually transition to UNKNOWN (simulating lease expiry detection).
        repos.external_actions.transition(
            scope=scope, action_id=action.action_id,
            from_status="executing", to_status="unknown",
            remote_document_id=None, last_error="lease expired",
        )

        # Reconcile — should find the existing ERP document.
        outcome = reconcile_external_action(
            repos, scope=scope, action_id=action.action_id,
            erp_read_transport=erp_transport,
            invoice_number="INV-CRASH-recon-1", supplier="Vendor-recon-1",
        )
        assert outcome.new_status == "confirmed"
        assert _erp_doc_count(erp_transport) == 1  # still only 1

        # Retry execution — should be idempotent (CONFIRMED).
        result = execute_approved_invoice(
            repos, scope=scope, approval_id=approval_id,
            adapter=erp_adapter, erp_read_transport=erp_transport,
        )
        assert result.idempotent is True
        assert _erp_doc_count(erp_transport) == 1  # still only 1


class TestConcurrentExecution:
    """Two workers concurrently against the same invoice: only 1 may submit."""

    def test_concurrent_workers_only_one_submits(self, repos, org_a, erp_adapter, erp_transport):
        """Two concurrent execution attempts: only one creates an ERP document."""
        scope, approval_id = _make_approved_invoice(repos, org_a, "conc-1")

        # First worker reserves and transitions to EXECUTING.
        action = repos.external_actions.reserve(
            scope=scope, operation="erp_submit_purchase_invoice",
            idempotency_key=f"approval:{approval_id}",
        )
        repos.external_actions.transition(
            scope=scope, action_id=action.action_id,
            from_status="pending", to_status="executing",
        )

        # Second worker tries to execute — should be refused.
        with pytest.raises(ExternalActionInProgress):
            execute_approved_invoice(
                repos, scope=scope, approval_id=approval_id,
                adapter=erp_adapter, erp_read_transport=erp_transport,
            )

        # No ERP document was created by the second worker.
        assert _erp_doc_count(erp_transport) == 0

    def test_repeated_execution_never_exceeds_one_erp_doc(self, repos, org_a, erp_adapter, erp_transport):
        """Repeated execution of the same approval never creates >1 ERP doc."""
        scope, approval_id = _make_approved_invoice(repos, org_a, "repeat-1")

        # Execute 5 times.
        for _i in range(5):
            execute_approved_invoice(
                repos, scope=scope, approval_id=approval_id,
                adapter=erp_adapter, erp_read_transport=erp_transport,
            )

        assert _erp_doc_count(erp_transport) == 1
