"""The authoritative approval decision path.

One PostgreSQL transaction, one outcome:

    BEGIN
      SELECT approval FOR UPDATE
      validate current state (must be pending)
      validate actor (active, authenticated)
      validate permission (approve | reject | hold)
      validate approval authority (amount, currency, project, valid period)
      validate separation of duties (creator != approver; dual approval if required)
      UPDATE approval
      INSERT approval_decision
      append audit event
    COMMIT

If any step fails the whole transaction rolls back. Invariant: a committed
approval decision always has a matching audit event and a matching
approval_decisions row. The caller never supplies an identity; the
`AuthenticatedActor` is server-derived from the session.

This replaces the v0.3 path that took `user_id` from the request body and ran the
approval UPDATE and the audit append in two separate transactions.
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


@dataclass(frozen=True)
class DecisionOutcome:
    approval_id: UUID
    decision: str
    actor_id: UUID
    decision_id: UUID
    policy_version: str


def decide_approval(
    repos: Repositories,
    *,
    actor: AuthenticatedActor,
    approval_id: UUID,
    decision: str,
    reason: str = "",
    policy: ApprovalPolicy = DEFAULT_POLICY,
) -> DecisionOutcome:
    """The only authoritative way to decide an approval."""
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

        # Mutate. `decided_by` is a denormalized human-readable snapshot; the
        # authoritative actor lives on the approval_decisions row (actor_id FK).
        decided = repos.approvals.decide(
            scope=scope, approval_id=approval_id, status=decision, decided_by=actor.display_name
        )
        if decided is None:
            # Lost the race between get_for_update and the conditional UPDATE;
            # the row is no longer pending. Safe to report as already decided.
            raise ApprovalAlreadyDecided("pending")

        project_id = UUID(decided.project_id) if decided.project_id else None
        previous = repos.approval_decisions.latest_for(scope=scope, approval_id=approval_id)
        decision_id = repos.approval_decisions.record(
            scope=scope,
            approval_id=approval_id,
            decision=decision,
            actor_id=actor.user_id,
            actor_role_snapshot=list(actor.roles),
            project_id=project_id,
            policy_version=policy.version,
            reason=reason,
            previous_decision_id=previous["decision_id"] if previous else None,
        )
        repos.audit.append(
            scope=Scope(actor.organization_id, project_id),
            event_type="APPROVAL_DECIDED",
            actor=str(actor.user_id),
            object_type="approval",
            object_id=approval_id,
            payload={
                "decision": decision,
                "actor": actor.display_name,
                "subject_id": decided.subject_id,
                "policy_version": policy.version,
                "reason": reason,
                "decision_id": str(decision_id),
            },
        )
    return DecisionOutcome(approval_id, decision, actor.user_id, decision_id, policy.version)


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
