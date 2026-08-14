"""rc4 Phase 21 — Expanded reconciliation tests.

Tests the bounded reconciliation logic with:
- exact remote-document lookup
- exact idempotency-key lookup
- local transaction reference lookup
- zero search results (PROVEN_ABSENT)
- transient lookup failures
- ambiguous multiple matches (AMBIGUOUS)
- draft result (REMOTE_DRAFT)
- submitted result (REMOTE_SUBMITTED)
- mismatched result (REMOTE_MISMATCH)
- retry refusal for ambiguous/unknown states
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from construction_ai.executive.reconcile_unknown import (
    AMBIGUOUS,
    PROVEN_ABSENT,
    REMOTE_DRAFT,
    REMOTE_MISMATCH,
    REMOTE_SUBMITTED,
    reconcile_external_action,
)


class FakeERPTransport:
    """Configurable fake ERP transport for reconciliation tests."""

    def __init__(self):
        self._docs: dict[str, dict] = {}
        self._search_results: dict[str, list[dict]] = {}
        self._fail_get = False

    def add_doc(self, name: str, docstatus: int = 1, **fields):
        doc = {"name": name, "docstatus": docstatus, **fields}
        self._docs[name] = doc

    def add_search_result(self, key: str, results: list[dict]):
        self._search_results[key] = results

    def fail_get(self, fail: bool = True):
        self._fail_get = fail

    def get(self, path, params=None):
        if self._fail_get:
            raise ConnectionError("transient ERP failure")
        from urllib.parse import unquote
        # Direct document read: /api/resource/Purchase%20Invoice/{name}
        if "/api/resource/" in path:
            rest = path.split("/api/resource/")[-1]
            parts = rest.split("/")
            if len(parts) == 2:
                docname = unquote(parts[1])
                if docname in self._docs:
                    return {"data": self._docs[docname]}
                return {"data": {}}
        # Search: /api/resource/Purchase Invoice with filters
        if "/api/resource/" in path and params and "filters" in params:
            import json
            filters = json.loads(params["filters"]) if isinstance(params["filters"], str) else params["filters"]
            for f in filters:
                if f[0] == "construct_idempotency_key":
                    key = f"idek:{f[2]}"
                    return {"data": self._search_results.get(key, [])}
                if f[0] == "bill_no":
                    for f2 in filters:
                        if f2[0] == "supplier":
                            key = f"inv:{f[2]}/{f2[2]}"
                            return {"data": self._search_results.get(key, [])}
        return {"data": []}


def _make_unknown_action(repos, scope, *, remote_document_id=None, erp_idempotency_key=None):
    """Create an UNKNOWN external action for reconciliation testing."""
    action = repos.external_actions.reserve(
        scope=scope.organization_only,
        operation="erp_submit_purchase_invoice",
        idempotency_key=f"recon-{uuid4()}",
        subject_type="invoice",
        subject_id=uuid4(),
    )
    repos.external_actions.transition(
        scope=scope.organization_only,
        action_id=action.action_id,
        from_status="pending",
        to_status="unknown",
        remote_document_id=remote_document_id,
        remote_state="remote_unknown",
        erp_idempotency_key=erp_idempotency_key,
        last_error="test unknown state",
    )
    return action


# -- Direct remote document lookup ------------------------------------------

def test_direct_remote_document_lookup_submitted(repos, org_a):
    """Reconciliation finds a submitted document by remote_document_id."""
    scope = org_a["scope"]
    transport = FakeERPTransport()
    transport.add_doc("PINV-FOUND", docstatus=1, supplier="V1", bill_no="INV-1")

    action = _make_unknown_action(repos, scope, remote_document_id="PINV-FOUND")
    outcome = reconcile_external_action(
        repos, scope=scope, action_id=action.action_id, erp_read_transport=transport,
    )
    assert outcome.classification == REMOTE_SUBMITTED
    assert outcome.new_status == "confirmed"
    assert outcome.remote_document_id == "PINV-FOUND"


def test_direct_remote_document_lookup_draft(repos, org_a):
    """Reconciliation finds a draft document (docstatus=0) → REMOTE_DRAFT."""
    scope = org_a["scope"]
    transport = FakeERPTransport()
    transport.add_doc("PINV-DRAFT", docstatus=0, supplier="V1", bill_no="INV-1")

    action = _make_unknown_action(repos, scope, remote_document_id="PINV-DRAFT")
    outcome = reconcile_external_action(
        repos, scope=scope, action_id=action.action_id, erp_read_transport=transport,
    )
    assert outcome.classification == REMOTE_DRAFT
    assert outcome.new_status == "unknown"  # Stays unknown — not confirmed


def test_direct_remote_document_not_found_proven_absent(repos, org_a):
    """Reconciliation with no remote_document_id and no matches → PROVEN_ABSENT."""
    scope = org_a["scope"]
    transport = FakeERPTransport()

    action = _make_unknown_action(repos, scope)
    outcome = reconcile_external_action(
        repos, scope=scope, action_id=action.action_id, erp_read_transport=transport,
    )
    assert outcome.classification == PROVEN_ABSENT
    assert outcome.new_status == "failed_retryable"


# -- Idempotency key lookup -------------------------------------------------

def test_idempotency_key_lookup_finds_submitted(repos, org_a):
    """Reconciliation finds a document by ERP idempotency key."""
    scope = org_a["scope"]
    transport = FakeERPTransport()
    transport.add_search_result("idek:construct-test-key", [
        {"name": "PINV-IDEK", "docstatus": 1, "supplier": "V1", "bill_no": "INV-1"}
    ])

    action = _make_unknown_action(repos, scope, erp_idempotency_key="construct-test-key")
    outcome = reconcile_external_action(
        repos, scope=scope, action_id=action.action_id,
        erp_read_transport=transport, erp_idempotency_key="construct-test-key",
    )
    assert outcome.classification == REMOTE_SUBMITTED
    assert outcome.remote_document_id == "PINV-IDEK"


# -- Invoice number + supplier lookup ---------------------------------------

def test_invoice_number_supplier_lookup_finds_submitted(repos, org_a):
    """Reconciliation finds a document by invoice number + supplier."""
    scope = org_a["scope"]
    transport = FakeERPTransport()
    transport.add_search_result("inv:INV-123/Vendor A", [
        {"name": "PINV-INV", "docstatus": 1, "supplier": "Vendor A", "bill_no": "INV-123"}
    ])

    action = _make_unknown_action(repos, scope)
    outcome = reconcile_external_action(
        repos, scope=scope, action_id=action.action_id,
        erp_read_transport=transport, invoice_number="INV-123", supplier="Vendor A",
    )
    assert outcome.classification == REMOTE_SUBMITTED
    assert outcome.remote_document_id == "PINV-INV"


# -- Ambiguous results ------------------------------------------------------

def test_ambiguous_multiple_matches_fails_terminal(repos, org_a):
    """Multiple matches → AMBIGUOUS → failed_terminal."""
    scope = org_a["scope"]
    transport = FakeERPTransport()
    transport.add_search_result("inv:INV-123/Vendor A", [
        {"name": "PINV-001", "docstatus": 1, "supplier": "Vendor A", "bill_no": "INV-123"},
        {"name": "PINV-002", "docstatus": 1, "supplier": "Vendor A", "bill_no": "INV-123"},
    ])

    action = _make_unknown_action(repos, scope)
    outcome = reconcile_external_action(
        repos, scope=scope, action_id=action.action_id,
        erp_read_transport=transport, invoice_number="INV-123", supplier="Vendor A",
    )
    assert outcome.classification == AMBIGUOUS
    assert outcome.new_status == "failed_terminal"


# -- Mismatch result --------------------------------------------------------

def test_mismatch_docstatus_fails_terminal(repos, org_a):
    """Document with unexpected docstatus (e.g. 2=cancelled) → REMOTE_MISMATCH."""
    scope = org_a["scope"]
    transport = FakeERPTransport()
    transport.add_doc("PINV-CANCELLED", docstatus=2, supplier="V1", bill_no="INV-1")

    action = _make_unknown_action(repos, scope, remote_document_id="PINV-CANCELLED")
    outcome = reconcile_external_action(
        repos, scope=scope, action_id=action.action_id, erp_read_transport=transport,
    )
    assert outcome.classification == REMOTE_MISMATCH
    assert outcome.new_status == "failed_terminal"


# -- Transient failures -----------------------------------------------------

def test_transient_lookup_failure_raises(repos, org_a):
    """Transient ERP failures propagate — no false PROVEN_ABSENT."""
    scope = org_a["scope"]
    transport = FakeERPTransport()
    transport.fail_get(True)

    # Provide a remote_document_id so the transport is actually called.
    action = _make_unknown_action(repos, scope, remote_document_id="PINV-ANY")
    with pytest.raises(ConnectionError):
        reconcile_external_action(
            repos, scope=scope, action_id=action.action_id, erp_read_transport=transport,
        )


# -- Non-UNKNOWN action refuses reconciliation -------------------------------

def test_non_unknown_action_refuses_reconciliation(repos, org_a):
    """Reconciliation only applies to UNKNOWN actions."""
    scope = org_a["scope"]
    action = repos.external_actions.reserve(
        scope=scope.organization_only,
        operation="test_op",
        idempotency_key=f"not-unknown-{uuid4()}",
        subject_type="invoice", subject_id=uuid4(),
    )
    # Action is PENDING, not UNKNOWN.
    transport = FakeERPTransport()
    with pytest.raises(ValueError, match="not unknown"):
        reconcile_external_action(
            repos, scope=scope, action_id=action.action_id, erp_read_transport=transport,
        )


# -- NoSearchResult != ProvenAbsent -----------------------------------------

def test_zero_search_results_is_proven_absent(repos, org_a):
    """Zero results from all searches → PROVEN_ABSENT (safe to retry).

    This is the key invariant: we only declare PROVEN_ABSENT after exhausting
    all available identifiers. A single zero result is not proof of absence.
    """
    scope = org_a["scope"]
    transport = FakeERPTransport()
    # No documents, no search results.

    action = _make_unknown_action(repos, scope, erp_idempotency_key="construct-key")
    outcome = reconcile_external_action(
        repos, scope=scope, action_id=action.action_id,
        erp_read_transport=transport,
        invoice_number="INV-123", supplier="Vendor A",
        erp_idempotency_key="construct-key",
    )
    assert outcome.classification == PROVEN_ABSENT
    assert outcome.new_status == "failed_retryable"
    # Verify all identifiers were tried.
    assert len(outcome.searched_by) >= 2  # idempotency key + invoice/supplier
