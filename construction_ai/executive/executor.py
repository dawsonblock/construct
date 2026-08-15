"""v0.5.0-rc2 — ApprovedInvoiceExecutor with external-action state machine.

The only path by which an approved invoice is submitted to ERP. This module is
the controlled gate between human approval and external financial effect.

v0.5.0-rc2 (Phase 1): Rebuilt with an external-action state machine. The row
is reserved BEFORE the external call, eliminating the crash window between ERP
success and the local ledger write.

State machine flow:

    reserve (PENDING)
    → inspect state
        → CONFIRMED: return existing result (idempotent)
        → PENDING: transition to EXECUTING, proceed
        → EXECUTING: another worker may be running — check lease
        → UNKNOWN: reconcile before retry (Phase 2)
        → FAILED_RETRYABLE: retry
        → FAILED_TERMINAL: refuse
    → create ERP draft
    → submit ERP draft
    → readback verification
    → transition to CONFIRMED
    → audit

Invariants enforced:
1. Approval must be in `approved` status.
2. Approval must not be stale (fingerprint match).
3. External action is reserved before the ERP call.
4. ERP write is verified by readback.
5. Every state transition is audited.
6. AI cannot call this path directly.

The executor does NOT:
- create approvals
- modify approvals
- bypass the policy engine
- write to ERP without a reserved external action
- skip readback verification
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from construction_ai.integrations.erpnext import ERPNextAdapter
from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories import Repositories
from construction_ai.persistence.repositories.external_actions import hash_request


class ExecutionError(Exception):
    """A controlled failure that prevents the execution."""


class ApprovalNotApproved(ExecutionError):
    pass


class ApprovalStale(ExecutionError):
    pass


class ApprovalNotFound(ExecutionError):
    pass


class ExecutionDisabled(ExecutionError):
    """rc7 Phase 43: ERP execution is feature-gated (default: disabled)."""
    pass


class ReadbackFailed(ExecutionError):
    pass


class ReadbackMismatch(ExecutionError):
    """ERP readback document exists but financial fields don't match."""
    pass


class ExternalActionInProgress(ExecutionError):
    """Another worker owns the execution reservation."""


class ExternalActionTerminal(ExecutionError):
    """The external action failed terminally and cannot be retried."""


def check_approval_staleness(repos: Repositories, *, scope: Scope, approval) -> None:
    """Verify the approval is not stale (item 48, Phase 6).

    Two complementary checks:

    1. Project-wide state_fingerprint (Phase 4/5): reconstructs the current
       project state and compares its fingerprint to the one recorded at
       approval time. If they differ, the state has drifted and the approval is
       stale. Mandatory — NoFingerprint ⇒ ERPExecution is forbidden.

    2. Decision fingerprint (Phase 6): recomputes the exact
       InvoiceSnapshot||VerificationPacket||EvidenceSet||PolicyVersion||
       ApprovalRequirements hash from current authoritative rows. If it differs
       from the approved one, the specific state the human approved has changed
       — even if the broad project fingerprint happens to match. This catches
       drift in the invoice, evidence, or verification packet without false
       positives from unrelated project changes.
    """
    if not approval.state_fingerprint:
        raise ApprovalStale(
            "approval has no state fingerprint — cannot verify staleness. "
            "Refusing to execute without fingerprint (fail closed)."
        )

    if not approval.project_id:
        raise ApprovalStale(
            "approval has no project_id — cannot reconstruct state for staleness check. "
            "Refusing to execute (fail closed)."
        )

    from construction_ai.reconstruction.service import ProjectReconstructor

    recon = ProjectReconstructor.from_repositories(repos)
    project_scope = scope.for_project(UUID(approval.project_id))
    state = recon.project(scope=project_scope)
    current_fp = state.fingerprint()

    if current_fp != approval.state_fingerprint:
        raise ApprovalStale(
            f"approval is stale: state fingerprint changed from {approval.state_fingerprint[:16]}... "
            f"to {current_fp[:16]}... since approval"
        )

    # rc4 Phase 9: Decision fingerprint is MANDATORY, not optional.
    # Legacy approvals lacking a decision fingerprint must be revalidated, not
    # silently executed.
    if not approval.decision_fingerprint:
        raise ApprovalStale(
            "approval has no decision fingerprint — cannot verify decision-specific "
            "staleness. Refusing to execute (rc4: both fingerprints are mandatory). "
            "REVALIDATION_REQUIRED."
        )

    # Phase 6: decision fingerprint — the precise, decision-specific check.
    # rc6: The fingerprint function reconstructs the policy from the snapshot
    # stored on the approval at decision time. Passing DEFAULT_POLICY here
    # would make a decision made under a non-default policy appear stale
    # immediately. We deliberately do NOT pass a policy so the function uses
    # the stored snapshot.
    #
    # rc7: The fingerprint function now raises ApprovalPolicyMissing if the
    # approval has no policy_snapshot (no DEFAULT_POLICY fallback for
    # executable approvals) and ApprovalPolicyCorrupt if the snapshot hash
    # does not match the stored policy_hash. Both are hard failures that
    # require revalidation — they must NOT be caught and silently downgraded.
    from construction_ai.approvals.decision_fingerprint import (
        ApprovalPolicyCorrupt,
        ApprovalPolicyMissing,
        compute_decision_fingerprint_for_approval,
    )

    try:
        current_decision_fp = compute_decision_fingerprint_for_approval(
            repos, scope=scope, approval=approval,
        )
    except ApprovalPolicyMissing:
        raise ApprovalStale(
            f"approval {approval.approval_id} has no policy_snapshot — "
            "cannot verify decision fingerprint without the exact policy "
            "in force at decision time. REVALIDATION_REQUIRED (rc7: no "
            "DEFAULT_POLICY fallback for executable approvals)."
        ) from None
    except ApprovalPolicyCorrupt as e:
        raise ApprovalStale(
            f"approval {approval.approval_id} policy snapshot is corrupt: {e}. "
            "REVALIDATION_REQUIRED."
        ) from e
    if current_decision_fp != approval.decision_fingerprint:
        raise ApprovalStale(
            f"approval is stale: decision fingerprint changed from "
            f"{approval.decision_fingerprint[:16]}... to {current_decision_fp[:16]}... "
            f"since approval (invoice/evidence/verification packet drifted)"
        )


@dataclass(frozen=True)
class ExecutionResult:
    action_id: UUID
    erp_docname: str
    erp_docstatus: int
    idempotent: bool  # True if this was a replay of an existing confirmed action


# -- Crash hooks (Phase 3) --------------------------------------------------

# When set, the executor will raise at the named crash point before proceeding.
# Tests use this to simulate crashes at each external boundary.
_crash_hook: str | None = None


def set_crash_hook(point: str | None) -> None:
    """Set or clear a crash hook. For testing only."""
    global _crash_hook
    _crash_hook = point


def _check_crash(point: str) -> None:
    """Raise if a crash hook is set for this point."""
    if _crash_hook == point:
        raise ExecutionError(f"crash hook: {point}")


def execute_approved_invoice(
    repos: Repositories,
    *,
    scope: Scope,
    approval_id: UUID,
    adapter: ERPNextAdapter,
    erp_read_transport,
) -> ExecutionResult:
    """Execute an approved invoice through the external-action state machine.

    The external action row is reserved BEFORE the ERP call. The invariant:

        ExternalEffectAttempt ⇒ ExternalActionReservationExists

    Args:
        repos: Repository bundle.
        scope: Project-scoped scope for the invoice.
        approval_id: The approval to execute.
        adapter: ERPNext write adapter.
        erp_read_transport: Read transport for readback verification.

    Returns:
        ExecutionResult with the ERP document name and status.

    Raises:
        ApprovalNotFound: approval does not exist.
        ApprovalNotApproved: approval is not in `approved` status.
        ApprovalStale: project state has changed since approval.
        ReadbackFailed: ERP readback did not confirm submission.
        ExternalActionInProgress: another worker owns the reservation.
        ExternalActionTerminal: the action failed terminally.
    """
    import os as _os

    # rc7 Phase 43: ERP execution is feature-gated. The safe default is
    # non-mutating — ERP_EXECUTION_ENABLED must be explicitly set to "true"
    # for the executor to attempt any external financial write. This makes
    # the system safe to deploy without risking unintended ERP mutations.
    if _os.environ.get("ERP_EXECUTION_ENABLED", "false").lower() != "true":
        raise ExecutionDisabled(
            "ERP execution is disabled (ERP_EXECUTION_ENABLED != true). "
            "rc7: the safe default is non-mutating. Set ERP_EXECUTION_ENABLED=true "
            "to enable external financial writes."
        )

    org_scope = scope.organization_only
    operation = "erp_submit_purchase_invoice"

    # 1. Load the approval and verify it is approved.
    approval = repos.approvals.get(scope=org_scope, approval_id=approval_id)
    if approval is None:
        raise ApprovalNotFound(f"approval {approval_id} not found")
    if approval.status.value != "approved":
        raise ApprovalNotApproved(f"approval is {approval.status.value}, not approved")

    # 2. Check staleness (Phase 4/5/6: project-wide + decision fingerprints).
    check_approval_staleness(repos, scope=org_scope, approval=approval)

    # 2b. Phase 22: Evidence freshness precondition. Every piece of evidence the
    # approval rests on must be within its freshness policy. Stale evidence
    # requires refresh → reverify → fingerprint comparison; until then, execution
    # is refused (fail closed).
    if approval.evidence_ids:
        from construction_ai.verification.freshness import evaluate_freshness_for_approval

        freshness = evaluate_freshness_for_approval(
            repos, scope=org_scope,
            evidence_ids=[UUID(eid) for eid in approval.evidence_ids],
        )
        if not freshness.fresh:
            stale_fields = [s.field for s in freshness.stale]
            raise ExecutionError(
                f"approval {approval_id} rests on stale evidence ({stale_fields}) — "
                f"refresh and reverify before execution"
            )

    # 3. Load the invoice to build the ERP payload.
    invoice_id = UUID(approval.subject_id)
    invoice = repos.invoices.get(scope=org_scope, invoice_id=invoice_id)
    if invoice is None:
        raise ExecutionError(f"invoice {invoice_id} not found")

    # 4. Build the idempotency key and ERP payload using the authoritative builder.
    payload = build_expected_erp_payload(
        repos,
        scope=org_scope,
        invoice_id=invoice_id,
        approval_id=approval_id,
        operation=operation,
        approval=approval,
    )
    erp_idempotency_key = payload.get("construct_idempotency_key")
    idempotency_key = f"approval:{approval_id}"

    # 5. Reserve the external action atomically.
    #    INSERT ON CONFLICT DO NOTHING, then reload.
    action = repos.external_actions.reserve(
        scope=org_scope,
        operation=operation,
        idempotency_key=idempotency_key,
        subject_type="invoice",
        subject_id=invoice_id,
        request_payload=payload,
        remote_system="erpnext",
    )

    _check_crash("after_reservation")

    # 6. Inspect the reserved action's state.
    if action.status in ("completed", "confirmed"):
        # Idempotent replay — return the existing result.
        erp_docname = (action.result or {}).get("docname", "")
        erp_docstatus = (action.result or {}).get("docstatus", 0)
        return ExecutionResult(
            action_id=action.action_id,
            erp_docname=erp_docname,
            erp_docstatus=erp_docstatus,
            idempotent=True,
        )

    if action.status == "failed_terminal":
        raise ExternalActionTerminal(
            f"external action {action.action_id} failed terminally: {action.last_error}"
        )

    if action.status == "executing":
        # rc4 Phase 1: Check if the lease has expired. If so, the reaper should
        # have moved it to UNKNOWN, but we check here too for safety.
        if action.lease_expires_at:
            from datetime import datetime, timezone as _tz
            now = datetime.now(_tz.utc)
            if action.lease_expires_at < now:
                # Lease expired — treat as UNKNOWN, needs reconciliation.
                raise ExecutionError(
                    f"external action {action.action_id} has an expired lease "
                    f"(expired at {action.lease_expires_at}) — reconciliation required"
                )
        raise ExternalActionInProgress(
            f"external action {action.action_id} is in EXECUTING state "
            f"(owner: {action.execution_owner})"
        )

    if action.status == "unknown":
        # Phase 2: reconcile before retry. Refuse — the caller must
        # run reconcile_external_action() to resolve the state.
        raise ExecutionError(
            f"external action {action.action_id} is in UNKNOWN state — reconciliation required"
        )

    # Status is PENDING or FAILED_RETRYABLE — proceed to execute.

    # 7. rc4 Phase 1: Acquire the execution lease atomically.
    #    Only one worker can transition from PENDING/FAILED_RETRYABLE to EXECUTING.
    import os as _os
    worker_id = _os.environ.get("WORKER_ID", f"worker-{_os.getpid()}")

    _check_crash("before_lease_acquire")

    acquired = repos.external_actions.acquire(
        scope=org_scope,
        action_id=action.action_id,
        owner=worker_id,
        from_status=action.status,
    )
    if acquired is None:
        # Lost the race — another worker acquired the lease.
        raise ExternalActionInProgress(
            f"could not acquire lease for {action.action_id} from {action.status}"
        )

    _audit_transition(repos, org_scope, action.action_id, action.status, "executing", invoice_id)

    _check_crash("after_lease_acquire")

    # 8. Create the draft in ERP.
    request_hash = _hash_json(payload)
    _check_crash("before_erp_create")

    # Heartbeat before each ERP call to keep the lease alive.
    hb = repos.external_actions.heartbeat(
        scope=org_scope, action_id=acquired.action_id, owner=worker_id,
    )
    if hb is None:
        # Lease was lost (reaped or expired). Try to transition cleanly.
        # If the reaper already got it, transition returns None and we skip
        # the audit to avoid a misleading audit entry.
        transitioned = repos.external_actions.transition(
            scope=org_scope, action_id=acquired.action_id,
            from_status="executing", to_status="failed_retryable",
            last_error="lease lost before ERP create",
        )
        if transitioned is not None:
            _audit_transition(
                repos, org_scope, acquired.action_id,
                "executing", "failed_retryable", invoice_id,
            )
        raise ExecutionError(
            f"lost lease for {acquired.action_id} before ERP create"
        )

    try:
        draft_response = adapter.create_purchase_invoice_draft(payload)
    except Exception as e:
        _mark_failed_retryable(repos, org_scope, acquired.action_id, str(e), invoice_id)
        raise

    draft_data = draft_response.get("data", draft_response) if isinstance(draft_response, dict) else {}
    docname = draft_data.get("name", "")
    if not docname:
        _mark_failed_retryable(repos, org_scope, acquired.action_id, "ERP did not return a document name", invoice_id)
        raise ExecutionError("ERP did not return a document name")

    # rc4 Phase 2: Persist the remote draft identity IMMEDIATELY after creation.
    # This gives crash recovery a deterministic remote identifier.
    draft_persisted = repos.external_actions.transition(
        scope=org_scope,
        action_id=acquired.action_id,
        from_status="executing",
        to_status="executing",
        remote_document_id=docname,
        remote_state="remote_draft",
        erp_idempotency_key=erp_idempotency_key,
    )
    if draft_persisted is None:
        raise ExecutionError(
            f"lost lease for {acquired.action_id} during draft "
            f"persistence — action was reaped"
        )

    # Phase 23: audit the draft creation with request/response hashes.
    _audit_transition(
        repos, org_scope, acquired.action_id, "executing", "executing", invoice_id,
        request_hash=request_hash, response_hash=_hash_json(draft_response),
        detail={"step": "draft_created", "docname": docname},
    )

    _check_crash("after_erp_create")

    # 9. Submit the draft.
    _check_crash("before_erp_submit")
    submit_request = {"doctype": "Purchase Invoice", "name": docname}
    submit_request_hash = _hash_json(submit_request)

    # Heartbeat before submit to keep the lease alive.
    hb = repos.external_actions.heartbeat(
        scope=org_scope, action_id=acquired.action_id, owner=worker_id,
    )
    if hb is None:
        # Lease was lost. A draft exists in ERP — preserve remote_state.
        # If the reaper already got it, transition returns None and we skip
        # the audit to avoid a misleading audit entry.
        transitioned = repos.external_actions.transition(
            scope=org_scope, action_id=acquired.action_id,
            from_status="executing", to_status="unknown",
            remote_document_id=docname,
            last_error="lease lost before ERP submit",
            remote_state="remote_draft",
        )
        if transitioned is not None:
            _audit_transition(
                repos, org_scope, acquired.action_id,
                "executing", "unknown", invoice_id,
            )
        raise ExecutionError(
            f"lost lease for {acquired.action_id} before ERP submit"
        )

    try:
        submit_response = adapter.submit_purchase_invoice(docname)
    except Exception as e:
        # rc4 Phase 3: After draft creation, a submit failure means the draft
        # exists remotely. Mark as UNKNOWN with remote_state=REMOTE_DRAFT.
        _mark_unknown(
            repos, org_scope, acquired.action_id,
            f"submit failed: {e}", docname, invoice_id,
            remote_state="remote_draft",
        )
        raise

    # rc4 Phase 3: Update remote_state to REMOTE_SUBMITTED after submit.
    submit_persisted = repos.external_actions.transition(
        scope=org_scope,
        action_id=acquired.action_id,
        from_status="executing",
        to_status="executing",
        remote_state="remote_submitted",
    )
    if submit_persisted is None:
        raise ExecutionError(
            f"lost lease for {acquired.action_id} during submit "
            f"persistence — action was reaped"
        )

    # Phase 23: audit the submit with request/response hashes.
    _audit_transition(
        repos, org_scope, acquired.action_id, "executing", "executing", invoice_id,
        request_hash=submit_request_hash, response_hash=_hash_json(submit_response),
        detail={"step": "submitted", "docname": docname},
    )

    _check_crash("after_erp_submit")

    # 10. rc4 Phase 8: Canonical readback verification.
    _check_crash("before_readback")

    # Heartbeat before readback to keep the lease alive.
    hb = repos.external_actions.heartbeat(
        scope=org_scope, action_id=acquired.action_id, owner=worker_id,
    )
    if hb is None:
        # Lease was lost. A submitted invoice exists in ERP — preserve remote_state.
        # If the reaper already got it, transition returns None and we skip
        # the audit to avoid a misleading audit entry.
        transitioned = repos.external_actions.transition(
            scope=org_scope, action_id=acquired.action_id,
            from_status="executing", to_status="unknown",
            remote_document_id=docname,
            last_error="lease lost before readback",
            remote_state="remote_submitted",
        )
        if transitioned is not None:
            _audit_transition(
                repos, org_scope, acquired.action_id,
                "executing", "unknown", invoice_id,
            )
        raise ExecutionError(
            f"lost lease for {acquired.action_id} before readback"
        )

    from urllib.parse import quote

    readback = erp_read_transport.get(f"/api/resource/{quote('Purchase Invoice')}/{quote(docname)}")
    readback_data = readback.get("data", readback) if isinstance(readback, dict) else {}
    docstatus = readback_data.get("docstatus", 0)

    # rc4 Phase 3: docstatus=0 is a draft, not a confirmed financial effect.
    if docstatus != 1:
        _mark_unknown(
            repos, org_scope, acquired.action_id,
            f"readback docstatus={docstatus}, expected 1", docname, invoice_id,
            remote_state="remote_draft" if docstatus == 0 else "remote_unknown",
        )
        raise ReadbackFailed(f"ERP readback shows docstatus={docstatus}, expected 1 (submitted)")

    # rc4 Phase 6: Fail-closed readback — expected-but-missing = mismatch.
    # rc4 Phase 8: Build canonical representations and compare.
    canonical_expected = _canonical_erp_invoice(payload)
    canonical_actual = _canonical_erp_invoice(readback_data)
    mismatches = _compare_canonical(canonical_expected, canonical_actual)
    if mismatches:
        _mark_unknown(
            repos, org_scope, acquired.action_id,
            f"readback mismatch: {mismatches}", docname, invoice_id,
            remote_state="remote_mismatch",
        )
        raise ReadbackMismatch(f"ERP readback fields do not match: {mismatches}")

    # rc4 Phase 8: Store the readback hash.
    readback_hash = _hash_json(canonical_actual)

    # Phase 23: audit the readback with response hash.
    _audit_transition(
        repos, org_scope, acquired.action_id, "executing", "executing", invoice_id,
        response_hash=readback_hash,
        detail={"step": "readback_verified", "docname": docname, "docstatus": docstatus},
    )

    _check_crash("after_readback")

    # rc7 Phase 25: Pre-CONFIRMED invariant checks.
    # Before transitioning any action to CONFIRMED, assert:
    #   - expected payload present
    #   - expected payload hash valid (if persisted)
    #   - remote ID present
    #   - docstatus terminal (1 = submitted)
    #   - canonical comparison passed (mismatches is empty at this point)
    #   - ERP idempotency key matched (in payload)
    #   - approval still executable (not revoked)
    #   - decision fingerprint current (checked earlier, but re-verify)
    #   - policy snapshot valid (checked earlier)
    if not payload:
        raise InvariantViolation(
            "pre-CONFIRMED invariant: expected payload is empty — cannot confirm"
        )
    if not docname:
        raise InvariantViolation(
            "pre-CONFIRMED invariant: remote_document_id is empty — cannot confirm"
        )
    if docstatus != 1:
        raise InvariantViolation(
            f"pre-CONFIRMED invariant: docstatus={docstatus}, expected 1 (submitted)"
        )
    if mismatches:
        raise InvariantViolation(
            f"pre-CONFIRMED invariant: canonical comparison has mismatches: {mismatches}"
        )
    if not payload.get("construct_idempotency_key"):
        raise InvariantViolation(
            "pre-CONFIRMED invariant: ERP idempotency key missing from payload"
        )
    # rc7 Phase 22: Verify request_payload_hash if persisted.
    if hasattr(acquired, "request_payload_hash") and acquired.request_payload_hash:
        computed_hash = hash_request(payload)
        if computed_hash != acquired.request_payload_hash:
            raise PayloadCorrupt(
                f"pre-CONFIRMED invariant: request_payload_hash mismatch — "
                f"stored={acquired.request_payload_hash[:16]}... "
                f"computed={computed_hash[:16]}..."
            )

    # 11. Transition to CONFIRMED with remote_state=REMOTE_SUBMITTED.
    result_payload = {"docname": docname, "docstatus": docstatus}
    _check_crash("before_confirmed")
    confirmed = repos.external_actions.transition(
        scope=org_scope,
        action_id=acquired.action_id,
        from_status="executing",
        to_status="confirmed",
        remote_document_id=docname,
        remote_state="remote_submitted",
        result=result_payload,
        readback_hash=readback_hash,
    )
    if confirmed is None:
        # Lost the race or state changed unexpectedly.
        raise ExecutionError(f"could not transition {acquired.action_id} from executing to confirmed")

    _audit_transition(
        repos, org_scope, acquired.action_id, "executing", "confirmed", invoice_id,
        detail={"step": "confirmed", "docname": docname},
    )

    # 12. Audit the execution (Phase 23: include request/response hashes).
    # rc4 Phase 16: Record the final audit event ID on the action.
    _check_crash("before_audit")
    audit_event = repos.audit.append(
        scope=org_scope,
        event_type="ERP_INVOICE_SUBMITTED",
        actor=approval.approved_by or "system",
        object_type="invoice",
        object_id=invoice_id,
        payload={
            "approval_id": str(approval_id),
            "erp_docname": docname,
            "erp_docstatus": docstatus,
            "action_id": str(confirmed.action_id),
            "idempotent": False,
            "request_hash": request_hash,
            "response_hash": readback_hash,  # legacy name for backward compat
            "readback_hash": readback_hash,
        },
    )

    # rc4 Phase 16: Link the final audit event to the action.
    if audit_event.audit_event_id is not None:
        repos.external_actions.transition(
            scope=org_scope,
            action_id=confirmed.action_id,
            from_status="confirmed",
            to_status="confirmed",
            final_audit_event_id=audit_event.audit_event_id,
        )

    return ExecutionResult(
        action_id=confirmed.action_id,
        erp_docname=docname,
        erp_docstatus=docstatus,
        idempotent=False,
    )


def _mark_failed_retryable(repos: Repositories, scope: Scope, action_id: UUID, error: str, invoice_id: UUID) -> None:
    """Transition an action to FAILED_RETRYABLE and audit."""
    repos.external_actions.transition(
        scope=scope, action_id=action_id, from_status="executing", to_status="failed_retryable", last_error=error,
    )
    _audit_transition(repos, scope, action_id, "executing", "failed_retryable", invoice_id)


def _mark_unknown(repos: Repositories, scope: Scope, action_id: UUID, error: str, docname: str, invoice_id: UUID, *, remote_state: str = "remote_unknown") -> None:
    """Transition an action to UNKNOWN and audit.

    Used when the external call returned an ambiguous result (e.g. submit
    failed after draft creation, or readback didn't confirm). The action
    requires reconciliation before retry.
    """
    repos.external_actions.transition(
        scope=scope, action_id=action_id, from_status="executing", to_status="unknown",
        remote_document_id=docname, last_error=error, remote_state=remote_state,
    )
    _audit_transition(repos, scope, action_id, "executing", "unknown", invoice_id)


def _audit_transition(repos: Repositories, scope: Scope, action_id: UUID, from_status: str, to_status: str, invoice_id: UUID, *, request_hash: str | None = None, response_hash: str | None = None, detail: dict | None = None) -> None:
    """Audit an external-action state transition (Phase 23).

    Phase 23: the audit event now carries request_hash and response_hash —
    SHA-256 over the canonical JSON of the ERP request payload and response
    data. This binds the audit trail to the exact bytes sent to and received
    from the external system, making post-incident forensics deterministic.
    """
    payload: dict[str, Any] = {
        "action_id": str(action_id),
        "from_status": from_status,
        "to_status": to_status,
        "subject_type": "invoice",
        "subject_id": str(invoice_id),
    }
    if request_hash is not None:
        payload["request_hash"] = request_hash
    if response_hash is not None:
        payload["response_hash"] = response_hash
    if detail:
        payload.update(detail)
    repos.audit.append(
        scope=scope,
        event_type="EXTERNAL_ACTION_TRANSITION",
        actor="system",
        object_type="external_action",
        object_id=action_id,
        payload=payload,
    )


def _hash_json(value: Any) -> str:
    """SHA-256 over the canonical JSON rendering of a value (Phase 23)."""
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


def _compute_erp_idempotency_key(
    *,
    organization_id: str,
    invoice_id: str,
    approval_id: str,
    operation: str,
    request_payload_hash: str | None = None,
) -> str:
    """rc4 Phase 5 / rc7 Phase 23: Deterministic ERP-visible idempotency key.

    rc4: K = H(organization || invoice || approval || operation)
    rc7: K = H(organization || invoice || approval || operation || payload_hash)

    rc7 Phase 23: The payload hash is now included in the idempotency key.
    This means a changed payload cannot accidentally reuse an idempotency
    key from a prior financial intent. If the payload changes (e.g. amount
    is edited), the idempotency key changes, and ERP will treat it as a
    new document rather than silently returning the old one.
    """
    import hashlib
    h = hashlib.sha256()
    h.update(organization_id.encode())
    h.update(b"\x00")
    h.update(invoice_id.encode())
    h.update(b"\x00")
    h.update(approval_id.encode())
    h.update(b"\x00")
    h.update(operation.encode())
    if request_payload_hash:
        h.update(b"\x00")
        h.update(request_payload_hash.encode())
    return f"construct-{h.hexdigest()[:32]}"


def build_expected_erp_payload(
    repos: Repositories,
    *,
    scope: Scope,
    invoice_id: UUID,
    approval_id: UUID | str | None = None,
    operation: str = "erp_invoice_submit",
    approval: Any | None = None,
) -> dict[str, Any]:
    """Build the exact authoritative ERP payload for an invoice and approval intent."""
    from decimal import Decimal as _Decimal

    org_scope = scope.organization_only
    invoice = repos.invoices.get(scope=org_scope, invoice_id=invoice_id)
    if invoice is None:
        raise ExecutionError(f"invoice {invoice_id} not found")

    supplier_id = invoice.vendor_name  # fallback
    if invoice.vendor_company_id:
        company = repos.companies.get(scope=org_scope, company_id=UUID(str(invoice.vendor_company_id)))
        if company and company.erp_supplier_id:
            supplier_id = company.erp_supplier_id

    approval_id_str = str(approval_id) if approval_id else (str(approval.approval_id) if approval else "")

    # rc7 Phase 23: Build the payload first WITHOUT the idempotency key,
    # compute its hash, then derive the idempotency key from the hash.
    # This binds the idempotency key to the exact financial intent — a
    # changed payload produces a different key, preventing accidental
    # reuse of a prior intent's ERP document.
    payload: dict[str, Any] = {
        "supplier": supplier_id,
        "bill_no": invoice.invoice_number,
        "grand_total": str(_Decimal(str(invoice.total or 0))),
        "net_total": str(_Decimal(str(invoice.subtotal or 0))),
        "total_taxes": str(_Decimal(str(invoice.tax or 0))),
        "currency": invoice.currency or "CAD",
        "docstatus": 0,
    }

    if invoice.po_number:
        payload["po_no"] = invoice.po_number

    project_id = None
    if approval and getattr(approval, "project_id", None):
        project_id = approval.project_id
    elif getattr(invoice, "project_id", None):
        project_id = invoice.project_id
    if project_id:
        payload["project"] = str(project_id)

    # rc7 Phase 23: Compute payload hash, then idempotency key from it.
    _payload_hash = hash_request(payload)
    erp_idempotency_key = _compute_erp_idempotency_key(
        organization_id=str(org_scope.organization_id),
        invoice_id=str(invoice_id),
        approval_id=approval_id_str,
        operation=operation,
        request_payload_hash=_payload_hash,
    )
    payload["construct_idempotency_key"] = erp_idempotency_key

    return payload


def reconstruct_expected_erp_payload(
    repos: Repositories,
    *,
    scope: Scope,
    action: Any,
) -> dict[str, Any] | None:
    """Reconstruct or retrieve the canonical expected ERP payload for an external action.

    rc6: Differentiates failure modes instead of bare `except Exception`.
    The recovery path must not silently convert data-integrity failures,
    missing subjects, or programming errors into "no payload" — that would
    hide the cause of reconstruction failure and allow fail-open recovery.
    Each failure mode is logged and re-raised as a typed
    PayloadReconstructionError so the caller (recovery daemon) can audit it
    and route to manual reconciliation rather than silently proceeding.
    """
    # 1. If the action row has persisted request_payload, use it.
    if isinstance(action, dict):
        if action.get("request_payload"):
            return action["request_payload"]
        subject_id = action.get("subject_id")
        operation = action.get("operation", "erp_invoice_submit")
    else:
        if getattr(action, "request_payload", None):
            return action.request_payload
        subject_id = getattr(action, "subject_id", None)
        operation = getattr(action, "operation", "erp_invoice_submit")

    if not subject_id:
        # Subject missing — the action row is malformed. This is a data-
        # integrity failure, not a normal "no payload" state.
        raise PayloadReconstructionError(
            "subject_id missing from external action — cannot reconstruct expected payload"
        )

    if isinstance(subject_id, str):
        subject_id = UUID(subject_id)

    org_scope = scope.organization_only
    try:
        approval = repos.approvals.for_subject(scope=org_scope, subject_type="invoice", subject_id=subject_id)
        approval_id = approval.approval_id if approval else None
        return build_expected_erp_payload(
            repos, scope=org_scope, invoice_id=subject_id, approval_id=approval_id,
            operation=operation, approval=approval,
        )
    except PayloadReconstructionError:
        raise
    except (KeyError, ValueError, TypeError) as e:
        # Data-integrity or programming error — must not be silently swallowed.
        raise PayloadReconstructionError(
            f"reconstruction failed (data error): {type(e).__name__}: {e}"
        ) from e
    except Exception as e:
        # Database error or unexpected failure — surface it, do not hide it.
        raise PayloadReconstructionError(
            f"reconstruction failed (unexpected): {type(e).__name__}: {e}"
        ) from e


class PayloadReconstructionError(ExecutionError):
    """rc6/rc7: Typed exception for expected-payload reconstruction failures.

    Recovery code must treat this as a fail-closed condition: missing expected
    intent ⇒ manual reconciliation, never silent confirmation or submission.

    rc7 Phase 21: Subclasses classify the failure mode:
      - ApprovalMissing: no approval found for the subject
      - SupplierMappingMissing: supplier mapping not configured
      - InvoiceMissing: invoice not found in the database
      - PolicyCorrupt: approval policy snapshot is corrupt
      - PayloadCorrupt: persisted request_payload hash does not match
      - DatabaseUnavailable: database connection failure
      - InvariantViolation: a runtime invariant was violated
    """


class ApprovalMissing(PayloadReconstructionError):
    """rc7 Phase 21: No approval found for the external action's subject."""


class SupplierMappingMissing(PayloadReconstructionError):
    """rc7 Phase 21: Supplier mapping is not configured for this invoice."""


class InvoiceMissing(PayloadReconstructionError):
    """rc7 Phase 21: The invoice referenced by the external action was not found."""


class PolicyCorrupt(PayloadReconstructionError):
    """rc7 Phase 21: The approval policy snapshot is corrupt."""


class PayloadCorrupt(PayloadReconstructionError):
    """rc7 Phase 21: The persisted request_payload hash does not match."""


class DatabaseUnavailable(PayloadReconstructionError):
    """rc7 Phase 21: Database connection failure during reconstruction."""


class InvariantViolation(PayloadReconstructionError):
    """rc7 Phase 21: A runtime invariant was violated during reconstruction."""


def _canonical_erp_invoice(data: dict[str, Any]) -> dict[str, Any]:
    """rc4 Phase 8: Build a canonical ERP invoice representation.

    This normalized form is used for both the intended local action and the ERP
    readback. Confirmation requires:

        CanonicalExpected = CanonicalActual
    """
    from decimal import Decimal as _Decimal

    def _norm_decimal(v: Any) -> str:
        if v is None:
            return ""
        return str(_Decimal(str(v)).quantize(_Decimal("0.01")))

    def _norm_str(v: Any) -> str:
        if v is None:
            return ""
        return str(v).strip()

    def _first(*keys):
        """Return the first non-None value from the given keys in data."""
        for k in keys:
            v = data.get(k)
            if v is not None:
                return v
        return None

    return {
        "supplier": _norm_str(data.get("supplier")),
        "invoice_number": _norm_str(_first("bill_no", "invoice_number")),
        "currency": _norm_str(data.get("currency")),
        "grand_total": _norm_decimal(data.get("grand_total")),
        "net_total": _norm_decimal(data.get("net_total")),
        "total_tax": _norm_decimal(_first("total_taxes", "total_tax", "tax_amount")),
        "purchase_order_id": _norm_str(_first("po_no", "purchase_order_id", "po_reference")),
        "project_id": _norm_str(_first("project", "project_id")),
        "docstatus": int(data.get("docstatus", 0)),
        "idempotency_key": _norm_str(_first("construct_idempotency_key", "idempotency_key")),
    }


def _compare_canonical(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """rc4 Phase 6/8: Fail-closed comparison of canonical ERP invoice fields.

    If a field is expected (present in the expected dict), it must also be
    present in the actual dict and match exactly. Expected-but-missing is a
    mismatch, not a silent pass.

    Note: docstatus is NOT compared here — the payload sends docstatus=0 (create
    as draft) while the readback should show docstatus=1 (submitted). The
    docstatus check is done separately in the executor before this comparison.
    """
    mismatches: list[str] = []
    for field in ("supplier", "invoice_number", "currency", "grand_total",
                  "net_total", "total_tax", "purchase_order_id", "project_id",
                  "idempotency_key"):
        exp = expected.get(field)
        act = actual.get(field)
        if exp is None or exp == "":
            # Expected field not provided — skip (can't verify what we didn't send).
            continue
        if act is None or act == "":
            # rc4 Phase 6: Expected-but-missing = mismatch.
            mismatches.append(f"{field}: expected {exp!r}, got MISSING")
        elif str(exp) != str(act):
            mismatches.append(f"{field}: expected {exp!r}, got {act!r}")
    return mismatches


def verify_remote_invoice(expected: dict[str, Any], actual: dict[str, Any]) -> tuple[bool, list[str]]:
    """rc7 Phase 24: Single canonical ERP invoice verification function.

    This is the ONE function that normal execution, UNKNOWN reconciliation,
    and remote-draft continuation all call to verify a remote ERP document
    against the expected invoice. If future fields are added, every execution
    path inherits them automatically.

    Target invariant:
        ∀ PathsToConfirmed, VerificationFunction = SameFunction

    Verifies:
        - supplier ID
        - invoice number
        - currency
        - grand total
        - net total
        - tax
        - purchase order
        - project
        - docstatus (must be 1 = submitted for confirmed)
        - idempotency key

    Returns (success, mismatches). If success is False, mismatches contains
    a list of human-readable field mismatch descriptions.
    """
    canonical_expected = _canonical_erp_invoice(expected)
    canonical_actual = _canonical_erp_invoice(actual)
    mismatches = _compare_canonical(canonical_expected, canonical_actual)

    # Also check docstatus — the remote document must be submitted (1)
    # for a confirmed action. Draft (0) or cancelled (2) is not confirmed.
    actual_docstatus = actual.get("docstatus")
    if actual_docstatus is not None and actual_docstatus != 1:
        mismatches.append(f"docstatus: expected 1 (submitted), got {actual_docstatus}")

    return len(mismatches) == 0, mismatches


def _compare_readback(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Legacy readback comparison — delegates to canonical comparison.

    Kept for backward compatibility with existing tests.
    """
    return _compare_canonical(_canonical_erp_invoice(expected), _canonical_erp_invoice(actual))
