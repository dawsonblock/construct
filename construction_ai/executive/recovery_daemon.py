"""rc4/rc5 — External-effect recovery daemon.

A dedicated recovery service responsible for:
- Expired EXECUTING leases → UNKNOWN (via reaper)
- UNKNOWN actions → bounded reconciliation with full financial readback verification
- REMOTE_DRAFT actions → deterministic verification and submission resumption
- CONFIRMED actions missing final audit → repair audit
- Stale external-action attempts

All operations are idempotent. The daemon can be run repeatedly without side
effects — it only transitions actions that need recovery.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote
from uuid import UUID

from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories import Repositories


@dataclass(frozen=True)
class RecoveryResult:
    """Result of one recovery daemon cycle."""
    reaped_leases: list[str] = field(default_factory=list)  # action_ids reaped
    reconciled_unknowns: list[str] = field(default_factory=list)
    resolved_drafts: list[str] = field(default_factory=list)
    repaired_audits: list[str] = field(default_factory=list)
    flagged_ambiguous: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def run_recovery_cycle(
    repos: Repositories,
    *,
    scope: Scope,
    erp_read_transport=None,
    erp_write_transport=None,
    lease_duration_seconds: int = 120,
    negative_confirmation_threshold: int = 2,
    worker_id: str = "recovery_daemon",
) -> RecoveryResult:
    """Run one complete recovery cycle.

    This is safe to call repeatedly — each step is idempotent.

    Args:
        repos: Repository bundle.
        scope: Organization scope.
        erp_read_transport: Read transport for ERP queries (needed for reconciliation).
        erp_write_transport: Optional write transport for resuming draft submissions.
        lease_duration_seconds: Lease duration for new acquisitions.
        negative_confirmation_threshold: Observation cycles required before PROVEN_ABSENT.
        worker_id: Identifier of the recovery worker.

    Returns:
        RecoveryResult with counts and action IDs for each step.
    """
    org_scope = scope.organization_only
    result = RecoveryResult()

    # 1. Reap expired EXECUTING leases.
    try:
        reaped = repos.external_actions.reap_expired(scope=org_scope)
        for action in reaped:
            result.reaped_leases.append(str(action.action_id))
            # Audit the reaping.
            repos.audit.append(
                scope=org_scope,
                event_type="EXTERNAL_ACTION_LEASE_REAPED",
                actor="recovery_daemon",
                object_type="external_action",
                object_id=action.action_id,
                payload={
                    "action_id": str(action.action_id),
                    "from_status": "executing",
                    "to_status": "unknown",
                    "reason": "lease expired",
                    "execution_owner": action.execution_owner,
                },
            )
    except Exception as e:
        result.errors.append(f"reap_expired: {e}")

    # 2. Reconcile UNKNOWN actions (requires ERP transport).
    if erp_read_transport is not None:
        try:
            unknowns = _find_unknown_actions(repos, org_scope)
            for action in unknowns:
                try:
                    outcome, expected_payload = _reconcile_one(
                        repos, org_scope, action, erp_read_transport,
                        negative_confirmation_threshold=negative_confirmation_threshold,
                    )
                    result.reconciled_unknowns.append(str(action["action_id"]))
                    if outcome.classification in ("remote_mismatch", "ambiguous"):
                        result.flagged_ambiguous.append(str(action["action_id"]))
                    elif outcome.classification == "remote_draft" and erp_write_transport is not None:
                        # rc5 Phase 3: Resume submission of verified matching draft.
                        resolved = _resolve_remote_draft(
                            repos, org_scope, action, outcome, expected_payload,
                            erp_read_transport, erp_write_transport, worker_id,
                        )
                        if resolved:
                            result.resolved_drafts.append(str(action["action_id"]))
                        else:
                            result.flagged_ambiguous.append(str(action["action_id"]))
                except Exception as e:
                    result.errors.append(f"reconcile {action.get('action_id')}: {e}")
        except Exception as e:
            result.errors.append(f"find_unknowns: {e}")

    # 3. Repair CONFIRMED actions missing final audit.
    try:
        missing_audit = repos.external_actions.find_missing_final_audit(scope=org_scope)
        for action in missing_audit:
            try:
                _repair_final_audit(repos, org_scope, action)
                result.repaired_audits.append(str(action.action_id))
            except Exception as e:
                result.errors.append(f"repair_audit {action.action_id}: {e}")
    except Exception as e:
        result.errors.append(f"find_missing_audit: {e}")

    return result


def _find_unknown_actions(repos: Repositories, scope: Scope) -> list:
    """Find all UNKNOWN external actions that need reconciliation."""
    with repos.db.scoped(scope) as cur:
        cur.execute(
            """SELECT action_id, organization_id, remote_document_id, erp_idempotency_key,
                      subject_id, subject_type, request_payload, remote_state, recovery_attempts
               FROM external_actions
               WHERE organization_id = %s AND status = 'unknown'
               ORDER BY created_at
               LIMIT 100""",
            (scope.organization_id,),
        )
        rows = cur.fetchall()
        columns = [c.name for c in cur.description]
        return [dict(zip(columns, row, strict=True)) for row in rows]


def _reconcile_one(
    repos: Repositories,
    scope: Scope,
    action_row: dict[str, Any],
    erp_read_transport,
    *,
    negative_confirmation_threshold: int = 2,
):
    """Reconcile a single UNKNOWN action with authoritative payload and supplier resolution."""
    from construction_ai.executive.executor import reconstruct_expected_erp_payload
    from construction_ai.executive.reconcile_unknown import reconcile_external_action

    action_id = action_row["action_id"]
    if isinstance(action_id, str):
        action_id = UUID(action_id)

    # rc5 Phase 1: Reconstruct authoritative expected payload.
    expected_payload = action_row.get("request_payload")
    if expected_payload is None:
        expected_payload = reconstruct_expected_erp_payload(repos, scope=scope, action=action_row)

    # rc5 Phase 4/8: Use authoritative supplier and invoice number from expected payload.
    invoice_number = None
    supplier = None
    if expected_payload:
        supplier = expected_payload.get("supplier")
        invoice_number = expected_payload.get("bill_no")
    elif action_row.get("subject_id"):
        subject_id = action_row["subject_id"]
        if isinstance(subject_id, str):
            subject_id = UUID(subject_id)
        invoice = repos.invoices.get(scope=scope, invoice_id=subject_id)
        if invoice:
            invoice_number = invoice.invoice_number
            supplier = invoice.vendor_name
            if invoice.vendor_company_id:
                company = repos.companies.get(scope=scope, company_id=UUID(str(invoice.vendor_company_id)))
                if company and company.erp_supplier_id:
                    supplier = company.erp_supplier_id

    outcome = reconcile_external_action(
        repos,
        scope=scope,
        action_id=action_id,
        erp_read_transport=erp_read_transport,
        invoice_number=invoice_number,
        supplier=supplier,
        erp_idempotency_key=action_row.get("erp_idempotency_key"),
        expected_payload=expected_payload,
        negative_confirmation_threshold=negative_confirmation_threshold,
    )
    return outcome, expected_payload


def _resolve_remote_draft(
    repos: Repositories,
    scope: Scope,
    action_row: dict[str, Any],
    outcome: Any,
    expected_payload: dict[str, Any] | None,
    erp_read_transport,
    erp_write_transport,
    worker_id: str,
) -> bool:
    """rc5 Phase 3: Deterministic remote draft recovery workflow.

    1. Verify exact draft contents against approved expected intent.
    2. Submit the existing draft via ERP write transport.
    3. Read back and verify submitted state.
    4. Transition to CONFIRMED and append audit.
    """
    from construction_ai.executive.executor import (
        _canonical_erp_invoice, _compare_canonical, _hash_json,
    )

    action_id = action_row["action_id"]
    if isinstance(action_id, str):
        action_id = UUID(action_id)
    docname = outcome.remote_document_id
    if not docname:
        return False

    # 1. Read current draft from ERP.
    read_resp = erp_read_transport.get(f"/api/resource/{quote('Purchase Invoice')}/{quote(docname)}")
    draft_data = read_resp.get("data", read_resp) if isinstance(read_resp, dict) else {}
    if not draft_data:
        return False

    # 2. Canonical compare draft fields against approved expected payload.
    if expected_payload:
        canonical_expected = _canonical_erp_invoice(expected_payload)
        canonical_actual = _canonical_erp_invoice(draft_data)
        mismatches = _compare_canonical(canonical_expected, canonical_actual)
        if mismatches:
            repos.external_actions.transition(
                scope=scope,
                action_id=action_id,
                from_status="unknown",
                to_status="failed_terminal",
                remote_document_id=docname,
                remote_state="remote_mismatch",
                last_error=f"recovery: draft mismatch with approved intent: {mismatches}",
            )
            repos.audit.append(
                scope=scope,
                event_type="EXTERNAL_ACTION_DRAFT_MISMATCH",
                actor="recovery_daemon",
                object_type="external_action",
                object_id=action_id,
                payload={"action_id": str(action_id), "docname": docname, "mismatches": mismatches},
            )
            return False

    # 3. Submit the existing draft.
    try:
        if hasattr(erp_write_transport, "submit_purchase_invoice"):
            erp_write_transport.submit_purchase_invoice(docname)
        elif hasattr(erp_write_transport, "post"):
            erp_write_transport.post(
                "/api/method/frappe.client.submit",
                json={"doc": {"doctype": "Purchase Invoice", "name": docname}},
            )
        else:
            return False
    except Exception as e:
        repos.external_actions.transition(
            scope=scope,
            action_id=action_id,
            from_status="unknown",
            to_status="unknown",
            remote_document_id=docname,
            remote_state="remote_draft",
            last_error=f"recovery: failed to submit draft {docname}: {e}",
        )
        return False

    # 4. Read back the submitted document.
    readback = erp_read_transport.get(f"/api/resource/{quote('Purchase Invoice')}/{quote(docname)}")
    readback_data = readback.get("data", readback) if isinstance(readback, dict) else {}
    docstatus = readback_data.get("docstatus", 0)
    if docstatus != 1:
        repos.external_actions.transition(
            scope=scope,
            action_id=action_id,
            from_status="unknown",
            to_status="unknown",
            remote_document_id=docname,
            remote_state="remote_draft" if docstatus == 0 else "remote_unknown",
            last_error=f"recovery: post-submit docstatus={docstatus}, expected 1",
        )
        return False

    # 5. Canonical comparison on submitted readback.
    if expected_payload:
        canonical_expected = _canonical_erp_invoice(expected_payload)
        canonical_actual = _canonical_erp_invoice(readback_data)
        mismatches = _compare_canonical(canonical_expected, canonical_actual)
        if mismatches:
            repos.external_actions.transition(
                scope=scope,
                action_id=action_id,
                from_status="unknown",
                to_status="failed_terminal",
                remote_document_id=docname,
                remote_state="remote_mismatch",
                last_error=f"recovery: post-submit readback mismatch: {mismatches}",
            )
            return False

    readback_hash = _hash_json(_canonical_erp_invoice(readback_data))
    result_payload = {"docname": docname, "docstatus": docstatus}

    # 6. Transition to CONFIRMED.
    confirmed = repos.external_actions.transition(
        scope=scope,
        action_id=action_id,
        from_status="unknown",
        to_status="confirmed",
        remote_document_id=docname,
        remote_state="remote_submitted",
        result=result_payload,
        readback_hash=readback_hash,
    )
    if confirmed is None:
        return False

    # 7. Append audit event.
    subject_id = action_row.get("subject_id")
    audit_event = repos.audit.append(
        scope=scope,
        event_type="ERP_INVOICE_SUBMITTED",
        actor="recovery_daemon",
        object_type="invoice" if action_row.get("subject_type") == "invoice" else "external_action",
        object_id=subject_id or action_id,
        payload={
            "action_id": str(action_id),
            "erp_docname": docname,
            "erp_docstatus": 1,
            "resumed_draft": True,
            "readback_hash": readback_hash,
        },
    )
    if audit_event.audit_event_id is not None:
        repos.external_actions.transition(
            scope=scope,
            action_id=action_id,
            from_status="confirmed",
            to_status="confirmed",
            final_audit_event_id=audit_event.audit_event_id,
        )

    return True


def _repair_final_audit(repos: Repositories, scope: Scope, action) -> None:
    """rc4 Phase 16: Repair a missing final audit event for a CONFIRMED action.

    Appends a deterministic repair audit event and links it to the action.
    This is idempotent — if the action already has a final_audit_event_id, this
    is a no-op.
    """
    if action.final_audit_event_id is not None:
        return  # Already repaired.

    result = action.result or {}
    audit_event = repos.audit.append(
        scope=scope,
        event_type="ERP_INVOICE_SUBMITTED_REPAIR",
        actor="recovery_daemon",
        object_type="external_action",
        object_id=action.action_id,
        payload={
            "action_id": str(action.action_id),
            "erp_docname": result.get("docname", ""),
            "erp_docstatus": result.get("docstatus", 0),
            "repair": True,
            "reason": "final audit event was missing — repaired by recovery daemon",
        },
    )

    if audit_event.audit_event_id is not None:
        repos.external_actions.transition(
            scope=scope,
            action_id=action.action_id,
            from_status="confirmed",
            to_status="confirmed",
            final_audit_event_id=audit_event.audit_event_id,
        )
