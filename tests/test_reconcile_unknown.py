"""v0.5.0-rc2 Phase 2 — Unknown-external-state handling tests.

Verifies that:
1. UNKNOWN action with one matching ERP document → CONFIRMED.
2. UNKNOWN action with no matching ERP document → FAILED_RETRYABLE.
3. UNKNOWN action with multiple matching documents → FAILED_TERMINAL.
4. UNKNOWN action with remote_document_id → direct readback confirmation.
5. Reconciliation of a non-UNKNOWN action raises ValueError.
6. Every reconciliation is audited.
7. UNKNOWN ⇒ RECONCILE, not UNKNOWN ⇒ RETRY.
"""
from __future__ import annotations

from uuid import UUID

import pytest

from construction_ai.executive.reconcile_unknown import (
    reconcile_external_action,
)


class FakeERPTransport:
    def __init__(self, docs=None, search_results=None):
        self.docs = docs or {}
        self.search_results = search_results or []

    def get(self, path, params=None):
        from urllib.parse import unquote
        # Direct document read: /api/resource/{doctype}/{name}
        parts = path.split("/api/resource/")
        if len(parts) == 2:
            rest = parts[1].split("/")
            if len(rest) == 2:
                doctype, name = unquote(rest[0]), unquote(rest[1])
                for row in self.docs.get(doctype, []):
                    if row.get("name") == name:
                        return {"data": row}
                return {"data": {}}
            # List with filters: /api/resource/{doctype}
            if len(rest) == 1 and params and "filters" in params:
                return {"data": self.search_results}
        return {"data": {}}


@pytest.fixture()
def setup_unknown_action(repos, org_a):
    """Create an external action in UNKNOWN state."""
    scope = org_a["scope"]
    action = repos.external_actions.reserve(
        scope=scope, operation="erp_submit_purchase_invoice",
        idempotency_key="reconcile-test-1",
        subject_type="invoice", subject_id=UUID(int=1),
    )
    repos.external_actions.transition(
        scope=scope, action_id=action.action_id,
        from_status="pending", to_status="executing",
    )
    repos.external_actions.transition(
        scope=scope, action_id=action.action_id,
        from_status="executing", to_status="unknown",
        remote_document_id=None,
        last_error="submit timed out",
    )
    return scope, action.action_id


class TestReconcileUnknown:

    def test_one_match_confirms(self, repos, org_a, setup_unknown_action):
        """UNKNOWN with one matching ERP document → CONFIRMED."""
        scope, action_id = setup_unknown_action
        transport = FakeERPTransport(
            search_results=[{"name": "PINV-0001", "supplier": "Vendor", "bill_no": "INV-001", "docstatus": 1}],
        )
        outcome = reconcile_external_action(
            repos, scope=scope, action_id=action_id,
            erp_read_transport=transport,
            invoice_number="INV-001", supplier="Vendor",
        )
        assert outcome.new_status == "confirmed"
        assert outcome.remote_document_id == "PINV-0001"
        action = repos.external_actions.get(scope=scope, action_id=action_id)
        assert action.status == "confirmed"

    def test_no_match_failed_retryable(self, repos, org_a, setup_unknown_action):
        """UNKNOWN with no matching ERP document → FAILED_RETRYABLE."""
        scope, action_id = setup_unknown_action
        transport = FakeERPTransport(search_results=[])
        outcome = reconcile_external_action(
            repos, scope=scope, action_id=action_id,
            erp_read_transport=transport,
            invoice_number="INV-001", supplier="Vendor",
        )
        assert outcome.new_status == "failed_retryable"
        action = repos.external_actions.get(scope=scope, action_id=action_id)
        assert action.status == "failed_retryable"

    def test_multiple_matches_failed_terminal(self, repos, org_a, setup_unknown_action):
        """UNKNOWN with multiple matching documents → FAILED_TERMINAL."""
        scope, action_id = setup_unknown_action
        transport = FakeERPTransport(
            search_results=[
                {"name": "PINV-0001", "supplier": "Vendor", "bill_no": "INV-001", "docstatus": 1},
                {"name": "PINV-0002", "supplier": "Vendor", "bill_no": "INV-001", "docstatus": 1},
            ],
        )
        outcome = reconcile_external_action(
            repos, scope=scope, action_id=action_id,
            erp_read_transport=transport,
            invoice_number="INV-001", supplier="Vendor",
        )
        assert outcome.new_status == "failed_terminal"
        action = repos.external_actions.get(scope=scope, action_id=action_id)
        assert action.status == "failed_terminal"

    def test_remote_document_id_direct_readback(self, repos, org_a):
        """UNKNOWN with remote_document_id → direct readback confirmation."""
        scope = org_a["scope"]
        action = repos.external_actions.reserve(
            scope=scope, operation="erp_submit_purchase_invoice",
            idempotency_key="reconcile-test-2",
        )
        repos.external_actions.transition(scope=scope, action_id=action.action_id, from_status="pending", to_status="executing")
        repos.external_actions.transition(
            scope=scope, action_id=action.action_id, from_status="executing", to_status="unknown",
            remote_document_id="PINV-0099", last_error="ambiguous",
        )
        transport = FakeERPTransport(
            docs={"Purchase Invoice": [{"name": "PINV-0099", "docstatus": 1, "supplier": "V", "bill_no": "I"}]},
        )
        outcome = reconcile_external_action(
            repos, scope=scope, action_id=action.action_id,
            erp_read_transport=transport,
        )
        assert outcome.new_status == "confirmed"
        assert outcome.remote_document_id == "PINV-0099"

    def test_non_unknown_raises(self, repos, org_a):
        """Reconciliation of a non-UNKNOWN action raises ValueError."""
        scope = org_a["scope"]
        action = repos.external_actions.reserve(
            scope=scope, operation="erp_submit_purchase_invoice",
            idempotency_key="reconcile-test-3",
        )
        # Action is PENDING, not UNKNOWN.
        with pytest.raises(ValueError, match="not unknown"):
            reconcile_external_action(
                repos, scope=scope, action_id=action.action_id,
                erp_read_transport=FakeERPTransport(),
            )

    def test_reconciliation_is_audited(self, repos, org_a, setup_unknown_action):
        """Every reconciliation produces an audit event."""
        scope, action_id = setup_unknown_action
        transport = FakeERPTransport(
            search_results=[{"name": "PINV-0001", "supplier": "V", "bill_no": "I", "docstatus": 1}],
        )
        reconcile_external_action(
            repos, scope=scope, action_id=action_id,
            erp_read_transport=transport,
            invoice_number="INV-001", supplier="Vendor",
        )
        with repos.db.scoped(scope) as cur:
            cur.execute(
                "SELECT event_type FROM audit_events WHERE event_type = 'EXTERNAL_ACTION_RECONCILED' AND object_id = %s",
                (action_id,),
            )
            assert cur.fetchone() is not None

    def test_unknown_does_not_auto_retry(self, repos, org_a, setup_unknown_action):
        """UNKNOWN ⇒ RECONCILE, not UNKNOWN ⇒ RETRY.

        The executor refuses to execute an UNKNOWN action — the caller must
        reconcile first. This test verifies the action stays UNKNOWN when no
        reconciliation is performed.
        """
        scope, action_id = setup_unknown_action
        # Without reconciliation, the action remains UNKNOWN.
        action = repos.external_actions.get(scope=scope, action_id=action_id)
        assert action.status == "unknown"
        # The executor's code explicitly checks for UNKNOWN and raises.
        # (Verified in test_external_action_state_machine.py::test_unknown_action_requires_reconciliation)
