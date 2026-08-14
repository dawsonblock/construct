"""External action state machine — prevents duplicate external effects on retry.

v0.5.0-rc2 (Phase 1): Rebuilt from the unsafe check-then-insert pattern to a
proper state machine with atomic reservation.

State machine:

    PENDING → EXECUTING → CONFIRMED
                         → UNKNOWN → (reconcile) → CONFIRMED or FAILED_RETRYABLE
                         → FAILED_RETRYABLE
                         → FAILED_TERMINAL

The row is reserved BEFORE the external call. The invariant:

    ExternalEffectAttempt ⇒ ExternalActionReservationExists

Acquisition is atomic:

    INSERT INTO external_actions (..., 'PENDING')
    ON CONFLICT (organization_id, operation, idempotency_key) DO NOTHING;

Then reload the row to see whether we won the reservation or someone else did.

The table allows UPDATE (for state transitions) but not DELETE. Every
transition should also be recorded in the append-only audit_events table.
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
    action_type: str  # legacy name; same as operation
    operation: str
    target_system: str
    target_id: str | None
    idempotency_key: str
    request_hash: str | None
    result: dict[str, Any] | None
    status: str
    created_at: datetime
    # New state-machine fields:
    subject_type: str | None = None
    subject_id: UUID | None = None
    remote_system: str | None = None
    remote_document_id: str | None = None
    attempt_count: int = 0
    reserved_at: datetime | None = None
    last_attempt_at: datetime | None = None
    confirmed_at: datetime | None = None
    last_error: str | None = None


def hash_request(payload: dict[str, Any]) -> str:
    """Stable hash of the request payload for audit trail."""
    import json

    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


class ExternalActionRepository(Repository):
    table = "external_actions"
    id_column = "action_id"
    # Not project-scoped — external actions are org-level.
    project_scoped = False

    # -- Atomic reservation (Phase 1) ----------------------------------------

    def reserve(
        self,
        *,
        scope: Scope,
        operation: str,
        idempotency_key: str,
        subject_type: str | None = None,
        subject_id: UUID | None = None,
        request_payload: dict[str, Any] | None = None,
        remote_system: str = "erpnext",
    ) -> ExternalAction:
        """Atomically reserve an external action row.

        Uses INSERT ... ON CONFLICT DO NOTHING, then reloads the row. This is
        race-safe: two concurrent reservations with the same idempotency key
        will both see the same single row.

        The row starts in PENDING status. The caller must then transition it
        through EXECUTING → CONFIRMED (or UNKNOWN / FAILED_*).
        """


        req_hash = hash_request(request_payload) if request_payload else None
        with self.db.scoped(scope) as cur:
            # Atomic insert-or-nothing.
            cur.execute(
                """INSERT INTO external_actions(
                       organization_id, action_type, operation, target_system,
                       idempotency_key, request_hash, status,
                       subject_type, subject_id, remote_system,
                       reserved_at
                   )
                   VALUES(%s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s, now())
                   ON CONFLICT (organization_id, action_type, idempotency_key) DO NOTHING
                   RETURNING action_id""",
                (
                    scope.organization_id,
                    operation,  # action_type (legacy column)
                    operation,  # operation (new column)
                    remote_system,
                    idempotency_key,
                    req_hash,
                    subject_type,
                    subject_id,
                    remote_system,
                ),
            )
            row = cur.fetchone()
            if row is not None:
                # We won the reservation — load the full row.
                action_id = row[0]
            else:
                # Someone else already has this reservation — load their row.
                cur.execute(
                    """SELECT action_id FROM external_actions
                       WHERE organization_id = %s AND action_type = %s AND idempotency_key = %s""",
                    (scope.organization_id, operation, idempotency_key),
                )
                existing = cur.fetchone()
                if existing is None:
                    # This should not happen — ON CONFLICT DO NOTHING means the
                    # row exists. But if the conflict was on a different unique
                    # constraint, we might get here. Treat as a race error.
                    raise RuntimeError(
                        f"reserve failed: could not find or create external action for key {idempotency_key!r}"
                    )
                action_id = existing[0]

        return self.get(scope=scope, action_id=action_id)

    def get(self, *, scope: Scope, action_id: UUID) -> ExternalAction | None:
        """Load an external action by action_id."""
        row = self.get_row(scope=scope, record_id=action_id)
        return _to_action(row) if row else None

    def get_by_key(self, *, scope: Scope, operation: str, idempotency_key: str) -> ExternalAction | None:
        """Load an external action by (operation, idempotency_key)."""
        with self.db.scoped(scope) as cur:
            cur.execute(
                self._select_sql() + " WHERE organization_id = %s AND action_type = %s AND idempotency_key = %s",
                (scope.organization_id, operation, idempotency_key),
            )
            row = cur.fetchone()
            if row is None:
                return None
            columns = [c.name for c in cur.description]
        return _to_action(dict(zip(columns, row, strict=True)))

    # -- State transitions (Phase 1) -----------------------------------------

    def transition(
        self,
        *,
        scope: Scope,
        action_id: UUID,
        from_status: str,
        to_status: str,
        remote_document_id: str | None = None,
        result: dict[str, Any] | None = None,
        last_error: str | None = None,
    ) -> ExternalAction | None:
        """Transition an external action to a new status.

        The transition is conditional on the current status matching
        `from_status`. This prevents race conditions: two workers cannot both
        transition the same action from EXECUTING to CONFIRMED.

        Returns the updated action, or None if the transition was not possible
        (the current status didn't match `from_status`).
        """
        from psycopg.types.json import Jsonb

        from construction_ai.persistence.serialization import dumps

        # Build the SET clause based on which fields are provided.
        sets = ["status = %s", "last_attempt_at = now()"]
        params: list[Any] = [to_status]

        if to_status == "executing":
            sets.append("attempt_count = attempt_count + 1")
        if to_status == "confirmed":
            sets.append("confirmed_at = now()")
        if remote_document_id is not None:
            sets.append("remote_document_id = %s")
            params.append(remote_document_id)
        if result is not None:
            sets.append("result = %s")
            params.append(Jsonb(result, dumps=dumps))
        if last_error is not None:
            sets.append("last_error = %s")
            params.append(last_error)

        clause, clause_params = self._tenant_clause(scope)
        params.extend(clause_params)
        params.extend([action_id, from_status])

        with self.db.scoped(scope) as cur:
            cur.execute(
                f"""UPDATE external_actions SET {', '.join(sets)}
                    WHERE {clause} AND action_id = %s AND status = %s
                    RETURNING {self._columns()}""",  # noqa: S608
                params,
            )
            row = cur.fetchone()
            if row is None:
                return None
            columns = [c.name for c in cur.description]
            return _to_action(dict(zip(columns, row, strict=True)))

    # -- Legacy methods (backward compatibility) -----------------------------

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
        """Legacy method: record a completed external action.

        v0.5.0-rc2: This method is retained for backward compatibility but
        should not be used by new code. Use reserve() + transition() instead.
        """
        from psycopg.types.json import Jsonb

        from construction_ai.persistence.serialization import dumps

        # Check for an existing action with the same idempotency key.
        existing = self.get_by_key(scope=scope, operation=action_type, idempotency_key=idempotency_key)
        if existing is not None:
            return existing

        req_hash = hash_request(request_payload) if request_payload else None
        # Map legacy 'completed' to 'confirmed' for new rows.
        new_status = "confirmed" if status == "completed" else status
        with self.db.scoped(scope) as cur:
            cur.execute(
                """INSERT INTO external_actions(organization_id, action_type, operation,
                       target_system, target_id, idempotency_key, request_hash, result, status,
                       remote_system, remote_document_id, reserved_at, confirmed_at)
                   VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())
                   ON CONFLICT (organization_id, action_type, idempotency_key) DO NOTHING
                   RETURNING action_id""",
                (
                    scope.organization_id,
                    action_type,
                    action_type,
                    target_system,
                    target_id,
                    idempotency_key,
                    req_hash,
                    Jsonb(result, dumps=dumps) if result else None,
                    new_status,
                    target_system,
                    target_id,
                ),
            )
            row = cur.fetchone()
            if row is not None:
                action_id = row[0]
            else:
                # Race: someone else inserted. Load their row.
                cur.execute(
                    """SELECT action_id FROM external_actions
                       WHERE organization_id = %s AND action_type = %s AND idempotency_key = %s""",
                    (scope.organization_id, action_type, idempotency_key),
                )
                action_id = cur.fetchone()[0]

        return self.get(scope=scope, action_id=action_id)

    def find_by_key(self, *, scope: Scope, action_type: str, idempotency_key: str) -> ExternalAction | None:
        """Find an existing action by idempotency key. Returns None if not found."""
        return self.get_by_key(scope=scope, operation=action_type, idempotency_key=idempotency_key)

    def list(self, *, scope: Scope, action_type: str | None = None, limit: int = 100) -> list[ExternalAction]:
        """List external actions, optionally filtered by action_type."""
        clause, params = self._tenant_clause(scope)
        sql = self._select_sql() + f" WHERE {clause}"
        if action_type:
            sql += " AND action_type = %s"
            params.append(action_type)
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        rows = self._fetch_all(scope, sql, params)
        return [_to_action(r) for r in rows]

    # -- Internal helpers ----------------------------------------------------

    def _columns(self) -> str:
        return (
            "organization_id, action_id, action_type, operation, target_system, target_id, "
            "idempotency_key, request_hash, result, status, created_at, "
            "subject_type, subject_id, remote_system, remote_document_id, "
            "attempt_count, reserved_at, last_attempt_at, confirmed_at, last_error"
        )

    def _select_sql(self) -> str:
        return f"SELECT {self._columns()} FROM external_actions"


def _to_action(row: dict[str, Any]) -> ExternalAction:
    return ExternalAction(
        action_id=row["action_id"] if isinstance(row.get("action_id"), UUID) else UUID(str(row["action_id"])),
        organization_id=row["organization_id"] if isinstance(row.get("organization_id"), UUID) else UUID(str(row["organization_id"])),
        action_type=row.get("action_type") or row.get("operation") or "",
        operation=row.get("operation") or row.get("action_type") or "",
        target_system=row.get("target_system") or "erpnext",
        target_id=row.get("target_id"),
        idempotency_key=row["idempotency_key"],
        request_hash=row.get("request_hash"),
        result=row.get("result"),
        status=row["status"],
        created_at=row["created_at"],
        subject_type=row.get("subject_type"),
        subject_id=row["subject_id"] if isinstance(row.get("subject_id"), UUID) else (UUID(str(row["subject_id"])) if row.get("subject_id") else None),
        remote_system=row.get("remote_system"),
        remote_document_id=row.get("remote_document_id"),
        attempt_count=row.get("attempt_count") or 0,
        reserved_at=row.get("reserved_at"),
        last_attempt_at=row.get("last_attempt_at"),
        confirmed_at=row.get("confirmed_at"),
        last_error=row.get("last_error"),
    )
