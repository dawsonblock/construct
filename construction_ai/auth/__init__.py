"""Authenticated user identity and authorization.

The v0.3 API key established the *organization*, never the *human*. This package
establishes the human: a server-derived `AuthenticatedActor` that every
financial mutation consumes. Identity comes from an identity provider (OIDC in
production, a local provider for development and tests), never from a request
field.

Authority flow:

    provider_subject -> identity -> user -> organization -> AuthenticatedActor
                                                                  |
                                            authorization.can(actor, permission)
                                            authorization.authority_for(actor, ...)
"""
from __future__ import annotations

from construction_ai.auth.authorization import AuthorizationError, AuthorizationResult, can, authority_for
from construction_ai.auth.identity import (
    DevIdentityProvider,
    IdentityProvider,
    IdentityProviderError,
)
from construction_ai.auth.models import AuthenticatedActor
from construction_ai.auth.permissions import ALL_PERMISSIONS, Permission

__all__ = [
    "ALL_PERMISSIONS",
    "AuthenticatedActor",
    "AuthorizationError",
    "AuthorizationResult",
    "DevIdentityProvider",
    "IdentityProvider",
    "IdentityProviderError",
    "Permission",
    "authority_for",
    "can",
]
