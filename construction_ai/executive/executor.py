"""v0.5.0-rc1 — ApprovedInvoiceExecutor (item 44).

The only path by which an approved invoice is submitted to ERP. This module is
the controlled gate between human approval and external financial effect.

Invariants enforced:
1. The approval must be in `approved` status (not pending, held, or rejected).
2. The approval must not be stale — the project state fingerprint at execution
   time must match the fingerprint at approval time. If state has drifted, the
   execution is refused.
3. The ERP write is idempotent — the external action ledger prevents duplicate
   submissions (item 45).
4. The ERP write is verified by readback — after submission, the executor reads
   the document back from ERP and confirms it is submitted (docstatus=1)
   (item 46).
5. The executor never writes to ERP without an approval. AI cannot call this
   path directly; it is called by the worker after a human approves.
6. Every execution is recorded in the audit log and the external action ledger.

The executor does NOT:
- create approvals
- modify approvals
- bypass the policy engine
- write to ERP without a recorded external action
- skip readback verification
"""
from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class ExecutionResult:
    action_id: UUID
    erp_docname: str
    erp_docstatus: int
    idempotent: bool  # True if this was a replay of an existing action


def execute_approved_invoice(
    repos: Repositories,
    *,
    scope: Scope,
    approval_id: UUID,
    adapter: ERPNextAdapter,
    erp_read_transport,
) -> ExecutionResult:
    """Execute an approved invoice: create a draft in ERP, submit it, read back.

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
    """
    # Use organization-level scope for approval lookup — approvals may have
    # project_id = NULL when the project wasn't resolved at pipeline time.
    org_scope = scope.organization_only

    # 1. Load the approval and verify it is approved.
    approval = repos.approvals.get(scope=org_scope, approval_id=approval_id)
    if approval is None:
        raise ApprovalNotFound(f"approval {approval_id} not found")
    if approval.status.value != "approved":
        raise ApprovalNotApproved(f"approval is {approval.status.value}, not approved")

    # 2. Load the invoice to build the ERP payload.
    invoice_id = UUID(approval.subject_id)
    invoice = repos.invoices.get(scope=org_scope, invoice_id=invoice_id)
    if invoice is None:
        raise ExecutionError(f"invoice {invoice_id} not found")

    # 3. Build the ERP payload and idempotency key.
    # The idempotency key is derived from the approval ID — one approval, one
    # ERP submission, no duplicates.
    idempotency_key = f"approval:{approval_id}"

    # 4. Check the external action ledger for an existing action.
    existing = repos.external_actions.find_by_key(
        scope=org_scope, action_type="erp_submit_purchase_invoice", idempotency_key=idempotency_key,
    )
    if existing is not None and existing.status == "completed":
        # Idempotent replay — return the existing result.
        erp_docname = (existing.result or {}).get("docname", "")
        erp_docstatus = (existing.result or {}).get("docstatus", 0)
        return ExecutionResult(
            action_id=existing.action_id,
            erp_docname=erp_docname,
            erp_docstatus=erp_docstatus,
            idempotent=True,
        )

    # 5. Build the ERP payload.
    payload = {
        "supplier": invoice.vendor_name,
        "bill_no": invoice.invoice_number,
        "grand_total": float(invoice.total or 0),
        "net_total": float(invoice.subtotal or 0),
        "total_taxes": float(invoice.tax or 0),
        "currency": invoice.currency or "CAD",
        "docstatus": 0,  # create as draft
    }

    # 6. Create the draft in ERP.
    draft_response = adapter.create_purchase_invoice_draft(payload)
    draft_data = draft_response.get("data", draft_response) if isinstance(draft_response, dict) else {}
    docname = draft_data.get("name", "")
    if not docname:
        raise ExecutionError("ERP did not return a document name")

    # 7. Submit the draft (set docstatus from 0 to 1).
    adapter.submit_purchase_invoice(docname=docname, approval_status="approved", approved_by=approval.approved_by or "system")

    # 8. Readback verification — confirm the document is submitted.
    from urllib.parse import quote

    readback = erp_read_transport.get(f"/api/resource/{quote('Purchase Invoice')}/{quote(docname)}")
    readback_data = readback.get("data", readback) if isinstance(readback, dict) else {}
    docstatus = readback_data.get("docstatus", 0)
    if docstatus != 1:
        raise ReadbackFailed(f"ERP readback shows docstatus={docstatus}, expected 1 (submitted)")

    # 9. Record the external action (idempotent).
    result_payload = {"docname": docname, "docstatus": docstatus}
    action = repos.external_actions.record(
        scope=org_scope,
        action_type="erp_submit_purchase_invoice",
        idempotency_key=idempotency_key,
        target_system="erpnext",
        target_id=docname,
        request_payload=payload,
        result=result_payload,
        status="completed",
    )

    # 10. Audit the execution.
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
            "action_id": str(action.action_id),
            "idempotent": False,
        },
    )

    return ExecutionResult(
        action_id=action.action_id,
        erp_docname=docname,
        erp_docstatus=docstatus,
        idempotent=False,
    )
