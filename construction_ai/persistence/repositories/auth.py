"""Repositories for the authority model (migration 007).

Identity is established here, the same way tenant identity is established in
`organizations.py`: by presenting a credential, never by stating an id. The
identity_bindings lookup uses `unscoped_auth` because it runs *before* a scope
exists — exactly like the organization API key lookup. Everything else is scoped.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from construction_ai.persistence.db import Database, Scope, row_to_dict, rows_to_dicts

SESSION_TOKEN_PREFIX = "csess."
# Sessions stay under RLS. The token carries the org so the resolver can set the
# scope before querying; a forged org with another tenant's session id sees nothing.
DEFAULT_SESSION_TTL = timedelta(hours=12)


@dataclass(frozen=True)
class User:
    user_id: UUID
    organization_id: UUID
    display_name: str
    email: str | None
    status: str


@dataclass(frozen=True)
class ResolvedIdentity:
    """A provider subject resolved to a user in an organization."""

    user_id: UUID
    organization_id: UUID
    display_name: str
    status: str


@dataclass(frozen=True)
class Session:
    session_id: UUID
    organization_id: UUID
    user_id: UUID
    provider: str
    authentication_strength: str
    expires_at: datetime
    revoked_at: datetime | None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UserRepository:
    """Users, identity bindings, roles, and the permissions a user has."""

    def __init__(self, db: Database):
        self.db = db

    # -- identity (pre-scope) ------------------------------------------------

    def resolve_identity(self, *, provider: str, provider_subject: str) -> ResolvedIdentity | None:
        """The authoritative provider_subject -> user -> organization lookup.

        Two steps because the tables live on opposite sides of the RLS wall:
        identity_bindings is not under RLS (it is consulted before a scope
        exists, like organization_api_keys), but users is. So read the binding
        unscoped, then read the user under the scope the binding just gave us.
        """
        with self.db.unscoped_auth() as cur:
            cur.execute(
                "SELECT user_id, organization_id FROM identity_bindings WHERE provider = %s AND provider_subject = %s",
                (provider, provider_subject),
            )
            row = row_to_dict(cur)
        if not row:
            return None
        org_id, user_id = row["organization_id"], row["user_id"]
        scope = Scope(org_id)
        with self.db.scoped(scope) as cur:
            cur.execute(
                "SELECT display_name, status FROM users WHERE organization_id = %s AND user_id = %s",
                (org_id, user_id),
            )
            user = row_to_dict(cur)
        if not user:
            # A binding without a user should not happen (FK), but fail closed.
            return None
        return ResolvedIdentity(user_id, org_id, user["display_name"], user["status"])

    # -- users (scoped) ------------------------------------------------------

    def create(self, *, scope: Scope, display_name: str, email: str | None = None) -> User:
        with self.db.scoped(scope) as cur:
            cur.execute(
                """INSERT INTO users(organization_id, display_name, email)
                   VALUES(%s, %s, %s) RETURNING user_id, organization_id, display_name, email, status""",
                (scope.organization_id, display_name, email),
            )
            row = row_to_dict(cur)
        return User(**row)

    def get(self, *, scope: Scope, user_id: UUID) -> User | None:
        with self.db.scoped(scope) as cur:
            cur.execute(
                """SELECT user_id, organization_id, display_name, email, status
                   FROM users WHERE organization_id = %s AND user_id = %s""",
                (scope.organization_id, user_id),
            )
            row = row_to_dict(cur)
        return User(**row) if row else None

    def disable(self, *, scope: Scope, user_id: UUID) -> bool:
        with self.db.scoped(scope) as cur:
            cur.execute(
                """UPDATE users SET status = 'disabled', disabled_at = now()
                   WHERE organization_id = %s AND user_id = %s AND status = 'active'
                   RETURNING user_id""",
                (scope.organization_id, user_id),
            )
            return cur.fetchone() is not None

    def bind_identity(self, *, scope: Scope, user_id: UUID, provider: str, provider_subject: str) -> None:
        with self.db.scoped(scope) as cur:
            cur.execute(
                """INSERT INTO identity_bindings(organization_id, user_id, provider, provider_subject)
                   VALUES(%s, %s, %s, %s)""",
                (scope.organization_id, user_id, provider, provider_subject),
            )

    # -- roles and permissions (scoped) --------------------------------------

    def create_role(self, *, scope: Scope, name: str) -> UUID:
        with self.db.scoped(scope) as cur:
            cur.execute(
                """INSERT INTO roles(organization_id, name)
                   VALUES(%s, %s) ON CONFLICT (organization_id, name) DO UPDATE SET name = EXCLUDED.name
                   RETURNING role_id""",
                (scope.organization_id, name),
            )
            return cur.fetchone()[0]

    def grant_role_permission(self, *, scope: Scope, role_id: UUID, permission: str) -> None:
        with self.db.scoped(scope) as cur:
            cur.execute(
                """INSERT INTO role_permissions(organization_id, role_id, permission)
                   VALUES(%s, %s, %s) ON CONFLICT DO NOTHING""",
                (scope.organization_id, role_id, permission),
            )

    def assign_role(self, *, scope: Scope, user_id: UUID, role_id: UUID) -> None:
        with self.db.scoped(scope) as cur:
            cur.execute(
                """INSERT INTO user_roles(organization_id, user_id, role_id)
                   VALUES(%s, %s, %s) ON CONFLICT DO NOTHING""",
                (scope.organization_id, user_id, role_id),
            )

    def permissions_for(self, *, scope: Scope, user_id: UUID) -> set[str]:
        with self.db.scoped(scope) as cur:
            cur.execute(
                """SELECT DISTINCT rp.permission FROM user_roles ur
                   JOIN role_permissions rp
                     ON rp.organization_id = ur.organization_id AND rp.role_id = ur.role_id
                   WHERE ur.organization_id = %s AND ur.user_id = %s""",
                (scope.organization_id, user_id),
            )
            return {r[0] for r in cur.fetchall()}

    def role_names_for(self, *, scope: Scope, user_id: UUID) -> list[str]:
        with self.db.scoped(scope) as cur:
            cur.execute(
                """SELECT r.name FROM user_roles ur JOIN roles r
                     ON r.organization_id = ur.organization_id AND r.role_id = ur.role_id
                   WHERE ur.organization_id = %s AND ur.user_id = %s ORDER BY r.name""",
                (scope.organization_id, user_id),
            )
            return [r[0] for r in cur.fetchall()]


class ApprovalAuthorityRepository:
    """Amount / currency / project limits. NULL maximum_amount means unlimited."""

    def __init__(self, db: Database):
        self.db = db

    def grant(
        self,
        *,
        scope: Scope,
        user_id: UUID,
        permission: str,
        currency: str = "CAD",
        minimum_amount: float = 0,
        maximum_amount: float | None = None,
        project_id: UUID | None = None,
        valid_until: datetime | None = None,
    ) -> UUID:
        from decimal import Decimal

        with self.db.scoped(scope) as cur:
            cur.execute(
                """INSERT INTO approval_authorities(
                       organization_id, user_id, project_id, currency, minimum_amount,
                       maximum_amount, permission, valid_until)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING authority_id""",
                (
                    scope.organization_id, user_id, project_id, currency,
                    Decimal(str(minimum_amount)),
                    Decimal(str(maximum_amount)) if maximum_amount is not None else None,
                    permission, valid_until,
                ),
            )
            return cur.fetchone()[0]

    def matching(
        self,
        *,
        scope: Scope,
        user_id: UUID,
        permission: str,
        currency: str,
        project_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        """Authorities that could govern this action: org-wide plus project-specific."""
        with self.db.scoped(scope) as cur:
            cur.execute(
                """SELECT minimum_amount, maximum_amount, currency, project_id, valid_from, valid_until
                   FROM approval_authorities
                   WHERE organization_id = %s AND user_id = %s AND permission = %s
                     AND currency = %s
                     AND (project_id IS NULL OR project_id = %s)
                     AND (valid_until IS NULL OR valid_until > now())
                   ORDER BY project_id NULLS FIRST""",
                (scope.organization_id, user_id, permission, currency, project_id),
            )
            return [
                {
                    "minimum_amount": float(r[0]) if r[0] is not None else None,
                    "maximum_amount": float(r[1]) if r[1] is not None else None,
                    "currency": r[2],
                    "project_id": r[3],
                    "valid_from": r[4],
                    "valid_until": r[5],
                }
                for r in cur.fetchall()
            ]


class SessionRepository:
    """Server-side sessions. Creation and revocation are scoped; resolution is by
    token, which carries the org so the lookup runs under RLS."""

    def __init__(self, db: Database):
        self.db = db

    def create(
        self,
        *,
        scope: Scope,
        user_id: UUID,
        provider: str,
        authentication_strength: str = "oidc",
        ttl: timedelta = DEFAULT_SESSION_TTL,
    ) -> str:
        with self.db.scoped(scope) as cur:
            cur.execute(
                """INSERT INTO sessions(organization_id, user_id, provider, authentication_strength, expires_at)
                   VALUES(%s, %s, %s, %s, %s) RETURNING session_id""",
                (scope.organization_id, user_id, provider, authentication_strength, _utcnow() + ttl),
            )
            session_id = cur.fetchone()[0]
        return f"{SESSION_TOKEN_PREFIX}{scope.organization_id}.{session_id}"

    def resolve(self, token: str) -> Session | None:
        """Bearer -> Session, or None. Enforces expiry and revocation at the query."""
        if not token.startswith(SESSION_TOKEN_PREFIX):
            return None
        body = token[len(SESSION_TOKEN_PREFIX):]
        org_str, _, session_str = body.partition(".")
        try:
            org_id = UUID(org_str)
            session_id = UUID(session_str)
        except ValueError:
            return None
        scope = Scope(org_id)
        with self.db.scoped(scope) as cur:
            cur.execute(
                """SELECT session_id, organization_id, user_id, provider, authentication_strength, expires_at, revoked_at
                   FROM sessions
                   WHERE organization_id = %s AND session_id = %s
                     AND revoked_at IS NULL AND expires_at > now()""",
                (org_id, session_id),
            )
            row = row_to_dict(cur)
        return Session(**row) if row else None

    def revoke(self, *, scope: Scope, session_id: UUID) -> bool:
        with self.db.scoped(scope) as cur:
            cur.execute(
                """UPDATE sessions SET revoked_at = now()
                   WHERE organization_id = %s AND session_id = %s AND revoked_at IS NULL
                   RETURNING session_id""",
                (scope.organization_id, session_id),
            )
            return cur.fetchone() is not None


class ApprovalDecisionRepository:
    """Append-only history. Insert only — there is no update or delete method."""

    def __init__(self, db: Database):
        self.db = db

    def record(
        self,
        *,
        scope: Scope,
        approval_id: UUID,
        decision: str,
        actor_id: UUID,
        actor_role_snapshot: list[str],
        project_id: UUID | None = None,
        invoice_id: UUID | None = None,
        policy_version: str | None = None,
        verification_packet_hash: str | None = None,
        reason: str = "",
        previous_decision_id: UUID | None = None,
    ) -> UUID:
        from psycopg.types.json import Jsonb

        with self.db.scoped(scope) as cur:
            cur.execute(
                """INSERT INTO approval_decisions(
                       organization_id, approval_id, project_id, invoice_id, decision, actor_id,
                       actor_role_snapshot, policy_version, verification_packet_hash, reason,
                       previous_decision_id)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING decision_id""",
                (
                    scope.organization_id, approval_id, project_id, invoice_id, decision, actor_id,
                    Jsonb(actor_role_snapshot), policy_version, verification_packet_hash, reason,
                    previous_decision_id,
                ),
            )
            return cur.fetchone()[0]

    def latest_for(self, *, scope: Scope, approval_id: UUID) -> dict[str, Any] | None:
        with self.db.scoped(scope) as cur:
            cur.execute(
                """SELECT decision_id, approval_id, decision, actor_id, actor_role_snapshot,
                          policy_version, verification_packet_hash, reason, previous_decision_id, created_at
                   FROM approval_decisions
                   WHERE organization_id = %s AND approval_id = %s
                   ORDER BY created_at DESC LIMIT 1""",
                (scope.organization_id, approval_id),
            )
            return row_to_dict(cur)


class ApprovalVoteRepository:
    """Append-only quorum votes. Insert only — no update or delete.

    v0.5.0-rc3 (Phase 7): Real multi-approver quorum. Each approver casts a
    vote (approve/reject/hold). The approval only transitions to 'approved'
    when the quorum_threshold is met by distinct approve votes.
    """

    def __init__(self, db: Database):
        self.db = db

    def cast_vote(
        self,
        *,
        scope: Scope,
        approval_id: UUID,
        actor_id: UUID,
        vote: str,
        reason: str = "",
        policy_version: str | None = None,
        actor_role_snapshot: list[str] | None = None,
    ) -> UUID:
        """Cast a vote. Fails if the actor already voted (unique constraint)."""
        from psycopg.types.json import Jsonb

        with self.db.scoped(scope) as cur:
            cur.execute(
                """INSERT INTO approval_votes(
                       organization_id, approval_id, actor_id, vote, reason,
                       policy_version, actor_role_snapshot)
                   VALUES(%s, %s, %s, %s, %s, %s, %s)
                   RETURNING vote_id""",
                (
                    scope.organization_id, approval_id, actor_id, vote, reason,
                    policy_version, Jsonb(actor_role_snapshot or []),
                ),
            )
            return cur.fetchone()[0]

    def count_approve_votes(self, *, scope: Scope, approval_id: UUID) -> int:
        """Count distinct approve votes for an approval."""
        with self.db.scoped(scope) as cur:
            cur.execute(
                """SELECT COUNT(*) FROM approval_votes
                   WHERE organization_id = %s AND approval_id = %s AND vote = 'approve'""",
                (scope.organization_id, approval_id),
            )
            return cur.fetchone()[0]

    def list_votes(self, *, scope: Scope, approval_id: UUID) -> list[dict[str, Any]]:
        """List all votes for an approval."""
        with self.db.scoped(scope) as cur:
            cur.execute(
                """SELECT vote_id, actor_id, vote, reason, policy_version,
                          actor_role_snapshot, created_at
                   FROM approval_votes
                   WHERE organization_id = %s AND approval_id = %s
                   ORDER BY created_at""",
                (scope.organization_id, approval_id),
            )
            return rows_to_dicts(cur)

    def has_voted(self, *, scope: Scope, approval_id: UUID, actor_id: UUID) -> bool:
        """Check if an actor has already voted on this approval."""
        with self.db.scoped(scope) as cur:
            cur.execute(
                """SELECT 1 FROM approval_votes
                   WHERE organization_id = %s AND approval_id = %s AND actor_id = %s""",
                (scope.organization_id, approval_id, actor_id),
            )
            return cur.fetchone() is not None
