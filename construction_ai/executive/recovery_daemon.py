"""rc4 Phase 17 — External-effect recovery daemon.

A dedicated recovery service responsible for:
- Expired EXECUTING leases → UNKNOWN (via reaper)
- UNKNOWN actions → reconciliation
- CONFIRMED actions missing final audit → repair audit
- Stale external-action attempts

All operations are idempotent. The daemon can be run repeatedly without side
effects — it only transitions actions that need recovery.

Conceptual loop:

    reap expired executions
    reconcile unknowns
    repair missing final audits
    flag ambiguous actions
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
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
    lease_duration_seconds: int = 120,
) -> RecoveryResult:
    """Run one complete recovery cycle.

    This is safe to call repeatedly — each step is idempotent.

    Args:
        repos: Repository bundle.
        scope: Organization scope.
        erp_read_transport: Read transport for ERP queries (needed for reconciliation).
        lease_duration_seconds: Lease duration for new acquisitions.

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
                    outcome = _reconcile_one(repos, org_scope, action, erp_read_transport)
                    result.reconciled_unknowns.append(str(action.action_id))
                    if outcome.classification in ("remote_mismatch", "ambiguous"):
                        result.flagged_ambiguous.append(str(action.action_id))
                except Exception as e:
                    result.errors.append(f"reconcile {action.action_id}: {e}")
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
                      subject_id, subject_type
               FROM external_actions
               WHERE organization_id = %s AND status = 'unknown'
               ORDER BY created_at
               LIMIT 100""",
            (scope.organization_id,),
        )
        rows = cur.fetchall()
        columns = [c.name for c in cur.description]
        return [dict(zip(columns, row, strict=True)) for row in rows]


def _reconcile_one(repos: Repositories, scope: Scope, action_row: dict[str, Any], erp_read_transport):
    """Reconcile a single UNKNOWN action."""
    from construction_ai.executive.reconcile_unknown import reconcile_external_action

    action_id = action_row["action_id"]
    if isinstance(action_id, str):
        action_id = UUID(action_id)

    # Look up the invoice to get invoice_number and supplier for the search.
    invoice_number = None
    supplier = None
    if action_row.get("subject_id"):
        subject_id = action_row["subject_id"]
        if isinstance(subject_id, str):
            subject_id = UUID(subject_id)
        invoice = repos.invoices.get(scope=scope, invoice_id=subject_id)
        if invoice:
            invoice_number = invoice.invoice_number
            supplier = invoice.vendor_name

    return reconcile_external_action(
        repos,
        scope=scope,
        action_id=action_id,
        erp_read_transport=erp_read_transport,
        invoice_number=invoice_number,
        supplier=supplier,
        erp_idempotency_key=action_row.get("erp_idempotency_key"),
    )


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
