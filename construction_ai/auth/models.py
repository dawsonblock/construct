from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class AuthenticatedActor:
    """The server-derived identity that every financial mutation consumes.

    Built from a verified session, never from a request field. `permissions`
    is the closed set the actor has been granted — authorization is DENY by
    default, so an action is allowed only if its permission is in this set.
    """

    user_id: UUID
    organization_id: UUID
    display_name: str
    roles: tuple[str, ...]
    permissions: frozenset[str]
    authentication_strength: str

    @property
    def is_active(self) -> bool:
        return True  # disabled users cannot resolve to an actor; see sessions.py

    def has(self, permission: str) -> bool:
        return permission in self.permissions
