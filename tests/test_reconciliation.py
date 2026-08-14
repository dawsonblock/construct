"""v0.5.0-rc1 — ERP readback and reconciliation (items 46, 47).

Verifies that:
1. readback_document confirms a submitted document's status.
2. readback_document detects a missing document.
3. readback_document detects a docstatus mismatch.
4. reconcile_erp_state reports all matched documents.
5. reconcile_erp_state detects drifted documents.
6. reconcile_erp_state detects missing documents.
7. reconciliation is read-only (no writes to ERP or database).
"""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from construction_ai.auth.identity import DevIdentityProvider
from construction_ai.auth.provisioning import provision_approver
from construction_ai.auth.sessions import actor_from_session, login
from construction_ai.approvals.service import decide_approval
from construction_ai.executive.executor import execute_approved_invoice
from construction_ai.executive.reconciliation import (
    readback_document,
    reconcile_erp_state,
)
from construction_ai.integrations.erpnext import ERPNextAdapter


class FakeERPTransport:
    """In-memory ERP transport."""

    def __init__(self):
        self.docs: dict[str, list[dict]] = {}
        self._counter = 0
        self.get_calls = 0
        self.post_calls = 0

    def post(self, path: str, json: dict | None = None) -> dict:
        json = json or {}
        self.post_calls += 1
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
        self.get_calls += 1
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


def _make_approved_and_executed(repos, org_a, erp_adapter, erp_transport):
    """Create an approved invoice, execute it, return (scope, docname)."""
    from construction_ai.executive.invoice_pipeline import InvoicePipeline
    from construction_ai.domain.models import Invoice

    scope = org_a["scope"]
    project = repos.projects.create(scope=scope, reference="P-RECON-01", name="Recon Test")
    project_scope = scope.for_project(UUID(project.project_id))

    invoice = Invoice(
        invoice_id="", organization_id="", reference="", invoice_number="INV-RECON-001",
        vendor_name="Recon Vendor", total=Decimal("100.00"), subtotal=Decimal("87.00"),
        tax=Decimal("13.00"), currency="CAD",
    )
    pipeline = InvoicePipeline(repositories=repos, erp_resolver=None)
    result = pipeline.process(
        scope=scope, extracted=invoice, signals={"project_reference": "P-RECON-01"},
    )
    approval_id = UUID(result["approval_id"])

    provision_approver(
        repos, scope.organization_only,
        subject="approver@recon.com",
        display_name="Approver",
        role="approver",
        permissions=["invoice.read", "invoice.review", "invoice.approve", "invoice.hold", "invoice.reject"],
        maximum_amount=10000.0,
    )
    session = login(repos, provider=DevIdentityProvider(), credential="approver@recon.com")
    actor = actor_from_session(repos, session)
    decide_approval(repos, actor=actor, approval_id=approval_id, decision="approved", reason="test")

    exec_result = execute_approved_invoice(
        repos, scope=project_scope, approval_id=approval_id,
        adapter=erp_adapter, erp_read_transport=erp_transport,
    )
    return scope, exec_result.erp_docname


class TestReadback:

    def test_readback_confirms_submitted_document(self, erp_transport):
        """readback_document confirms a submitted document has docstatus=1."""
        # Create and submit a document.
        erp_transport.post("/api/resource/Purchase Invoice", json={"supplier": "Test", "docstatus": 0})
        erp_transport.post("/api/method/frappe.client.submit", json={"doctype": "Purchase Invoice", "name": "PINV-0001"})

        result = readback_document(
            erp_read_transport=erp_transport,
            doctype="Purchase Invoice",
            docname="PINV-0001",
            expected_docstatus=1,
        )
        assert result.found is True
        assert result.docstatus == 1
        assert result.matches_expected is True

    def test_readback_detects_missing_document(self, erp_transport):
        """readback_document reports not found for a nonexistent document."""
        result = readback_document(
            erp_read_transport=erp_transport,
            doctype="Purchase Invoice",
            docname="NONEXISTENT-0001",
        )
        assert result.found is False
        assert result.matches_expected is False

    def test_readback_detects_docstatus_mismatch(self, erp_transport):
        """readback_document detects when docstatus doesn't match expected."""
        # Create a draft (docstatus=0) but don't submit.
        erp_transport.post("/api/resource/Purchase Invoice", json={"supplier": "Test", "docstatus": 0})

        result = readback_document(
            erp_read_transport=erp_transport,
            doctype="Purchase Invoice",
            docname="PINV-0001",
            expected_docstatus=1,
        )
        assert result.found is True
        assert result.docstatus == 0
        assert result.matches_expected is False
        assert len(result.discrepancies) == 1


class TestReconciliation:

    def test_reconciliation_reports_matched_documents(self, repos, org_a, erp_adapter, erp_transport):
        """reconcile_erp_state reports all matched documents."""
        scope, docname = _make_approved_and_executed(repos, org_a, erp_adapter, erp_transport)

        report = reconcile_erp_state(repos, scope=scope, erp_read_transport=erp_transport)
        assert report.total_checked == 1
        assert report.matched == 1
        assert report.drifted == 0
        assert report.missing == 0
        assert report.details[0]["status"] == "matched"
        assert report.details[0]["docname"] == docname

    def test_reconciliation_detects_drifted_documents(self, repos, org_a, erp_adapter, erp_transport):
        """reconcile_erp_state detects when ERP docstatus has changed."""
        scope, docname = _make_approved_and_executed(repos, org_a, erp_adapter, erp_transport)

        # Simulate drift: revert the docstatus in ERP.
        for docs in erp_transport.docs.values():
            for doc in docs:
                if doc.get("name") == docname:
                    doc["docstatus"] = 0  # reverted to draft

        report = reconcile_erp_state(repos, scope=scope, erp_read_transport=erp_transport)
        assert report.total_checked == 1
        assert report.matched == 0
        assert report.drifted == 1
        assert report.details[0]["status"] == "drifted"

    def test_reconciliation_detects_missing_documents(self, repos, org_a, erp_adapter, erp_transport):
        """reconcile_erp_state detects when an ERP document is missing."""
        scope, docname = _make_approved_and_executed(repos, org_a, erp_adapter, erp_transport)

        # Simulate deletion: remove the document from ERP.
        for docs in erp_transport.docs.values():
            docs[:] = [d for d in docs if d.get("name") != docname]

        report = reconcile_erp_state(repos, scope=scope, erp_read_transport=erp_transport)
        assert report.total_checked == 1
        assert report.matched == 0
        assert report.missing == 1
        assert report.details[0]["status"] == "missing"

    def test_reconciliation_is_read_only(self, repos, org_a, erp_adapter, erp_transport):
        """reconcile_erp_state never writes to ERP (no POST calls)."""
        scope, docname = _make_approved_and_executed(repos, org_a, erp_adapter, erp_transport)
        post_before = erp_transport.post_calls

        reconcile_erp_state(repos, scope=scope, erp_read_transport=erp_transport)

        assert erp_transport.post_calls == post_before  # no writes during reconciliation

    def test_reconciliation_empty_when_no_actions(self, repos, org_a, erp_transport):
        """reconcile_erp_state returns an empty report when no actions exist."""
        report = reconcile_erp_state(repos, scope=org_a["scope"], erp_read_transport=erp_transport)
        assert report.total_checked == 0
        assert report.matched == 0
        assert report.drifted == 0
        assert report.missing == 0
