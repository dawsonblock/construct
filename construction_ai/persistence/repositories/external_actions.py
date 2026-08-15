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
    # rc4 Phase 1: Lease fields.
    execution_owner: str | None = None
    lease_acquired_at: datetime | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    recovery_attempts: int = 0
    # rc4 Phase 3: Remote state.
    remote_state: str = "no_remote_effect"
    # rc4 Phase 5: ERP-visible idempotency key.
    erp_idempotency_key: str | None = None
    # rc4 Phase 8: Readback hash.
    readback_hash: str | None = None
    # rc4 Phase 16: Final audit completion.
    final_audit_event_id: UUID | None = None
    finalized_at: datetime | None = None
    # rc5 Phase 1: Request payload for unified recovery readback.
    request_payload: dict[str, Any] | None = None
    # rc7 Phase 22: Hash of request_payload, persisted at reservation time.
    # Recovery verifies Hash(request_payload) == request_payload_hash before
    # using the payload for comparison. If they differ, the intent record is
    # corrupt and recovery must fail closed with EXTERNAL_ACTION_PAYLOAD_CORRUPT.
    request_payload_hash: str | None = None
    # rc6: Time-bounded negative confirmation — the timestamp of the first
    # empty ERP search sweep. PROVEN_ABSENT now requires both
    # attempts >= threshold AND elapsed >= window since this timestamp.
    first_negative_observation_at: datetime | None = None


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
        from psycopg.types.json import Jsonb
        from construction_ai.persistence.serialization import dumps

        with self.db.scoped(scope) as cur:
            # Atomic insert-or-nothing.
            cur.execute(
                """INSERT INTO external_actions(
                       organization_id, action_type, operation, target_system,
                       idempotency_key, request_hash, request_payload, request_payload_hash, status,
                       subject_type, subject_id, remote_system,
                       remote_state, reserved_at
                   )
                   VALUES(%s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s, 'no_remote_effect', now())
                   ON CONFLICT (organization_id, action_type, idempotency_key) DO NOTHING
                   RETURNING action_id""",
                (
                    scope.organization_id,
                    operation,  # action_type (legacy column)
                    operation,  # operation (new column)
                    remote_system,
                    idempotency_key,
                    req_hash,
                    Jsonb(request_payload, dumps=dumps) if request_payload else None,
                    req_hash,  # rc7: request_payload_hash (same as request_hash for now)
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
        remote_state: str | None = None,
        erp_idempotency_key: str | None = None,
        readback_hash: str | None = None,
        final_audit_event_id: UUID | None = None,
        recovery_attempts: int | None = None,
        first_negative_observation_at: datetime | None = None,
        reset_negative_observation: bool = False,
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

        if to_status == "executing" and from_status != "executing":
            sets.append("attempt_count = attempt_count + 1")
        if from_status == "executing" and to_status != "executing":
            sets.append("execution_owner = NULL")
            sets.append("lease_acquired_at = NULL")
            sets.append("lease_expires_at = NULL")
            sets.append("heartbeat_at = NULL")
        if to_status == "confirmed":
            sets.append("confirmed_at = now()")
            sets.append("finalized_at = now()")
        if remote_document_id is not None:
            sets.append("remote_document_id = %s")
            params.append(remote_document_id)
        if result is not None:
            sets.append("result = %s")
            params.append(Jsonb(result, dumps=dumps))
        if last_error is not None:
            sets.append("last_error = %s")
            params.append(last_error)
        if remote_state is not None:
            sets.append("remote_state = %s")
            params.append(remote_state)
        if erp_idempotency_key is not None:
            sets.append("erp_idempotency_key = %s")
            params.append(erp_idempotency_key)
        if readback_hash is not None:
            sets.append("readback_hash = %s")
            params.append(readback_hash)
        if final_audit_event_id is not None:
            sets.append("final_audit_event_id = %s")
            params.append(final_audit_event_id)
        if recovery_attempts is not None:
            sets.append("recovery_attempts = %s")
            params.append(recovery_attempts)
        # rc7: Support explicit reset of first_negative_observation_at to NULL.
        # When reset_negative_observation=True, set the column to NULL even
        # though first_negative_observation_at is None. This is needed because
        # the negative-observation state must be reset when a positive remote
        # document is later observed.
        if first_negative_observation_at is not None:
            sets.append("first_negative_observation_at = %s")
            params.append(first_negative_observation_at)
        elif reset_negative_observation:
            sets.append("first_negative_observation_at = NULL")

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

    # -- rc4 Phase 1: Lease management ---------------------------------------

    def acquire(
        self,
        *,
        scope: Scope,
        action_id: UUID,
        owner: str,
        lease_duration_seconds: int = 120,
        from_status: str = "pending",
    ) -> ExternalAction | None:
        """Atomically acquire the execution lease for an action.

        Transitions the action from `from_status` (default PENDING) to EXECUTING
        and sets the lease owner, acquisition time, and expiry. Only one worker
        can hold the lease at a time.

        Returns the updated action if this worker won the lease, or None if
        another worker already holds it (or the status didn't match).
        """
        from datetime import timedelta

        with self.db.scoped(scope) as cur:
            cur.execute(
                f"""UPDATE external_actions
                    SET status = 'executing',
                        execution_owner = %s,
                        lease_acquired_at = now(),
                        lease_expires_at = now() + %s::interval,
                        heartbeat_at = now(),
                        attempt_count = attempt_count + 1,
                        last_attempt_at = now()
                    WHERE {self._tenant_clause(scope)[0]} AND action_id = %s AND status = %s
                    RETURNING {self._columns()}""",  # noqa: S608
                [owner, timedelta(seconds=lease_duration_seconds)]
                + self._tenant_clause(scope)[1]
                + [action_id, from_status],
            )
            row = cur.fetchone()
            if row is None:
                return None
            columns = [c.name for c in cur.description]
            return _to_action(dict(zip(columns, row, strict=True)))

    def heartbeat(
        self,
        *,
        scope: Scope,
        action_id: UUID,
        owner: str,
        lease_duration_seconds: int = 120,
    ) -> ExternalAction | None:
        """Extend the lease on an EXECUTING action.

        Only the current lease owner may heartbeat. Returns the updated action,
        or None if the action is not EXECUTING, the owner doesn't match, or the
        lease has already expired.
        """
        from datetime import timedelta

        with self.db.scoped(scope) as cur:
            cur.execute(
                f"""UPDATE external_actions
                    SET heartbeat_at = now(),
                        lease_expires_at = now() + %s::interval
                    WHERE {self._tenant_clause(scope)[0]}
                      AND action_id = %s
                      AND status = 'executing'
                      AND execution_owner = %s
                      AND lease_expires_at > now()
                    RETURNING {self._columns()}""",  # noqa: S608
                [timedelta(seconds=lease_duration_seconds)]
                + self._tenant_clause(scope)[1]
                + [action_id, owner],
            )
            row = cur.fetchone()
            if row is None:
                return None
            columns = [c.name for c in cur.description]
            return _to_action(dict(zip(columns, row, strict=True)))

    def release(
        self,
        *,
        scope: Scope,
        action_id: UUID,
        owner: str,
        to_status: str = "failed_retryable",
        last_error: str | None = None,
    ) -> ExternalAction | None:
        """Release the lease on an EXECUTING action.

        Only the current lease owner may release. Transitions to `to_status`
        (default FAILED_RETRYABLE) and clears the lease fields.
        """
        sets = [
            "status = %s", "execution_owner = NULL",
            "lease_acquired_at = NULL", "lease_expires_at = NULL",
            "heartbeat_at = NULL", "last_attempt_at = now()",
        ]
        params: list[Any] = [to_status]
        if last_error is not None:
            sets.append("last_error = %s")
            params.append(last_error)

        clause, clause_params = self._tenant_clause(scope)
        params.extend(clause_params)
        params.extend([action_id, owner])

        with self.db.scoped(scope) as cur:
            cur.execute(
                f"""UPDATE external_actions SET {', '.join(sets)}
                    WHERE {clause} AND action_id = %s AND status = 'executing'
                      AND execution_owner = %s
                    RETURNING {self._columns()}""",  # noqa: S608
                params,
            )
            row = cur.fetchone()
            if row is None:
                return None
            columns = [c.name for c in cur.description]
            return _to_action(dict(zip(columns, row, strict=True)))

    def reap_expired(self, *, scope: Scope, batch_limit: int = 100) -> list[ExternalAction]:
        """rc4 Phase 1: Reap EXECUTING actions whose leases have expired.

        Transitions expired EXECUTING actions to UNKNOWN with remote_state
        REMOTE_UNKNOWN. This is the automatic recovery that the recovery daemon
        calls — it happens in production code, not manually in tests.

        Returns the list of reaped actions.
        """
        clause, clause_params = self._tenant_clause(scope)
        with self.db.scoped(scope) as cur:
            cur.execute(
                f"""UPDATE external_actions
                    SET status = 'unknown',
                        remote_state = 'remote_unknown',
                        last_error = COALESCE(last_error, '') || 'lease expired; ',
                        recovery_attempts = recovery_attempts + 1,
                        execution_owner = NULL,
                        lease_acquired_at = NULL,
                        lease_expires_at = NULL,
                        heartbeat_at = NULL
                    WHERE {clause}
                      AND status = 'executing'
                      AND lease_expires_at < now()
                      AND action_id IN (
                        SELECT action_id FROM external_actions
                        WHERE {clause}
                          AND status = 'executing'
                          AND lease_expires_at < now()
                        LIMIT %s FOR UPDATE SKIP LOCKED
                      )
                    RETURNING {self._columns()}""",  # noqa: S608
                clause_params + clause_params + [batch_limit],
            )
            rows = cur.fetchall()
            if not rows:
                return []
            columns = [c.name for c in cur.description]
            return [_to_action(dict(zip(columns, row, strict=True))) for row in rows]

    def find_missing_final_audit(self, *, scope: Scope, batch_limit: int = 100) -> list[ExternalAction]:
        """rc4 Phase 16: Find CONFIRMED actions missing their final audit event.

        These actions need audit repair — a deterministic repair audit event
        must be appended.
        """
        clause, clause_params = self._tenant_clause(scope)
        with self.db.scoped(scope) as cur:
            cur.execute(
                self._select_sql() + f" WHERE {clause} AND status = 'confirmed' AND final_audit_event_id IS NULL LIMIT %s",
                clause_params + [batch_limit],
            )
            rows = cur.fetchall()
            if not rows:
                return []
            columns = [c.name for c in cur.description]
            return [_to_action(dict(zip(columns, row, strict=True))) for row in rows]

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
        new_remote_state = "remote_submitted" if new_status == "confirmed" else "no_remote_effect"
        with self.db.scoped(scope) as cur:
            cur.execute(
                """INSERT INTO external_actions(organization_id, action_type, operation,
                       target_system, target_id, idempotency_key, request_hash, result, status,
                       remote_system, remote_document_id, remote_state, reserved_at, confirmed_at)
                   VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())
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
                    new_remote_state,
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
            "attempt_count, reserved_at, last_attempt_at, confirmed_at, last_error, "
            "execution_owner, lease_acquired_at, lease_expires_at, heartbeat_at, "
            "recovery_attempts, remote_state, erp_idempotency_key, readback_hash, "
            "final_audit_event_id, finalized_at, request_payload, "
            "request_payload_hash, first_negative_observation_at"
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
        execution_owner=row.get("execution_owner"),
        lease_acquired_at=row.get("lease_acquired_at"),
        lease_expires_at=row.get("lease_expires_at"),
        heartbeat_at=row.get("heartbeat_at"),
        recovery_attempts=row.get("recovery_attempts") or 0,
        remote_state=row.get("remote_state") or "no_remote_effect",
        erp_idempotency_key=row.get("erp_idempotency_key"),
        readback_hash=row.get("readback_hash"),
        final_audit_event_id=row["final_audit_event_id"] if isinstance(row.get("final_audit_event_id"), UUID) else (UUID(str(row["final_audit_event_id"])) if row.get("final_audit_event_id") else None),
        finalized_at=row.get("finalized_at"),
        request_payload=row.get("request_payload"),
        request_payload_hash=row.get("request_payload_hash"),
        first_negative_observation_at=row.get("first_negative_observation_at"),
    )
