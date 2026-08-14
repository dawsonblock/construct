"""Provisioning helpers for the authority model.

Used by the test suite, seed scripts, and the organization-admin flow (v0.4.3).
Creates a user, binds a dev identity, assigns a role with permissions, and
optionally grants an approval authority. Each piece is also reachable directly
through `repos.users` / `repos.authorities`.
"""
from __future__ import annotations

from uuid import UUID

from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories import Repositories


def provision_approver(
    repos: Repositories,
    scope: Scope,
    *,
    subject: str,
    display_name: str,
    permissions: list[str],
    role: str = "approver",
    currency: str = "CAD",
    maximum_amount: float | None = None,
    project_id: UUID | None = None,
    provider: str = "dev",
) -> UUID:
    """Create a user with a role, permissions, and an approval authority. Returns user_id."""
    user = repos.users.create(scope=scope, display_name=display_name, email=subject if "@" in subject else None)
    repos.users.bind_identity(scope=scope, user_id=user.user_id, provider=provider, provider_subject=subject)
    role_id = repos.users.create_role(scope=scope, name=role)
    for permission in permissions:
        repos.users.grant_role_permission(scope=scope, role_id=role_id, permission=permission)
    repos.users.assign_role(scope=scope, user_id=user.user_id, role_id=role_id)
    if "invoice.approve" in permissions:
        repos.authorities.grant(
            scope=scope,
            user_id=user.user_id,
            permission="invoice.approve",
            currency=currency,
            maximum_amount=maximum_amount,
            project_id=project_id,
        )
    return user.user_id
