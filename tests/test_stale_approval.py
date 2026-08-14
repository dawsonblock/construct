"""v0.5.0-rc1 — stale-approval checking (item 48).

Verifies that:
1. An approval is refused if the project state has changed since approval.
2. An approval is NOT refused if the state hasn't changed.
3. An approval without a state_fingerprint is allowed (backward compatible).
4. An approval with project_id=None is allowed (no state to check).
5. The state_fingerprint is recorded at approval time.
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
    check_approval_staleness,
    ApprovalStale,
)
from construction_ai.executive.invoice_pipeline import InvoicePipeline
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
        return {"data": {}}


def _make_approved_invoice_with_project(repos, org_a):
    """Create an invoice with a resolved project, get it approved, return (scope, approval)."""
    scope = org_a["scope"]
    project = repos.projects.create(scope=scope, reference="P-STALE-01", name="Stale Test")

    invoice = Invoice(
        invoice_id="", organization_id="", reference="", invoice_number="INV-STALE-001",
        vendor_name="Vendor", total=Decimal("100.00"), subtotal=Decimal("87.00"),
        tax=Decimal("13.00"), currency="CAD",
    )
    pipeline = InvoicePipeline(repositories=repos, erp_resolver=None)
    result = pipeline.process(
        scope=scope, extracted=invoice, signals={"project_id": project.project_id},
    )
    approval_id = UUID(result["approval_id"])

    provision_approver(
        repos, scope.organization_only,
        subject="approver@stale.com",
        display_name="Approver",
        role="approver",
        permissions=["invoice.read", "invoice.review", "invoice.approve", "invoice.hold", "invoice.reject"],
        maximum_amount=10000.0,
    )
    session = login(repos, provider=DevIdentityProvider(), credential="approver@stale.com")
    actor = actor_from_session(repos, session)
    decide_approval(repos, actor=actor, approval_id=approval_id, decision="approved", reason="test")
    # Record the state fingerprint after the approval decision commits.
    record_state_fingerprint(repos, actor=actor, approval_id=approval_id)

    approval = repos.approvals.get(scope=scope, approval_id=approval_id)
    return scope, approval


class TestStaleApprovalChecking:

    def test_approval_with_unchanged_state_is_not_stale(self, repos, org_a):
        """An approval is not stale when the state hasn't changed."""
        scope, approval = _make_approved_invoice_with_project(repos, org_a)
        # Should not raise.
        check_approval_staleness(repos, scope=scope, approval=approval)

    def test_approval_with_changed_state_is_stale(self, repos, org_a):
        """An approval is stale when the project state has changed."""
        scope, approval = _make_approved_invoice_with_project(repos, org_a)

        # Change the project state by adding evidence.
        project_scope = scope.for_project(UUID(approval.project_id))
        repos.evidence.record(
            scope=project_scope, field="total", value="999.00", confidence=0.9, authority=0.9,
            source_type="erp", source_id="erp1",
        )

        with pytest.raises(ApprovalStale, match="stale"):
            check_approval_staleness(repos, scope=scope, approval=approval)

    def test_approval_without_fingerprint_is_allowed(self, repos, org_a):
        """An approval without state_fingerprint is allowed (backward compatible)."""
        scope, approval = _make_approved_invoice_with_project(repos, org_a)
        # Manually clear the fingerprint.
        with repos.db.scoped(scope) as cur:
            cur.execute("UPDATE approvals SET state_fingerprint = NULL WHERE approval_id = %s", (UUID(approval.approval_id),))
        approval = repos.approvals.get(scope=scope, approval_id=UUID(approval.approval_id))
        assert approval.state_fingerprint is None
        # Should not raise.
        check_approval_staleness(repos, scope=scope, approval=approval)

    def test_approval_without_project_id_is_allowed(self, repos, org_a):
        """An approval with project_id=None is allowed (no state to check)."""
        scope, approval = _make_approved_invoice_with_project(repos, org_a)
        # Manually clear the project_id and state_fingerprint.
        with repos.db.scoped(scope) as cur:
            cur.execute(
                "UPDATE approvals SET project_id = NULL, state_fingerprint = NULL WHERE approval_id = %s",
                (UUID(approval.approval_id),),
            )
        approval = repos.approvals.get(scope=scope, approval_id=UUID(approval.approval_id))
        assert approval.project_id is None
        # Should not raise.
        check_approval_staleness(repos, scope=scope, approval=approval)

    def test_state_fingerprint_is_recorded_at_approval_time(self, repos, org_a):
        """The state_fingerprint is set when the approval is decided."""
        scope, approval = _make_approved_invoice_with_project(repos, org_a)
        assert approval.state_fingerprint is not None
        assert len(approval.state_fingerprint) == 64  # SHA-256 hex

    def test_stale_approval_blocks_execution(self, repos, org_a):
        """A stale approval blocks ERP execution."""
        from construction_ai.executive.executor import execute_approved_invoice, ApprovalStale

        scope, approval = _make_approved_invoice_with_project(repos, org_a)

        # Change the project state.
        project_scope = scope.for_project(UUID(approval.project_id))
        repos.evidence.record(
            scope=project_scope, field="total", value="999.00", confidence=0.9, authority=0.9,
            source_type="erp", source_id="erp1",
        )

        erp_transport = FakeERPTransport()
        adapter = ERPNextAdapter(transport=erp_transport)

        with pytest.raises(ApprovalStale):
            execute_approved_invoice(
                repos, scope=project_scope, approval_id=UUID(approval.approval_id),
                adapter=adapter, erp_read_transport=erp_transport,
            )

        # No ERP document was created.
        all_docs = []
        for docs in erp_transport.docs.values():
            all_docs.extend(docs)
        assert len(all_docs) == 0
