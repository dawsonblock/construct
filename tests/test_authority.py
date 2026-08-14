"""v0.4.1 authority repair — human identity, authorization, and the atomic
approval decision.

These are the qualification invariants for the authority release, exercised
through the repositories and the approval service (the same path the API now
uses):

    UnauthorizedActor  !=> FinancialApproval
    CallerAssertion    !=> VerifiedFact        (no user_id request field exists)
    FinancialMutation  => AtomicAudit          (approval + decision + audit in one tx)
    Creator            !=> Approver            (separation of duties)

Needs a real PostgreSQL as the non-superuser app role, like the other
integration suites.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from construction_ai.approvals.policy import ApprovalPolicy
from construction_ai.approvals.service import (
    ApprovalAlreadyDecided,
    ApprovalNotFound,
    decide_approval,
)
from construction_ai.auth.authorization import AuthorizationError, can
from construction_ai.auth.identity import DevIdentityProvider
from construction_ai.auth.provisioning import provision_approver
from construction_ai.auth.sessions import AuthenticationError, actor_from_session, login
from construction_ai.persistence.db import Scope


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture()
def approval_for_org_a(repos, org_a):
    """A pending approval owned by org A, system-originated ('ai')."""
    # v0.5.0-rc3: Approvals that will be decided as 'approved' require a
    # project_id for state fingerprint computation (mandatory fingerprint).
    project = repos.projects.create(
        scope=org_a["scope"], reference=f"P-AUTH-{uuid4().hex[:6]}", name="Auth Test",
    )
    project_scope = org_a["scope"].for_project(UUID(project.project_id))
    approval = repos.approvals.create(
        scope=project_scope,
        reference=f"appr-{uuid4().hex[:8]}",
        approval_type="invoice",
        subject_type="invoice",
        subject_id=uuid4(),
        recommended_action="APPROVE",
        amount=Decimal("4760.00"),
        requested_by="ai",
    )
    return approval


@pytest.fixture()
def controller_a(repos, org_a):
    """A controller in org A with CAD 0–100,000 approval authority."""
    return provision_approver(
        repos, org_a["scope"],
        subject="controller@a", display_name="Dana Controller", role="controller",
        permissions=["invoice.read", "invoice.review", "invoice.approve", "invoice.hold", "invoice.reject"],
        maximum_amount=100000.0,
    )


@pytest.fixture()
def controller_session_a(repos, org_a, controller_a):
    return login(repos, provider=DevIdentityProvider(), credential="controller@a")


# --------------------------------------------------------------------------
# Identity: provider_subject -> user -> organization, never caller-supplied
# --------------------------------------------------------------------------


def test_a_subject_resolves_to_a_user_in_an_organization(repos, org_a, controller_a):
    identity = repos.users.resolve_identity(provider="dev", provider_subject="controller@a")
    assert identity is not None
    assert identity.user_id == controller_a
    assert identity.organization_id == org_a["organization_id"]


def test_an_unknown_subject_resolves_to_nothing(repos):
    assert repos.users.resolve_identity(provider="dev", provider_subject="nobody@a") is None


def test_login_issues_a_server_session_token(repos, controller_session_a, org_a):
    token = controller_session_a
    assert token.startswith("csess.")
    # The token carries the org so the resolver can scope the lookup under RLS.
    assert str(org_a["organization_id"]) in token


def test_a_session_resolves_to_an_authenticated_actor(repos, controller_session_a, controller_a, org_a):
    actor = actor_from_session(repos, controller_session_a)
    assert actor.user_id == controller_a
    assert actor.organization_id == org_a["organization_id"]
    assert "invoice.approve" in actor.permissions
    assert actor.authentication_strength == "dev"


def test_an_invalid_session_token_resolves_to_no_actor(repos):
    with pytest.raises(AuthenticationError):
        actor_from_session(repos, "csess.not-a-uuid.either")
    with pytest.raises(AuthenticationError):
        actor_from_session(repos, "")


def test_a_revoked_session_cannot_become_an_actor(repos, controller_session_a, org_a, controller_a):
    # Parse the session id out of the token and revoke it.
    session_id = UUID(controller_session_a.rsplit(".", 1)[1])
    assert repos.sessions.revoke(scope=Scope(org_a["organization_id"]), session_id=session_id) is True
    with pytest.raises(AuthenticationError):
        actor_from_session(repos, controller_session_a)


# --------------------------------------------------------------------------
# Authorization: DENY by default; permission required; authority limits
# --------------------------------------------------------------------------


def test_an_actor_with_no_approve_permission_cannot_approve(repos, org_a, approval_for_org_a):
    provision_approver(
        repos, org_a["scope"],
        subject="viewer@a", display_name="Vince Viewer", role="viewer",
        permissions=["invoice.read"],  # no invoice.approve
        maximum_amount=100000.0,
    )
    token = login(repos, provider=DevIdentityProvider(), credential="viewer@a")
    actor = actor_from_session(repos, token)
    assert not can(actor, "invoice.approve")
    with pytest.raises(AuthorizationError, match="invoice.approve"):
        decide_approval(repos, actor=actor, approval_id=UUID(approval_for_org_a.approval_id), decision="approved")


def test_an_approver_above_their_authority_limit_is_denied(repos, org_a):
    """A PM limited to 25,000 cannot approve a 30,000 invoice."""
    provision_approver(
        repos, org_a["scope"],
        subject="pm@a", display_name="Pat Manager", role="pm",
        permissions=["invoice.read", "invoice.approve"],
        maximum_amount=25000.0,
    )
    big_approval = repos.approvals.create(
        scope=org_a["scope"], reference=f"big-{uuid4().hex[:8]}", approval_type="invoice",
        subject_type="invoice", subject_id=uuid4(), recommended_action="APPROVE",
        amount=Decimal("30000.00"), requested_by="ai",
    )
    actor = actor_from_session(repos, login(repos, provider=DevIdentityProvider(), credential="pm@a"))
    with pytest.raises(AuthorizationError, match="outside every approval authority"):
        decide_approval(repos, actor=actor, approval_id=UUID(big_approval.approval_id), decision="approved")


def test_an_approver_within_their_limit_succeeds(repos, org_a, approval_for_org_a, controller_session_a):
    actor = actor_from_session(repos, controller_session_a)
    outcome = decide_approval(repos, actor=actor, approval_id=UUID(approval_for_org_a.approval_id), decision="approved")
    assert outcome.decision == "approved"


def test_currency_mismatch_blocks_approval(repos, org_a):
    """CAD authority does not authorize a USD invoice, even for a small amount."""
    provision_approver(
        repos, org_a["scope"],
        subject="cad-controller@a", display_name="Cass Controller", role="controller",
        permissions=["invoice.approve"], maximum_amount=100000.0, currency="CAD",
    )
    usd_approval = repos.approvals.create(
        scope=org_a["scope"], reference=f"usd-{uuid4().hex[:8]}", approval_type="invoice",
        subject_type="invoice", subject_id=uuid4(), recommended_action="APPROVE",
        amount=Decimal("100.00"), currency="USD", requested_by="ai",
    )
    actor = actor_from_session(repos, login(repos, provider=DevIdentityProvider(), credential="cad-controller@a"))
    with pytest.raises(AuthorizationError, match="USD"):
        decide_approval(repos, actor=actor, approval_id=UUID(usd_approval.approval_id), decision="approved")


# --------------------------------------------------------------------------
# Separation of duties: creator != approver
# --------------------------------------------------------------------------


def test_an_approver_cannot_approve_their_own_request(repos, org_a):
    controller = provision_approver(
        repos, org_a["scope"],
        subject="selfapp@a", display_name="Sue Selfapp", role="controller",
        permissions=["invoice.approve"], maximum_amount=100000.0,
    )
    own_request = repos.approvals.create(
        scope=org_a["scope"], reference=f"own-{uuid4().hex[:8]}", approval_type="invoice",
        subject_type="invoice", subject_id=uuid4(), recommended_action="APPROVE",
        amount=Decimal("1000.00"), requested_by=str(controller),  # originated by this user
    )
    actor = actor_from_session(repos, login(repos, provider=DevIdentityProvider(), credential="selfapp@a"))
    with pytest.raises(AuthorizationError, match="creator != approver"):
        decide_approval(repos, actor=actor, approval_id=UUID(own_request.approval_id), decision="approved")


def test_system_originated_approvals_have_no_human_creator(repos, org_a, approval_for_org_a, controller_session_a):
    """An 'ai'-originated approval can be approved by any authorized human (SoD is vacuous)."""
    actor = actor_from_session(repos, controller_session_a)
    outcome = decide_approval(repos, actor=actor, approval_id=UUID(approval_for_org_a.approval_id), decision="approved")
    assert outcome.decision == "approved"


def test_dual_approval_requires_a_second_distinct_approver(repos, org_a):
    """The separation-of-duties rule for high-value payments: a second *distinct*
    approver. Full multi-signature accumulation (pending until N distinct
    approvals) lands with the invoice state machine; v0.4.1 implements the rule
    itself and exercises it directly here."""
    from construction_ai.auth.authorization import separation_of_duties_holds

    first = provision_approver(
        repos, org_a["scope"], subject="first@a", display_name="First Approver", role="controller",
        permissions=["invoice.approve"], maximum_amount=100000.0,
    )
    actor1 = actor_from_session(repos, login(repos, provider=DevIdentityProvider(), credential="first@a"))
    # Below threshold: distinct-user check is not engaged.
    assert separation_of_duties_holds(
        actor=actor1, requested_by="ai", previous_approvers=(first,), require_distinct_users=False
    ).allowed
    # Above threshold and the same user already approved: denied.
    assert not separation_of_duties_holds(
        actor=actor1, requested_by="ai", previous_approvers=(first,), require_distinct_users=True
    ).allowed
    # A different second approver is allowed.
    provision_approver(
        repos, org_a["scope"], subject="second@a", display_name="Second Approver", role="controller",
        permissions=["invoice.approve"], maximum_amount=100000.0,
    )
    actor2 = actor_from_session(repos, login(repos, provider=DevIdentityProvider(), credential="second@a"))
    assert separation_of_duties_holds(
        actor=actor2, requested_by="ai", previous_approvers=(first,), require_distinct_users=True
    ).allowed


def test_dual_approval_threshold_is_currency_scoped():
    policy = ApprovalPolicy(dual_approval_threshold=Decimal("25000"), dual_approval_currency="CAD")
    assert policy.requires_second_distinct_approver(Decimal("40000"), "CAD") is True
    assert policy.requires_second_distinct_approver(Decimal("10000"), "CAD") is False
    # A USD amount does not trigger a CAD dual-approval rule.
    assert policy.requires_second_distinct_approver(Decimal("40000"), "USD") is False


# --------------------------------------------------------------------------
# Atomicity: approval + decision + audit commit together
# --------------------------------------------------------------------------


def test_a_decision_records_an_approval_decision_row(repos, org_a, approval_for_org_a, controller_session_a, controller_a):
    actor = actor_from_session(repos, controller_session_a)
    outcome = decide_approval(repos, actor=actor, approval_id=UUID(approval_for_org_a.approval_id), decision="approved")
    record = repos.approval_decisions.latest_for(scope=org_a["scope"], approval_id=UUID(approval_for_org_a.approval_id))
    assert record is not None
    assert record["decision"] == "approved"
    assert record["actor_id"] == controller_a
    assert record["policy_version"] == outcome.policy_version


def test_approval_and_audit_are_atomic(repos, org_a, approval_for_org_a, controller_session_a):
    """A committed decision always has a matching audit event and decision row."""
    actor = actor_from_session(repos, controller_session_a)
    decide_approval(repos, actor=actor, approval_id=UUID(approval_for_org_a.approval_id), decision="approved")
    approval = repos.approvals.get(scope=org_a["scope"], approval_id=UUID(approval_for_org_a.approval_id))
    assert approval.status.value == "approved"
    audit = repos.audit.for_object(scope=org_a["scope"], object_type="approval", object_id=UUID(approval_for_org_a.approval_id))
    assert any(e["event_type"] == "APPROVAL_DECIDED" for e in audit)
    assert repos.audit.verify_chain(scope=org_a["scope"]) is True
    decision = repos.approval_decisions.latest_for(scope=org_a["scope"], approval_id=UUID(approval_for_org_a.approval_id))
    assert decision is not None
    # Invariant: ApprovalExists <=> AuditEventExists for committed decisions.
    assert approval.approved_by == actor.display_name


def test_an_already_decided_approval_cannot_be_re_decided(repos, org_a, approval_for_org_a, controller_session_a):
    actor = actor_from_session(repos, controller_session_a)
    decide_approval(repos, actor=actor, approval_id=UUID(approval_for_org_a.approval_id), decision="approved")
    with pytest.raises(ApprovalAlreadyDecided):
        decide_approval(repos, actor=actor, approval_id=UUID(approval_for_org_a.approval_id), decision="rejected")


def test_a_nonexistent_approval_is_not_found(repos, org_a, controller_session_a):
    actor = actor_from_session(repos, controller_session_a)
    with pytest.raises(ApprovalNotFound):
        decide_approval(repos, actor=actor, approval_id=uuid4(), decision="approved")


# --------------------------------------------------------------------------
# Tenant isolation for the new authority tables (RLS)
# --------------------------------------------------------------------------


def test_b_cannot_resolve_as_identity_of_a(repos, org_a, org_b, controller_a):
    """B's scope sees none of A's users."""
    b_viewer = provision_approver(
        repos, org_b["scope"], subject="viewer@b", display_name="B Viewer", role="viewer",
        permissions=["invoice.read"], maximum_amount=1000.0,
    )
    # A's controller is invisible from B's scope.
    assert repos.users.get(scope=org_b["scope"], user_id=controller_a) is None
    # B's viewer is invisible from A's scope.
    assert repos.users.get(scope=org_a["scope"], user_id=b_viewer) is None


def test_b_actor_cannot_decide_as_approval(repos, org_a, org_b, approval_for_org_a, controller_a):
    """A B-scoped actor deciding A's approval sees nothing (404-equivalent)."""
    provision_approver(
        repos, org_b["scope"], subject="controller@b", display_name="B Controller", role="controller",
        permissions=["invoice.approve"], maximum_amount=100000.0,
    )
    b_actor = actor_from_session(repos, login(repos, provider=DevIdentityProvider(), credential="controller@b"))
    assert b_actor.organization_id == org_b["organization_id"]
    with pytest.raises(ApprovalNotFound):
        decide_approval(repos, actor=b_actor, approval_id=UUID(approval_for_org_a.approval_id), decision="approved")


def test_a_disabled_user_cannot_become_an_actor(repos, org_a, controller_a, controller_session_a):
    assert repos.users.disable(scope=org_a["scope"], user_id=controller_a) is True
    with pytest.raises(AuthenticationError):
        actor_from_session(repos, controller_session_a)


# --------------------------------------------------------------------------
# Permission catalog: the DB matches the code
# --------------------------------------------------------------------------


def test_the_permission_catalog_in_the_db_matches_code(repos):
    from construction_ai.auth.permissions import ALL_PERMISSIONS

    with repos.db.unscoped_auth() as cur:
        cur.execute("SELECT name FROM permissions ORDER BY name")
        db_permissions = {r[0] for r in cur.fetchall()}
    assert db_permissions == ALL_PERMISSIONS
