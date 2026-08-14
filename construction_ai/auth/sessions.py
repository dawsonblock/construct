"""Server-side session establishment and resolution.

Two entry points, both server-derived:

- `login(repos, provider, credential)` — an identity provider verifies a subject;
  the server resolves it to a user, creates a session, and returns a session
  token. The caller supplies a *credential*, never a user_id.
- `actor_from_session(repos, token)` — a bearer session token resolves to an
  `AuthenticatedActor`. This is the dependency every financial mutation uses.

A disabled user can never become an actor: `resolve_identity` returns their
status, and we fail closed unless it is 'active'.
"""
from __future__ import annotations

from construction_ai.auth.identity import DevIdentityProvider, IdentityProvider, IdentityProviderError
from construction_ai.auth.models import AuthenticatedActor
from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories import Repositories


class AuthenticationError(Exception):
    """A credential or session was rejected."""


def login(
    repos: Repositories,
    *,
    provider: IdentityProvider,
    credential: str,
) -> str:
    """Verify a credential, resolve the subject to a user, issue a session token."""
    verified = provider.verify(credential)
    identity = repos.users.resolve_identity(provider=verified.provider, provider_subject=verified.provider_subject)
    if identity is None:
        raise AuthenticationError(f"no user bound to {verified.provider}:{verified.provider_subject!r}")
    if identity.status != "active":
        raise AuthenticationError("user is not active")
    scope = Scope(identity.organization_id)
    return repos.sessions.create(
        scope=scope,
        user_id=identity.user_id,
        provider=verified.provider,
        authentication_strength=verified.authentication_strength,
    )


def actor_from_session(repos: Repositories, token: str) -> AuthenticatedActor:
    """Bearer session token -> AuthenticatedActor. Raises AuthenticationError."""
    if not token:
        raise AuthenticationError("a session bearer token is required")
    session = repos.sessions.resolve(token)
    if session is None:
        raise AuthenticationError("session is invalid, expired, or revoked")
    scope = Scope(session.organization_id)
    user = repos.users.get(scope=scope, user_id=session.user_id)
    if user is None or user.status != "active":
        # The user was disabled after the session was issued. Revoke and reject.
        repos.sessions.revoke(scope=scope, session_id=session.session_id)
        raise AuthenticationError("user is not active")
    permissions = repos.users.permissions_for(scope=scope, user_id=session.user_id)
    roles = tuple(repos.users.role_names_for(scope=scope, user_id=session.user_id))
    return AuthenticatedActor(
        user_id=session.user_id,
        organization_id=session.organization_id,
        display_name=user.display_name,
        roles=roles,
        permissions=frozenset(permissions),
        authentication_strength=session.authentication_strength,
    )


def provider_from_env() -> IdentityProvider:
    """Select the identity provider from CONSTRUCT_AUTH_PROVIDER (default: dev)."""
    import os

    name = os.getenv("CONSTRUCT_AUTH_PROVIDER", "dev").lower()
    if name == "dev":
        return DevIdentityProvider()
    # OIDC wiring lands with the production auth deployment; until then only the
    # dev provider is available, and asking for oidc without configuration fails
    # closed rather than silently falling back.
    if name == "oidc":
        raise IdentityProviderError(
            "OIDC provider is not yet configured; set CONSTRUCT_AUTH_PROVIDER=dev for local development"
        )
    raise IdentityProviderError(f"unknown CONSTRUCT_AUTH_PROVIDER={name!r}")
