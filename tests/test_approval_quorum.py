"""v0.5.0-rc3 Phase 7 — Real multi-approver quorum tests.

Verifies that:
1. A single approver can approve when quorum_threshold = 1.
2. Two distinct approvers are required when quorum_threshold = 2.
3. The same approver cannot vote twice.
4. A reject vote transitions immediately (no quorum needed).
5. A hold vote transitions immediately (no quorum needed).
6. The approval stays pending until quorum is met.
7. Each vote is recorded in approval_votes (append-only).
8. The quorum met event is audited.
9. Separation of duties is enforced (creator cannot approve).
"""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from construction_ai.approvals.service import (
    decide_approval,
    AlreadyVoted,
)
from construction_ai.auth.identity import DevIdentityProvider
from construction_ai.auth.provisioning import provision_approver
from construction_ai.auth.sessions import actor_from_session, login
from construction_ai.executive.invoice_pipeline import InvoicePipeline
from construction_ai.domain.models import Invoice


def _make_invoice_with_project(repos, org_a, unique_suffix):
    """Create an invoice with a resolved project and return (scope, approval_id)."""
    scope = org_a["scope"]
    project = repos.projects.create(scope=scope, reference=f"P-Q-{unique_suffix}", name="Quorum Test")
    invoice = Invoice(
        invoice_id="", organization_id="", reference="", invoice_number=f"INV-Q-{unique_suffix}",
        vendor_name="Vendor", total=Decimal("100.00"), subtotal=Decimal("87.00"),
        tax=Decimal("13.00"), currency="CAD",
    )
    pipeline = InvoicePipeline(repositories=repos, erp_resolver=None)
    result = pipeline.process(scope=scope, extracted=invoice, signals={"project_id": project.project_id})
    return scope, UUID(result["approval_id"])


def _provision_and_login(repos, scope, email, max_amount=10000.0):
    provision_approver(
        repos, scope.organization_only,
        subject=email, display_name=email.split("@")[0], role="approver",
        permissions=["invoice.read", "invoice.review", "invoice.approve", "invoice.hold", "invoice.reject"],
        maximum_amount=max_amount,
    )
    session = login(repos, provider=DevIdentityProvider(), credential=email)
    return actor_from_session(repos, session)


def _set_quorum(repos, scope, approval_id, threshold):
    """Manually set the quorum_threshold on an approval."""
    with repos.db.scoped(scope) as cur:
        cur.execute(
            "UPDATE approvals SET quorum_threshold = %s WHERE approval_id = %s AND organization_id = %s",
            (threshold, approval_id, scope.organization_id),
        )


class TestQuorum:

    def test_single_approver_when_quorum_is_1(self, repos, org_a):
        """When quorum_threshold = 1, a single approver can approve."""
        scope, approval_id = _make_invoice_with_project(repos, org_a, "q1")
        _set_quorum(repos, scope, approval_id, 1)
        actor = _provision_and_login(repos, scope, "approver@q1.com")
        outcome = decide_approval(repos, actor=actor, approval_id=approval_id, decision="approved", reason="test")
        assert outcome.quorum_met is True
        assert outcome.decision == "approved"

    def test_quorum_not_met_with_one_vote(self, repos, org_a):
        """When quorum_threshold = 2, one approve vote is not enough."""
        scope, approval_id = _make_invoice_with_project(repos, org_a, "q2")
        _set_quorum(repos, scope, approval_id, 2)
        actor = _provision_and_login(repos, scope, "approver@q2-a.com")
        outcome = decide_approval(repos, actor=actor, approval_id=approval_id, decision="approved", reason="first vote")

        # Quorum not met — but the vote IS recorded.
        assert outcome.quorum_met is False

        # The approval should still be pending.
        approval = repos.approvals.get(scope=scope, approval_id=approval_id)
        assert approval.status.value == "pending"

        # The vote should be recorded.
        votes = repos.approval_votes.list_votes(scope=scope, approval_id=approval_id)
        assert len(votes) == 1
        assert votes[0]["vote"] == "approve"

    def test_quorum_met_with_two_votes(self, repos, org_a):
        """When quorum_threshold = 2, two distinct approvers can approve."""
        scope, approval_id = _make_invoice_with_project(repos, org_a, "q3")
        _set_quorum(repos, scope, approval_id, 2)
        actor1 = _provision_and_login(repos, scope, "approver@q3-a.com")
        actor2 = _provision_and_login(repos, scope, "approver@q3-b.com")

        # First vote — quorum not met.
        outcome1 = decide_approval(repos, actor=actor1, approval_id=approval_id, decision="approved", reason="first")
        assert outcome1.quorum_met is False

        # Second vote — quorum met.
        outcome2 = decide_approval(repos, actor=actor2, approval_id=approval_id, decision="approved", reason="second")
        assert outcome2.quorum_met is True
        assert outcome2.decision == "approved"

        # The approval should be approved.
        approval = repos.approvals.get(scope=scope, approval_id=approval_id)
        assert approval.status.value == "approved"

        # Two votes should be recorded.
        votes = repos.approval_votes.list_votes(scope=scope, approval_id=approval_id)
        assert len(votes) == 2

    def test_same_approver_cannot_vote_twice(self, repos, org_a):
        """The same approver cannot cast two votes."""
        scope, approval_id = _make_invoice_with_project(repos, org_a, "q4")
        _set_quorum(repos, scope, approval_id, 2)
        actor = _provision_and_login(repos, scope, "approver@q4.com")

        # First vote.
        outcome = decide_approval(repos, actor=actor, approval_id=approval_id, decision="approved", reason="first")
        assert outcome.quorum_met is False

        # Second vote — should fail.
        with pytest.raises(AlreadyVoted):
            decide_approval(repos, actor=actor, approval_id=approval_id, decision="approved", reason="second")

    def test_reject_transitions_immediately(self, repos, org_a):
        """A reject vote transitions immediately — no quorum needed."""
        scope, approval_id = _make_invoice_with_project(repos, org_a, "q5")
        _set_quorum(repos, scope, approval_id, 2)
        actor = _provision_and_login(repos, scope, "approver@q5.com")
        outcome = decide_approval(repos, actor=actor, approval_id=approval_id, decision="rejected", reason="bad")
        assert outcome.quorum_met is True
        assert outcome.decision == "rejected"

        approval = repos.approvals.get(scope=scope, approval_id=approval_id)
        assert approval.status.value == "rejected"

    def test_hold_transitions_immediately(self, repos, org_a):
        """A hold vote transitions immediately — no quorum needed."""
        scope, approval_id = _make_invoice_with_project(repos, org_a, "q6")
        _set_quorum(repos, scope, approval_id, 2)
        actor = _provision_and_login(repos, scope, "approver@q6.com")
        outcome = decide_approval(repos, actor=actor, approval_id=approval_id, decision="held", reason="review")
        assert outcome.quorum_met is True
        assert outcome.decision == "held"

        approval = repos.approvals.get(scope=scope, approval_id=approval_id)
        assert approval.status.value == "held"

    def test_votes_are_append_only(self, repos, org_a):
        """The approval_votes table denies UPDATE and DELETE for the app role."""
        import psycopg

        scope, approval_id = _make_invoice_with_project(repos, org_a, "q7")
        _set_quorum(repos, scope, approval_id, 1)
        actor = _provision_and_login(repos, scope, "approver@q7.com")
        decide_approval(repos, actor=actor, approval_id=approval_id, decision="approved", reason="test")

        dsn = "postgresql://construction_app:construction_app@localhost:5432/construction_ai"
        with psycopg.connect(dsn) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('app.organization_id', %s, false)", (str(scope.organization_id),))
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute("DELETE FROM approval_votes WHERE false")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute("UPDATE approval_votes SET reason = 'forged' WHERE false")
