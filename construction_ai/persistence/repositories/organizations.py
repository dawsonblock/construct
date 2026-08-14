"""Organizations and the bearer-token → organization lookup.

This is the only place tenant identity is *established*. Everywhere else it is
carried in a `Scope`. Callers never state which organization they are; they
present a credential and the server decides.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from construction_ai.persistence.db import Database, row_to_dict

API_KEY_PREFIX = "cai_"


def hash_api_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


@dataclass(frozen=True)
class Organization:
    organization_id: UUID
    slug: str
    name: str


class OrganizationRepository:
    def __init__(self, db: Database):
        self.db = db

    def create(self, *, slug: str, name: str) -> Organization:
        with self.db.unscoped_auth() as cur:
            cur.execute(
                """INSERT INTO organizations(slug, name) VALUES(%s, %s)
                   ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
                   RETURNING organization_id, slug, name""",
                (slug, name),
            )
            row = row_to_dict(cur)
        return Organization(row["organization_id"], row["slug"], row["name"])

    def get_by_slug(self, slug: str) -> Organization | None:
        with self.db.unscoped_auth() as cur:
            cur.execute("SELECT organization_id, slug, name FROM organizations WHERE slug = %s", (slug,))
            row = row_to_dict(cur)
        return Organization(row["organization_id"], row["slug"], row["name"]) if row else None

    def issue_api_key(self, *, organization_id: UUID, label: str = "") -> str:
        """Returns the token once. Only its SHA-256 digest is stored."""
        token = generate_api_key()
        with self.db.unscoped_auth() as cur:
            cur.execute(
                "INSERT INTO organization_api_keys(organization_id, key_hash, label) VALUES(%s, %s, %s)",
                (organization_id, hash_api_key(token), label),
            )
        return token

    def authenticate(self, token: str) -> UUID | None:
        """Bearer token → organization id, or None. Never trusts caller-supplied ids."""
        if not token:
            return None
        with self.db.unscoped_auth() as cur:
            cur.execute(
                "SELECT organization_id FROM organization_api_keys WHERE key_hash = %s AND revoked_at IS NULL",
                (hash_api_key(token),),
            )
            row: Any = cur.fetchone()
        return row[0] if row else None

    def revoke_api_key(self, *, organization_id: UUID, key_hash: str) -> bool:
        with self.db.unscoped_auth() as cur:
            cur.execute(
                "UPDATE organization_api_keys SET revoked_at = now() WHERE organization_id = %s AND key_hash = %s RETURNING key_id",
                (organization_id, key_hash),
            )
            return cur.fetchone() is not None
