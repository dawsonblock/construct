"""External action ledger — prevents duplicate external effects on retry.

Every external side-effect (ERP writes, notifications, etc.) is recorded with
an idempotency key. Before performing an external action, check if an action
with the same (organization, action_type, idempotency_key) already exists.
If it does, return the existing result instead of performing the action again.

This is the enforcement layer for:
  RepeatedExecution ⇒ NoDuplicateFinancialEffect
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories.base import Repository


@dataclass(frozen=True)
class ExternalAction:
    action_id: UUID
    organization_id: UUID
    action_type: str
    target_system: str
    target_id: str | None
    idempotency_key: str
    request_hash: str | None
    result: dict[str, Any] | None
    status: str
    created_at: datetime


def hash_request(payload: dict[str, Any]) -> str:
    """Stable hash of the request payload for audit trail."""
    import json

    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


class ExternalActionRepository(Repository):
    table = "external_actions"
    id_column = "action_id"

    def record(
        self,
        *,
        scope: Scope,
        action_type: str,
        idempotency_key: str,
        target_system: str = "erpnext",
        target_id: str | None = None,
        request_payload: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        status: str = "completed",
    ) -> ExternalAction:
        """Record an external action. If an action with the same idempotency
        key already exists, return it instead of creating a duplicate."""
        from psycopg.types.json import Jsonb

        from construction_ai.persistence.serialization import dumps

        # Check for an existing action with the same idempotency key.
        existing = self.find_by_key(scope=scope, action_type=action_type, idempotency_key=idempotency_key)
        if existing is not None:
            return existing

        req_hash = hash_request(request_payload) if request_payload else None
        with self.db.scoped(scope) as cur:
            cur.execute(
                """INSERT INTO external_actions(organization_id, action_type, target_system,
                       target_id, idempotency_key, request_hash, result, status)
                   VALUES(%s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING action_id, organization_id, action_type, target_system,
                             target_id, idempotency_key, request_hash, result, status, created_at""",
                (
                    scope.organization_id,
                    action_type,
                    target_system,
                    target_id,
                    idempotency_key,
                    req_hash,
                    Jsonb(result, dumps=dumps) if result else None,
                    status,
                ),
            )
            row = cur.fetchone()
        return ExternalAction(
            action_id=row[0],
            organization_id=row[1],
            action_type=row[2],
            target_system=row[3],
            target_id=row[4],
            idempotency_key=row[5],
            request_hash=row[6],
            result=row[7] if row[7] else None,
            status=row[8],
            created_at=row[9],
        )

    def find_by_key(self, *, scope: Scope, action_type: str, idempotency_key: str) -> ExternalAction | None:
        """Find an existing action by idempotency key. Returns None if not found."""
        with self.db.scoped(scope) as cur:
            cur.execute(
                """SELECT action_id, organization_id, action_type, target_system,
                          target_id, idempotency_key, request_hash, result, status, created_at
                   FROM external_actions
                   WHERE organization_id = %s AND action_type = %s AND idempotency_key = %s""",
                (scope.organization_id, action_type, idempotency_key),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return ExternalAction(
            action_id=row[0],
            organization_id=row[1],
            action_type=row[2],
            target_system=row[3],
            target_id=row[4],
            idempotency_key=row[5],
            request_hash=row[6],
            result=row[7] if row[7] else None,
            status=row[8],
            created_at=row[9],
        )

    def list(self, *, scope: Scope, action_type: str | None = None, limit: int = 100) -> list[ExternalAction]:
        clause, params = self._tenant_clause(scope)
        sql = f"""SELECT action_id, organization_id, action_type, target_system,
                         target_id, idempotency_key, request_hash, result, status, created_at
                  FROM external_actions WHERE {clause}"""
        if action_type:
            sql += " AND action_type = %s"
            params.append(action_type)
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        rows = self._fetch_all(scope, sql, params)
        return [
            ExternalAction(
                action_id=r["action_id"],
                organization_id=r["organization_id"],
                action_type=r["action_type"],
                target_system=r["target_system"],
                target_id=r.get("target_id"),
                idempotency_key=r["idempotency_key"],
                request_hash=r.get("request_hash"),
                result=r.get("result"),
                status=r["status"],
                created_at=r["created_at"],
            )
            for r in rows
        ]
