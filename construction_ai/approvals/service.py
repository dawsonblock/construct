"""The authoritative approval decision path.

One PostgreSQL transaction, one outcome:

    BEGIN
      SELECT approval FOR UPDATE
      validate current state (must be pending)
      validate actor (active, authenticated)
      validate permission (approve | reject | hold)
      validate approval authority (amount, currency, project, valid period)
      validate separation of duties (creator != approver; dual approval if required)
      compute state fingerprint (mandatory for 'approved', Phase 4/5)
      UPDATE approval (with state_fingerprint)
      INSERT approval_decision
      append audit event
    COMMIT

If any step fails the whole transaction rolls back. Invariant: a committed
approval decision always has a matching audit event and a matching
approval_decisions row. The caller never supplies an identity; the
`AuthenticatedActor` is server-derived from the session.

v0.5.0-rc3 (Phase 4/5): The state fingerprint is now computed INSIDE the
transaction and is mandatory for 'approved' decisions. If the fingerprint
cannot be computed, the approval is refused — fail closed, not fail open.
The separate post-commit record_state_fingerprint() call is eliminated.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from construction_ai.approvals.policy import ApprovalPolicy, DEFAULT_POLICY
from construction_ai.auth.authorization import AuthorizationError, authority_for, can, separation_of_duties_holds
from construction_ai.auth.models import AuthenticatedActor
from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories import Repositories

# Decision -> permission required. hold uses invoice.hold, etc.
_DECISION_PERMISSION = {
    "approved": "invoice.approve",
    "rejected": "invoice.reject",
    "held": "invoice.hold",
}


class ApprovalDecisionError(Exception):
    """A validation failure that prevents the decision. Fail closed."""


class ApprovalNotFound(ApprovalDecisionError):
    pass


class ApprovalAlreadyDecided(ApprovalDecisionError):
    def __init__(self, current_status: str):
        super().__init__(f"approval already {current_status}")
        self.current_status = current_status


class FingerprintUnavailable(ApprovalDecisionError):
    """The state fingerprint could not be computed. Fail closed (Phase 4)."""


class AlreadyVoted(ApprovalDecisionError):
    """The actor has already cast a vote on this approval (Phase 7)."""


class QuorumNotMet(ApprovalDecisionError):
    """The approval requires more votes to reach quorum (Phase 7)."""


@dataclass(frozen=True)
class DecisionOutcome:
    approval_id: UUID
    decision: str
    actor_id: UUID
    decision_id: UUID
    policy_version: str
    state_fingerprint: str | None
    quorum_met: bool  # True if the approval transitioned to 'approved'


def decide_approval(
    repos: Repositories,
    *,
    actor: AuthenticatedActor,
    approval_id: UUID,
    decision: str,
    reason: str = "",
    policy: ApprovalPolicy = DEFAULT_POLICY,
) -> DecisionOutcome:
    """The only authoritative way to decide an approval.

    v0.5.0-rc3 (Phase 7): For approvals with quorum_threshold > 1, this casts
    a vote rather than immediately transitioning the approval. The approval
    only transitions to 'approved' when the quorum is met by distinct approve
    votes from distinct approvers.

    For 'rejected' and 'held' decisions, the approval transitions immediately
    (a single reject or hold is terminal — no quorum needed).
    """
    if decision not in _DECISION_PERMISSION:
        raise ApprovalDecisionError(f"unsupported decision: {decision!r}")

    scope = Scope(actor.organization_id)
    with repos.db.transaction():
        approval = repos.approvals.get_for_update(scope=scope, approval_id=approval_id)
        if approval is None:
            raise ApprovalNotFound("approval not found")
        if approval.status.value != "pending":
            raise ApprovalAlreadyDecided(approval.status.value)

        _validate_actor_strength(actor, policy)
        _validate_permission(actor, decision)
        _validate_authority(repos, actor, approval, decision)
        _validate_separation_of_duties(repos, actor, approval, policy)

        # Phase 7: Check if the actor already voted.
        if repos.approval_votes.has_voted(scope=scope, approval_id=approval_id, actor_id=actor.user_id):
            raise AlreadyVoted(f"actor {actor.user_id} already voted on approval {approval_id}")

        # Phase 7: Cast the vote (append-only).
        vote_value = {"approved": "approve", "rejected": "reject", "held": "hold"}[decision]
        vote_id = repos.approval_votes.cast_vote(
            scope=scope,
            approval_id=approval_id,
            actor_id=actor.user_id,
            vote=vote_value,
            reason=reason,
            policy_version=policy.version,
            actor_role_snapshot=list(actor.roles),
        )

        # For 'rejected' and 'held', transition immediately — no quorum needed.
        if decision in ("rejected", "held"):
            decided = repos.approvals.decide(
                scope=scope, approval_id=approval_id, status=decision,
                decided_by=actor.display_name, state_fingerprint=None,
            )
            if decided is None:
                raise ApprovalAlreadyDecided("pending")

            project_id = UUID(decided.project_id) if decided.project_id else None
            decision_id = repos.approval_decisions.record(
                scope=scope, approval_id=approval_id, decision=decision,
                actor_id=actor.user_id, actor_role_snapshot=list(actor.roles),
                project_id=project_id, policy_version=policy.version, reason=reason,
            )
            repos.audit.append(
                scope=Scope(actor.organization_id, project_id),
                event_type="APPROVAL_DECIDED",
                actor=str(actor.user_id), object_type="approval", object_id=approval_id,
                payload={"decision": decision, "actor": actor.display_name,
                         "subject_id": decided.subject_id, "policy_version": policy.version,
                         "reason": reason, "decision_id": str(decision_id),
                         "vote_id": str(vote_id), "quorum_met": True},
            )
            return DecisionOutcome(approval_id, decision, actor.user_id, decision_id, policy.version, None, True)

        # For 'approved', check quorum.
        approve_count = repos.approval_votes.count_approve_votes(scope=scope, approval_id=approval_id)
        quorum_threshold = getattr(approval, "quorum_threshold", 1) or 1

        if approve_count < quorum_threshold:
            # Quorum not yet met — stay pending, but the vote IS recorded.
            # This is not an error — it's an expected state. Return a normal
            # outcome with quorum_met=False so the caller knows to wait.
            repos.audit.append(
                scope=Scope(actor.organization_id),
                event_type="APPROVAL_VOTE_CAST",
                actor=str(actor.user_id), object_type="approval", object_id=approval_id,
                payload={"vote": "approve", "actor": actor.display_name,
                         "policy_version": policy.version, "reason": reason,
                         "vote_id": str(vote_id), "approve_count": approve_count,
                         "quorum_threshold": quorum_threshold, "quorum_met": False},
            )
        else:
            # Quorum met — transition to approved.
            decided = repos.approvals.decide(
                scope=scope, approval_id=approval_id, status="approved",
                decided_by=actor.display_name, state_fingerprint=None,
            )
            if decided is None:
                raise ApprovalAlreadyDecided("pending")

            project_id = UUID(decided.project_id) if decided.project_id else None
            decision_id = repos.approval_decisions.record(
                scope=scope, approval_id=approval_id, decision="approved",
                actor_id=actor.user_id, actor_role_snapshot=list(actor.roles),
                project_id=project_id, policy_version=policy.version, reason=reason,
            )

            # Phase 4/5: Compute the state fingerprint INSIDE the transaction.
            state_fingerprint = _compute_state_fingerprint(repos, scope, decided)
            if state_fingerprint is None:
                raise FingerprintUnavailable(
                    "cannot approve without a state fingerprint — "
                    "the project state could not be reconstructed"
                )

            # Phase 6: Compute the decision fingerprint — the exact
            # InvoiceSnapshot||VerificationPacket||EvidenceSet||PolicyVersion||
            # ApprovalRequirements hash — atomically with the decision.
            from construction_ai.approvals.decision_fingerprint import (
                compute_decision_fingerprint_for_approval,
            )

            decision_fingerprint = compute_decision_fingerprint_for_approval(
                repos, scope=scope, approval=decided, policy=policy,
            )

            with repos.db.scoped(scope) as cur:
                cur.execute(
                    "UPDATE approvals SET state_fingerprint = %s, decision_fingerprint = %s "
                    "WHERE approval_id = %s AND organization_id = %s",
                    (state_fingerprint, decision_fingerprint, approval_id, scope.organization_id),
                )

            repos.audit.append(
                scope=Scope(actor.organization_id, project_id),
                event_type="APPROVAL_DECIDED",
                actor=str(actor.user_id), object_type="approval", object_id=approval_id,
                payload={"decision": "approved", "actor": actor.display_name,
                         "subject_id": decided.subject_id, "policy_version": policy.version,
                         "reason": reason, "decision_id": str(decision_id),
                         "vote_id": str(vote_id), "state_fingerprint": state_fingerprint,
                         "decision_fingerprint": decision_fingerprint,
                         "approve_count": approve_count, "quorum_threshold": quorum_threshold,
                         "quorum_met": True},
            )
            return DecisionOutcome(approval_id, "approved", actor.user_id, decision_id, policy.version, state_fingerprint, True)

    # Quorum not met — return normally (not an exception).
    return DecisionOutcome(approval_id, "approved", actor.user_id, vote_id, policy.version, None, False)


def record_state_fingerprint(
    repos: Repositories,
    *,
    actor: AuthenticatedActor,
    approval_id: UUID,
) -> str | None:
    """Legacy: Record the project state fingerprint after an approval decision.

    v0.5.0-rc3: This function is retained for backward compatibility but is
    now a no-op — the fingerprint is computed atomically inside decide_approval().
    Calls to this function will simply reload the existing fingerprint.
    """
    scope = Scope(actor.organization_id)
    approval = repos.approvals.get(scope=scope, approval_id=approval_id)
    if approval is None:
        return None
    return approval.state_fingerprint


def _validate_actor_strength(actor: AuthenticatedActor, policy: ApprovalPolicy) -> None:
    required = policy.required_authentication_strength
    strength_order = {"dev": 0, "oidc": 1, "oidc_mfa": 2}
    if strength_order.get(actor.authentication_strength, -1) < strength_order.get(required, 0):
        raise AuthorizationError(
            f"authentication strength {actor.authentication_strength!r} below required {required!r}"
        )


def _validate_permission(actor: AuthenticatedActor, decision: str) -> None:
    permission = _DECISION_PERMISSION[decision]
    if not can(actor, permission):
        raise AuthorizationError(f"actor lacks permission {permission!r}")


def _validate_authority(repos: Repositories, actor: AuthenticatedActor, approval, decision: str) -> None:
    if decision != "approved":
        # Reject and hold are not amount-bound; only invoice.approve carries
        # financial authority. They still require their permission (checked above).
        return
    currency = approval.currency or "CAD"
    project_id = UUID(approval.project_id) if approval.project_id else None
    matching = repos.authorities.matching(
        scope=Scope(actor.organization_id),
        user_id=actor.user_id,
        permission="invoice.approve",
        currency=currency,
        project_id=project_id,
    )
    result = authority_for(
        actor,
        permission="invoice.approve",
        amount=approval.amount,
        currency=currency,
        project_id=project_id,
        matching_authorities=matching,
    )
    if not result.allowed:
        raise AuthorizationError(result.reason)


def _validate_separation_of_duties(repos: Repositories, actor: AuthenticatedActor, approval, policy: ApprovalPolicy) -> None:
    previous = repos.approval_decisions.latest_for(scope=Scope(actor.organization_id), approval_id=UUID(approval.approval_id))
    previous_approvers: tuple[UUID, ...] = ()
    if previous:
        previous_approvers = (previous["actor_id"],)
    require_distinct = policy.requires_second_distinct_approver(approval.amount, approval.currency or "CAD")
    result = separation_of_duties_holds(
        actor=actor,
        requested_by=approval.requested_by,
        previous_approvers=previous_approvers,
        require_distinct_users=require_distinct,
    )
    if not result.allowed:
        raise AuthorizationError(result.reason)


def _compute_state_fingerprint(repos: Repositories, scope: Scope, approval) -> str | None:
    """Compute the project state fingerprint at decision time.

    v0.5.0-rc3 (Phase 5): This is now called INSIDE the approval transaction.
    The fingerprint binds the decision to the exact state that was approved.

    Returns None if the project cannot be reconstructed (e.g. project_id is
    NULL). For 'approved' decisions, a None fingerprint causes the approval
    to be refused (fail closed, Phase 4).
    """
    if not approval.project_id:
        return None
    try:
        from construction_ai.reconstruction.service import ProjectReconstructor

        recon = ProjectReconstructor.from_repositories(repos)
        project_scope = scope.for_project(UUID(approval.project_id))
        state = recon.project(scope=project_scope)
        return state.fingerprint()
    except Exception:
        return None
