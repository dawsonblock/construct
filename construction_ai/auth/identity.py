"""Identity providers.

Production uses OIDC/OAuth2: an external provider vouches for a subject, and the
server resolves `provider_subject -> user -> organization`. The provider never
supplies a user_id; it supplies a verified subject, and the server decides who
that is.

`DevIdentityProvider` is the offline provider for development and the test
suite: it trusts a caller-supplied subject only inside the bootstrap/seeding
path, which is the same trust boundary as `scripts/seed_demo.py`. It is never
enabled in production (see CONSTRUCT_AUTH_PROVIDER).
"""
from __future__ import annotations

from dataclasses import dataclass


class IdentityProviderError(Exception):
    """A credential was rejected or the provider could not vouch for the subject."""


@dataclass(frozen=True)
class VerifiedSubject:
    """What an identity provider hands back: who, vouched by whom, how strongly."""

    provider: str
    provider_subject: str
    authentication_strength: str


class IdentityProvider:
    """Exchange a credential for a verified subject. Never returns a user_id."""

    name: str = ""

    def verify(self, credential: str) -> VerifiedSubject:  # pragma: no cover - interface
        raise NotImplementedError


class DevIdentityProvider(IdentityProvider):
    """Offline provider. The 'credential' is the provider_subject itself.

    Only for development and tests. Configure with CONSTRUCT_AUTH_PROVIDER=dev.
    A real deployment sets CONSTRUCT_AUTH_PROVIDER=oidc and configures an issuer.
    """

    name = "dev"

    def verify(self, credential: str) -> VerifiedSubject:
        if not credential:
            raise IdentityProviderError("a dev identity requires a subject string")
        # The dev provider's authentication strength is weak; policy can require
        # stronger for high-value approvals and these will fail closed.
        return VerifiedSubject(provider=self.name, provider_subject=credential, authentication_strength="dev")
