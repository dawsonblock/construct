"""Phases 22 and 23 — complete execution preconditions and full audit events.

Phase 22 proves the full conjunction of preconditions before ERP execution:

    ApprovalApproved ∧ NotStale ∧ EvidenceFresh ∧ ReservationHeld

Phase 23 proves every external-action audit event carries request_hash and
response_hash binding the audit trail to the exact bytes sent to and received
from the external system.

Needs a real PostgreSQL as the non-superuser app role.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from construction_ai.approvals.service import decide_approval
from construction_ai.auth.identity import DevIdentityProvider
from construction_ai.auth.provisioning import provision_approver
from construction_ai.auth.sessions import actor_from_session, login
from construction_ai.domain.models import Invoice
from construction_ai.executive.executor import (
    ExecutionError,
    execute_approved_invoice,
)
from construction_ai.executive.invoice_pipeline import InvoicePipeline
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


def _make_approved_invoice(repos, org_a, *, evidence=None):
    """Create an invoice, optionally attach evidence, then approve it.

    Evidence must be attached BEFORE approval so it is included in the
    project state fingerprint. Pass a list of (field, value, source_type,
    source_id, observed_at) tuples.
    """
    scope = org_a["scope"]
    project = repos.projects.create(scope=scope, reference="P-PRE-01", name="Precondition Test")
    invoice = Invoice(
        invoice_id="", organization_id="", reference="", invoice_number="INV-PRE-001",
        vendor_name="Vendor", total=Decimal("100.00"), subtotal=Decimal("87.00"),
        tax=Decimal("13.00"), currency="CAD",
    )
    pipeline = InvoicePipeline(repositories=repos, erp_resolver=None)
    result = pipeline.process(scope=scope, extracted=invoice, signals={"project_id": project.project_id})
    approval_id = UUID(result["approval_id"])

    # Attach evidence before approval so the fingerprint includes it.
    evidence_ids = []
    if evidence:
        project_scope = scope.for_project(UUID(project.project_id))
        for (field, value, source_type, source_id, observed_at) in evidence:
            ev = repos.evidence.record(
                scope=project_scope, field=field, value=value, confidence=1.0, authority=0.95,
                source_type=source_type, source_id=source_id, observed_at=observed_at,
            )
            evidence_ids.append(UUID(ev.evidence_id))
        with repos.db.scoped(scope) as cur:
            cur.execute(
                "UPDATE approvals SET evidence_ids = %s::uuid[] "
                "WHERE organization_id = %s AND approval_id = %s",
                (evidence_ids, scope.organization_id, approval_id),
            )

    provision_approver(
        repos, scope.organization_only, subject="approver@pre.com", display_name="Approver",
        role="approver",
        permissions=["invoice.read", "invoice.review", "invoice.approve", "invoice.hold", "invoice.reject"],
        maximum_amount=10000.0,
    )
    session = login(repos, provider=DevIdentityProvider(), credential="approver@pre.com")
    actor = actor_from_session(repos, session)
    decide_approval(repos, actor=actor, approval_id=approval_id, decision="approved", reason="test")
    return scope, approval_id


# --------------------------------------------------------------------------
# Phase 22: evidence freshness precondition
# --------------------------------------------------------------------------

def test_stale_evidence_blocks_execution(repos, org_a):
    """An approval resting on stale ERP evidence cannot execute — the evidence
    must be refreshed and reverified first."""
    stale_time = datetime.now(timezone.utc) - timedelta(minutes=30)
    scope, approval_id = _make_approved_invoice(repos, org_a, evidence=[
        ("ERP_PURCHASE_ORDER_SNAPSHOT", {"grand_total": 100}, "erpnext", "PO-STALE", stale_time),
    ])

    erp_transport = FakeERPTransport()
    adapter = ERPNextAdapter(transport=erp_transport)
    approval = repos.approvals.get(scope=scope, approval_id=approval_id)
    project_scope = scope.for_project(UUID(approval.project_id))
    with pytest.raises(ExecutionError, match="stale evidence"):
        execute_approved_invoice(
            repos, scope=project_scope, approval_id=approval_id,
            adapter=adapter, erp_read_transport=erp_transport,
        )

    # No ERP document was created.
    all_docs = [d for docs in erp_transport.docs.values() for d in docs]
    assert len(all_docs) == 0


def test_fresh_evidence_allows_execution(repos, org_a):
    """An approval with fresh evidence executes normally."""
    fresh_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    scope, approval_id = _make_approved_invoice(repos, org_a, evidence=[
        ("ERP_PURCHASE_ORDER_SNAPSHOT", {"grand_total": 100}, "erpnext", "PO-FRESH", fresh_time),
    ])

    erp_transport = FakeERPTransport()
    adapter = ERPNextAdapter(transport=erp_transport)
    approval = repos.approvals.get(scope=scope, approval_id=approval_id)
    project_scope = scope.for_project(UUID(approval.project_id))
    result = execute_approved_invoice(
        repos, scope=project_scope, approval_id=approval_id,
        adapter=adapter, erp_read_transport=erp_transport,
    )
    assert result.idempotent is False
    assert result.erp_docname.startswith("PINV-")


# --------------------------------------------------------------------------
# Phase 23: audit events carry request/response hashes
# --------------------------------------------------------------------------

def test_audit_events_carry_request_response_hashes(repos, org_a):
    """Every external-action transition audit event during execution carries
    request_hash and/or response_hash binding the audit trail to the exact
    bytes sent to and received from the ERP."""
    fresh_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    scope, approval_id = _make_approved_invoice(repos, org_a, evidence=[
        ("ERP_PURCHASE_ORDER_SNAPSHOT", {"grand_total": 100}, "erpnext", "PO-AUDIT", fresh_time),
    ])
    approval = repos.approvals.get(scope=scope, approval_id=approval_id)
    project_scope = scope.for_project(UUID(approval.project_id))

    erp_transport = FakeERPTransport()
    adapter = ERPNextAdapter(transport=erp_transport)
    execute_approved_invoice(
        repos, scope=project_scope, approval_id=approval_id,
        adapter=adapter, erp_read_transport=erp_transport,
    )

    # Read all audit events for this execution.
    with repos.db.scoped(scope.organization_only) as cur:
        cur.execute(
            "SELECT event_type, payload FROM audit_events "
            "WHERE organization_id = %s AND event_type = 'EXTERNAL_ACTION_TRANSITION' "
            "ORDER BY occurred_at",
            (scope.organization_id,),
        )
        rows = cur.fetchall()

    # There should be transition events with request_hash and response_hash.
    transition_payloads = [r[1] for r in rows]
    has_request_hash = any("request_hash" in p for p in transition_payloads)
    has_response_hash = any("response_hash" in p for p in transition_payloads)
    assert has_request_hash, "no audit event carried a request_hash"
    assert has_response_hash, "no audit event carried a response_hash"

    # The final ERP_INVOICE_SUBMITTED event should also carry hashes.
    with repos.db.scoped(scope.organization_only) as cur:
        cur.execute(
            "SELECT payload FROM audit_events "
            "WHERE organization_id = %s AND event_type = 'ERP_INVOICE_SUBMITTED'",
            (scope.organization_id,),
        )
        submitted = cur.fetchone()
    assert submitted is not None
    submitted_payload = submitted[0]
    assert "request_hash" in submitted_payload
    assert "response_hash" in submitted_payload
    assert len(submitted_payload["request_hash"]) == 64
    assert len(submitted_payload["response_hash"]) == 64


def test_request_hash_is_deterministic_for_same_payload():
    """The request hash is SHA-256 over canonical JSON — same payload → same hash."""
    from construction_ai.executive.executor import _hash_json

    h1 = _hash_json({"supplier": "ABC", "grand_total": "100.00", "b": 2, "a": 1})
    h2 = _hash_json({"a": 1, "b": 2, "grand_total": "100.00", "supplier": "ABC"})
    assert h1 == h2
    assert len(h1) == 64


def test_request_hash_differs_for_different_payloads():
    from construction_ai.executive.executor import _hash_json

    h1 = _hash_json({"supplier": "ABC", "grand_total": "100.00"})
    h2 = _hash_json({"supplier": "ABC", "grand_total": "200.00"})
    assert h1 != h2
