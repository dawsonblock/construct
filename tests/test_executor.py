"""v0.5.0-rc1 — ApprovedInvoiceExecutor and ERP idempotency (items 44, 45).

Verifies that:
1. An approved invoice is submitted to ERP (draft → submit → readback).
2. A non-approved invoice is refused.
3. A duplicate execution is idempotent (returns existing result).
4. The external action ledger prevents duplicate ERP writes.
5. Readback verification catches a failed submission.
6. Every execution is audited.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from construction_ai.auth.identity import DevIdentityProvider
from construction_ai.auth.provisioning import provision_approver
from construction_ai.auth.sessions import actor_from_session, login
from construction_ai.approvals.service import decide_approval
from construction_ai.executive.executor import (
    execute_approved_invoice,
    ApprovalNotApproved,
    ApprovalNotFound,
)
from construction_ai.integrations.erpnext import ERPNextAdapter


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
        # /api/resource/{doctype}/{name}
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


def _make_approved_invoice(repos, org_a, erp_adapter=None):
    """Helper: create an invoice, get it approved, return (scope, approval_id, invoice_id)."""
    from construction_ai.executive.invoice_pipeline import InvoicePipeline

    scope = org_a["scope"]
    project = repos.projects.create(scope=scope, reference="P-EXEC-01", name="Exec Test")
    project_scope = scope.for_project(UUID(project.project_id))

    repos.companies.create(
        scope=scope.organization_only, reference="V-EXEC-01", name="Exec Vendor", company_type="vendor",
        erp_supplier_id="Exec Vendor",
    )

    from construction_ai.domain.models import Invoice

    invoice = Invoice(
        invoice_id="", organization_id="", reference="", invoice_number="INV-EXEC-001",
        vendor_name="Exec Vendor", total=Decimal("100.00"), subtotal=Decimal("87.00"),
        tax=Decimal("13.00"), currency="CAD", po_number=None, quote_number=None,
        project_id=None, vendor_company_id=None,
    )

    pipeline = InvoicePipeline(repositories=repos, erp_resolver=None)
    result = pipeline.process(
        scope=scope, extracted=invoice, signals={"project_reference": "P-EXEC-01"},
    )
    approval_id = UUID(result["approval_id"])
    invoice_id = UUID(result["invoice_id"])

    # Approve it — need an authenticated actor with authority.
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


class TestApprovedInvoiceExecutor:

    def test_approved_invoice_is_submitted(self, repos, org_a, erp_adapter, erp_transport):
        """An approved invoice is submitted to ERP with readback verification."""
        project_scope, approval_id, invoice_id = _make_approved_invoice(repos, org_a)

        result = execute_approved_invoice(
            repos, scope=project_scope, approval_id=approval_id,
            adapter=erp_adapter, erp_read_transport=erp_transport,
        )

        assert result.idempotent is False
        assert result.erp_docstatus == 1  # submitted
        assert result.erp_docname.startswith("PINV-")

    def test_non_approved_invoice_refused(self, repos, org_a, erp_adapter, erp_transport):
        """A pending approval is refused."""
        from construction_ai.executive.invoice_pipeline import InvoicePipeline
        from construction_ai.domain.models import Invoice

        scope = org_a["scope"]
        project = repos.projects.create(scope=scope, reference="P-EXEC-02", name="Exec Test 2")
        project_scope = scope.for_project(UUID(project.project_id))

        invoice = Invoice(
            invoice_id="", organization_id="", reference="", invoice_number="INV-EXEC-002",
            vendor_name="Vendor", total=Decimal("100.00"), subtotal=Decimal("87.00"),
            tax=Decimal("13.00"), currency="CAD", po_number=None, quote_number=None,
            project_id=None, vendor_company_id=None,
        )
        pipeline = InvoicePipeline(repositories=repos, erp_resolver=None)
        result = pipeline.process(
            scope=scope, extracted=invoice, signals={"project_reference": "P-EXEC-02"},
        )
        approval_id = UUID(result["approval_id"])

        with pytest.raises(ApprovalNotApproved):
            execute_approved_invoice(
                repos, scope=project_scope, approval_id=approval_id,
                adapter=erp_adapter, erp_read_transport=erp_transport,
            )

    def test_nonexistent_approval_refused(self, repos, org_a, erp_adapter, erp_transport):
        """A nonexistent approval raises ApprovalNotFound."""
        scope = org_a["scope"]
        project = repos.projects.create(scope=scope, reference="P-EXEC-03", name="Exec Test 3")
        project_scope = scope.for_project(UUID(project.project_id))

        with pytest.raises(ApprovalNotFound):
            execute_approved_invoice(
                repos, scope=project_scope, approval_id=uuid4(),
                adapter=erp_adapter, erp_read_transport=erp_transport,
            )

    def test_duplicate_execution_is_idempotent(self, repos, org_a, erp_adapter, erp_transport):
        """Executing the same approval twice only writes to ERP once."""
        project_scope, approval_id, invoice_id = _make_approved_invoice(repos, org_a)

        # First execution.
        result1 = execute_approved_invoice(
            repos, scope=project_scope, approval_id=approval_id,
            adapter=erp_adapter, erp_read_transport=erp_transport,
        )
        assert result1.idempotent is False

        # Second execution — should be idempotent.
        result2 = execute_approved_invoice(
            repos, scope=project_scope, approval_id=approval_id,
            adapter=erp_adapter, erp_read_transport=erp_transport,
        )
        assert result2.idempotent is True
        assert result2.erp_docname == result1.erp_docname
        assert result2.erp_docstatus == result1.erp_docstatus

        # Only one ERP document was created.
        all_docs = []
        for docs in erp_transport.docs.values():
            all_docs.extend(docs)
        assert len(all_docs) == 1

    def test_execution_is_audited(self, repos, org_a, erp_adapter, erp_transport):
        """Every execution produces an audit event."""
        project_scope, approval_id, invoice_id = _make_approved_invoice(repos, org_a)

        execute_approved_invoice(
            repos, scope=project_scope, approval_id=approval_id,
            adapter=erp_adapter, erp_read_transport=erp_transport,
        )

        # Check audit log.
        with repos.db.scoped(project_scope) as cur:
            cur.execute(
                "SELECT event_type, payload FROM audit_events WHERE object_id = %s ORDER BY created_at DESC LIMIT 1",
                (str(invoice_id),),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "ERP_INVOICE_SUBMITTED"
        payload = row[1]
        assert payload["erp_docstatus"] == 1
        assert "erp_docname" in payload

    def test_external_action_ledger_records_execution(self, repos, org_a, erp_adapter, erp_transport):
        """The external action ledger records the ERP submission."""
        project_scope, approval_id, invoice_id = _make_approved_invoice(repos, org_a)

        execute_approved_invoice(
            repos, scope=project_scope, approval_id=approval_id,
            adapter=erp_adapter, erp_read_transport=erp_transport,
        )

        actions = repos.external_actions.list(scope=org_a["scope"], action_type="erp_submit_purchase_invoice")
        assert len(actions) == 1
        assert actions[0].status == "completed"
        assert actions[0].target_system == "erpnext"
        assert actions[0].result["docstatus"] == 1


class TestReadbackVerification:

    def test_readback_catches_failed_submission(self, repos, org_a, erp_adapter):
        """If readback shows docstatus != 1, execution fails."""
        project_scope, approval_id, invoice_id = _make_approved_invoice(repos, org_a)

        # A transport where readback returns docstatus=0 (not submitted).
        class FailedReadbackTransport(FakeERPTransport):
            def get(self, path, params=None):
                return {"data": {"name": "PINV-0001", "docstatus": 0}}

        from construction_ai.executive.executor import ReadbackFailed

        with pytest.raises(ReadbackFailed):
            execute_approved_invoice(
                repos, scope=project_scope, approval_id=approval_id,
                adapter=erp_adapter, erp_read_transport=FailedReadbackTransport(),
            )
