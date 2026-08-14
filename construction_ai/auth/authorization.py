"""Authorization: 'is this actor allowed to do this?'

Authentication only answers *who*. This module answers *may they*. The default
is DENY: an action is allowed only if a policy explicitly allows it.

Approval authority is the conjunction:

    Authorized(actor, invoice) =
        can(actor, approve)
        AND amount <= limit(actor, currency, project)
        AND currency allowed
        AND project allowed (or authority is org-wide)
        AND authority is within its valid period
        AND separation-of-duties holds (creator != approver)

Separation of duties is configurable rather than hardcoded; the default policy is
`creator != approver` for every approval, plus a second distinct approver above
a configurable threshold.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from construction_ai.auth.models import AuthenticatedActor


class AuthorizationError(Exception):
    """The actor is not allowed to perform this action. Fail closed."""


@dataclass(frozen=True)
class AuthorizationResult:
    allowed: bool
    reason: str = ""

    @classmethod
    def deny(cls, reason: str) -> "AuthorizationResult":
        return cls(allowed=False, reason=reason)

    @classmethod
    def allow(cls) -> "AuthorizationResult":
        return cls(allowed=True)


def can(actor: AuthenticatedActor, permission: str) -> bool:
    """Permission check. DENY unless the permission is in the actor's set."""
    return permission in actor.permissions


def authority_for(
    actor: AuthenticatedActor,
    *,
    permission: str,
    amount: Decimal | float | int | None,
    currency: str,
    project_id: UUID | None,
    matching_authorities: list[dict[str, Any]],
) -> AuthorizationResult:
    """Decide whether `actor` may exercise `permission` over a financial amount.

    `matching_authorities` is the pre-filtered set of approval_authorities rows
    for this actor/permission/currency/(project or org-wide), already
    validity-window-filtered by the repository. This function applies the amount
    bound — the part that must live in policy, not SQL, so it is testable and
    auditable in one place.
    """
    if not can(actor, permission):
        return AuthorizationResult.deny(f"actor lacks permission {permission!r}")

    if not matching_authorities:
        return AuthorizationResult.deny(
            f"no approval authority grants {permission!r} to this actor in {currency}"
        )

    if amount is None:
        # An approval with no amount cannot be bound-checked; require an explicit
        # unlimited authority rather than silently passing.
        has_unlimited = any(a["maximum_amount"] is None for a in matching_authorities)
        if has_unlimited:
            return AuthorizationResult.allow()
        return AuthorizationResult.deny("amount unknown and no unlimited authority applies")

    value = Decimal(str(amount))
    # A project-specific authority takes precedence over an org-wide one when both
    # match; the repository orders project_id NULLS FIRST, so the tightest bound is
    # checked first. The first authority that covers the amount authorizes.
    for authority in matching_authorities:
        minimum = Decimal(str(authority["minimum_amount"] or 0))
        maximum = authority["maximum_amount"]
        if value < minimum:
            continue
        if maximum is not None and value > Decimal(str(maximum)):
            continue
        return AuthorizationResult.allow()
    return AuthorizationResult.deny(
        f"amount {value} {currency} is outside every approval authority for this actor"
    )


def separation_of_duties_holds(
    *,
    actor: AuthenticatedActor,
    requested_by: str | None,
    previous_approvers: tuple[UUID, ...] = (),
    require_distinct_users: bool = False,
) -> AuthorizationResult:
    """Creator != Approver, and (optionally) Approver_1 != Approver_2.

    `requested_by` is the approval's originator (the `approvals.requested_by`
    string: 'ai' for system-originated, or a user_id string). `previous_approvers`
    are the user_ids of prior approval decisions on this subject, for the
    multi-approver case.
    """
    actor_id_str = str(actor.user_id)
    # System-originated approvals ('ai') have no human creator, so the creator
    # check is vacuously satisfied — a human is never 'ai'.
    if requested_by and requested_by not in {"ai", "system"} and requested_by == actor_id_str:
        return AuthorizationResult.deny("approver cannot approve their own request (creator != approver)")
    if require_distinct_users and actor.user_id in previous_approvers:
        return AuthorizationResult.deny("this approval requires a second distinct approver")
    return AuthorizationResult.allow()
