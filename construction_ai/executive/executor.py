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


class ExecutionError(Exception):
    """A controlled failure that prevents the execution."""


class ApprovalNotApproved(ExecutionError):
    pass


class ApprovalStale(ExecutionError):
    pass


class ApprovalNotFound(ExecutionError):
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
    """Verify the approval is not stale (item 48).

    Reconstructs the current project state and compares its fingerprint to the
    state_fingerprint recorded at approval time. If they differ, the state has
    drifted and the approval is stale — the human approved a different state
    than the one we'd execute against.

    Raises ApprovalStale if the state has drifted.
    Does nothing if state_fingerprint is None (approval predates fingerprinting
    or project_id was NULL).
    """
    if not approval.state_fingerprint:
        return  # no fingerprint to check against

    if not approval.project_id:
        return  # no project to reconstruct

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
    org_scope = scope.organization_only
    operation = "erp_submit_purchase_invoice"

    # 1. Load the approval and verify it is approved.
    approval = repos.approvals.get(scope=org_scope, approval_id=approval_id)
    if approval is None:
        raise ApprovalNotFound(f"approval {approval_id} not found")
    if approval.status.value != "approved":
        raise ApprovalNotApproved(f"approval is {approval.status.value}, not approved")

    # 2. Check staleness.
    check_approval_staleness(repos, scope=org_scope, approval=approval)

    # 3. Load the invoice to build the ERP payload.
    invoice_id = UUID(approval.subject_id)
    invoice = repos.invoices.get(scope=org_scope, invoice_id=invoice_id)
    if invoice is None:
        raise ExecutionError(f"invoice {invoice_id} not found")

    # 4. Build the idempotency key and ERP payload.
    # Phase 14: Keep money as Decimal — convert to string at the serialization
    # boundary, never to float.
    from decimal import Decimal as _Decimal

    idempotency_key = f"approval:{approval_id}"
    payload = {
        "supplier": invoice.vendor_name,
        "bill_no": invoice.invoice_number,
        "grand_total": str(_Decimal(str(invoice.total or 0))),
        "net_total": str(_Decimal(str(invoice.subtotal or 0))),
        "total_taxes": str(_Decimal(str(invoice.tax or 0))),
        "currency": invoice.currency or "CAD",
        "docstatus": 0,  # create as draft
    }

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
        # Another worker may be running. In a full implementation, we'd check
        # the lease timestamp. For now, refuse to avoid duplicate submission.
        raise ExternalActionInProgress(
            f"external action {action.action_id} is in EXECUTING state"
        )

    if action.status == "unknown":
        # Phase 2: reconcile before retry. For now, refuse — the caller must
        # run reconcile_external_action() to resolve the state.
        raise ExecutionError(
            f"external action {action.action_id} is in UNKNOWN state — reconciliation required"
        )

    # Status is PENDING or FAILED_RETRYABLE — proceed to execute.

    # 7. Transition to EXECUTING.
    updated = repos.external_actions.transition(
        scope=org_scope,
        action_id=action.action_id,
        from_status=action.status,
        to_status="executing",
    )
    if updated is None:
        # Lost the race — another worker transitioned it.
        raise ExternalActionInProgress(
            f"could not transition {action.action_id} from {action.status} to executing"
        )

    _audit_transition(repos, org_scope, action.action_id, action.status, "executing", invoice_id)

    _check_crash("before_erp_create")

    # 8. Create the draft in ERP.
    try:
        draft_response = adapter.create_purchase_invoice_draft(payload)
    except Exception as e:
        _mark_failed_retryable(repos, org_scope, updated.action_id, str(e), invoice_id)
        raise

    draft_data = draft_response.get("data", draft_response) if isinstance(draft_response, dict) else {}
    docname = draft_data.get("name", "")
    if not docname:
        _mark_failed_retryable(repos, org_scope, updated.action_id, "ERP did not return a document name", invoice_id)
        raise ExecutionError("ERP did not return a document name")

    _check_crash("after_erp_create")

    # 9. Submit the draft.
    _check_crash("before_erp_submit")
    try:
        adapter.submit_purchase_invoice(docname)
    except Exception as e:
        # After ERP create, a submit failure is potentially ambiguous — the
        # document may exist as a draft. Mark as UNKNOWN for reconciliation.
        _mark_unknown(repos, org_scope, updated.action_id, f"submit failed: {e}", docname, invoice_id)
        raise

    _check_crash("after_erp_submit")

    # 10. Readback verification — compare all financial fields (Phase 11).
    _check_crash("before_readback")
    from urllib.parse import quote
    from decimal import Decimal as _Decimal

    readback = erp_read_transport.get(f"/api/resource/{quote('Purchase Invoice')}/{quote(docname)}")
    readback_data = readback.get("data", readback) if isinstance(readback, dict) else {}
    docstatus = readback_data.get("docstatus", 0)
    if docstatus != 1:
        _mark_unknown(repos, org_scope, updated.action_id, f"readback docstatus={docstatus}, expected 1", docname, invoice_id)
        raise ReadbackFailed(f"ERP readback shows docstatus={docstatus}, expected 1 (submitted)")

    # Compare financial fields: supplier, bill_no, currency, totals.
    mismatches = _compare_readback(payload, readback_data)
    if mismatches:
        _mark_unknown(
            repos, org_scope, updated.action_id,
            f"readback mismatch: {mismatches}", docname, invoice_id,
        )
        raise ReadbackMismatch(f"ERP readback fields do not match: {mismatches}")

    _check_crash("after_readback")

    # 11. Transition to CONFIRMED.
    result_payload = {"docname": docname, "docstatus": docstatus}
    _check_crash("before_confirmed")
    confirmed = repos.external_actions.transition(
        scope=org_scope,
        action_id=updated.action_id,
        from_status="executing",
        to_status="confirmed",
        remote_document_id=docname,
        result=result_payload,
    )
    if confirmed is None:
        # Lost the race or state changed unexpectedly.
        raise ExecutionError(f"could not transition {updated.action_id} from executing to confirmed")

    _audit_transition(repos, org_scope, updated.action_id, "executing", "confirmed", invoice_id)

    # 12. Audit the execution.
    _check_crash("before_audit")
    repos.audit.append(
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
        },
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


def _mark_unknown(repos: Repositories, scope: Scope, action_id: UUID, error: str, docname: str, invoice_id: UUID) -> None:
    """Transition an action to UNKNOWN and audit.

    Used when the external call returned an ambiguous result (e.g. submit
    failed after draft creation, or readback didn't confirm). The action
    requires reconciliation before retry.
    """
    repos.external_actions.transition(
        scope=scope, action_id=action_id, from_status="executing", to_status="unknown",
        remote_document_id=docname, last_error=error,
    )
    _audit_transition(repos, scope, action_id, "executing", "unknown", invoice_id)


def _audit_transition(repos: Repositories, scope: Scope, action_id: UUID, from_status: str, to_status: str, invoice_id: UUID) -> None:
    """Audit an external-action state transition (Phase 23)."""
    repos.audit.append(
        scope=scope,
        event_type="EXTERNAL_ACTION_TRANSITION",
        actor="system",
        object_type="external_action",
        object_id=action_id,
        payload={
            "action_id": str(action_id),
            "from_status": from_status,
            "to_status": to_status,
            "subject_type": "invoice",
            "subject_id": str(invoice_id),
        },
    )


def _compare_readback(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Compare expected ERP payload fields against readback data (Phase 11).

    Returns a list of mismatch descriptions. Empty list means all fields match.
    Compares: supplier, bill_no, currency, grand_total, net_total, total_taxes.
    """
    from decimal import Decimal as _Decimal

    mismatches: list[str] = []

    # String fields — exact match.
    for field in ("supplier", "bill_no", "currency"):
        exp = expected.get(field)
        act = actual.get(field)
        if exp is not None and act is not None and str(exp) != str(act):
            mismatches.append(f"{field}: expected {exp!r}, got {act!r}")

    # Numeric fields — compare as Decimal for precision.
    for field in ("grand_total", "net_total", "total_taxes"):
        exp = expected.get(field)
        act = actual.get(field)
        if exp is not None and act is not None:
            try:
                exp_d = _Decimal(str(exp))
                act_d = _Decimal(str(act))
                if exp_d != act_d:
                    mismatches.append(f"{field}: expected {exp}, got {act}")
            except Exception:
                mismatches.append(f"{field}: could not compare {exp!r} vs {act!r}")

    return mismatches
