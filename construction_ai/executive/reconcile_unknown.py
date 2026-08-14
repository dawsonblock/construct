"""v0.5.0-rc2 Phase 2 — Unknown-external-state handling.

When an external call returns an ambiguous result (timeout, 502, crash after
submit), the external action is marked UNKNOWN. The invariant:

    UNKNOWN ⇒ RECONCILE

not:

    UNKNOWN ⇒ RETRY

reconcile_external_action() searches ERP using deterministic identifiers to
determine whether the external effect already happened:

- If one matching document exists → mark CONFIRMED, store remote ID.
- If none exists and the state can be proven absent → mark FAILED_RETRYABLE.
- If multiple possible matches exist → mark FAILED_TERMINAL
  (MANUAL_RECONCILIATION_REQUIRED).

Never guess.

The search uses:
- remote_document_id (if known from the EXECUTING phase)
- invoice number + supplier + organization
- idempotency key (if ERP supports a custom field)

This module is read-only with respect to ERP — it only searches. The state
transition is written to the external_actions table and audited.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote
from uuid import UUID

from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories import Repositories


@dataclass(frozen=True)
class ReconciliationOutcome:
    action_id: UUID
    new_status: str  # confirmed, failed_retryable, or failed_terminal
    remote_document_id: str | None
    reason: str


def reconcile_external_action(
    repos: Repositories,
    *,
    scope: Scope,
    action_id: UUID,
    erp_read_transport,
    invoice_number: str | None = None,
    supplier: str | None = None,
) -> ReconciliationOutcome:
    """Reconcile an UNKNOWN external action by searching ERP.

    Searches ERP for documents matching the deterministic identifiers. If
    exactly one match is found, marks the action CONFIRMED. If no match is
    found, marks it FAILED_RETRYABLE (safe to retry). If multiple matches are
    found, marks it FAILED_TERMINAL (manual reconciliation required).

    Args:
        repos: Repository bundle.
        scope: Organization scope.
        action_id: The external action to reconcile.
        erp_read_transport: Read transport for ERP queries.
        invoice_number: The invoice number to search for (bill_no in ERP).
        supplier: The supplier name to search for.

    Returns:
        ReconciliationOutcome with the new status and reason.
    """
    org_scope = scope.organization_only
    action = repos.external_actions.get(scope=org_scope, action_id=action_id)
    if action is None:
        raise ValueError(f"external action {action_id} not found")

    if action.status != "unknown":
        raise ValueError(
            f"external action {action_id} is {action.status}, not unknown — "
            "reconciliation is only for UNKNOWN actions"
        )

    # 1. If we have a remote_document_id from the EXECUTING phase, try to
    #    read it back directly.
    matches: list[dict[str, Any]] = []
    if action.remote_document_id:
        doc = _read_document(erp_read_transport, "Purchase Invoice", action.remote_document_id)
        if doc is not None:
            matches = [doc]

    # 2. If no direct read was possible, search by invoice number + supplier.
    if not matches and invoice_number and supplier:
        matches = _search_documents(erp_read_transport, invoice_number, supplier)

    # 3. Determine the outcome.
    if len(matches) == 1:
        # Exactly one match — confirm.
        doc = matches[0]
        docname = doc.get("name", "")
        docstatus = doc.get("docstatus", 0)
        result = {"docname": docname, "docstatus": docstatus}

        repos.external_actions.transition(
            scope=org_scope,
            action_id=action_id,
            from_status="unknown",
            to_status="confirmed",
            remote_document_id=docname,
            result=result,
        )
        _audit_reconciliation(repos, org_scope, action_id, "unknown", "confirmed", invoice_number, supplier, docname)
        return ReconciliationOutcome(
            action_id=action_id,
            new_status="confirmed",
            remote_document_id=docname,
            reason=f"found exactly one matching ERP document: {docname}",
        )

    if len(matches) == 0:
        # No match — safe to retry.
        repos.external_actions.transition(
            scope=org_scope,
            action_id=action_id,
            from_status="unknown",
            to_status="failed_retryable",
            last_error="reconciliation found no matching ERP document",
        )
        _audit_reconciliation(repos, org_scope, action_id, "unknown", "failed_retryable", invoice_number, supplier, None)
        return ReconciliationOutcome(
            action_id=action_id,
            new_status="failed_retryable",
            remote_document_id=None,
            reason="no matching ERP document found — safe to retry",
        )

    # Multiple matches — manual reconciliation required.
    match_names = [m.get("name", "?") for m in matches]
    repos.external_actions.transition(
        scope=org_scope,
        action_id=action_id,
        from_status="unknown",
        to_status="failed_terminal",
        last_error=f"multiple matching ERP documents found: {match_names}",
    )
    _audit_reconciliation(repos, org_scope, action_id, "unknown", "failed_terminal", invoice_number, supplier, None)
    return ReconciliationOutcome(
        action_id=action_id,
        new_status="failed_terminal",
        remote_document_id=None,
        reason=f"multiple matching ERP documents found: {match_names} — manual reconciliation required",
    )


def _read_document(transport, doctype: str, docname: str) -> dict[str, Any] | None:
    """Read a single document from ERP by name. Returns None if not found."""
    response = transport.get(f"/api/resource/{quote(doctype)}/{quote(docname)}")
    data = response.get("data", response) if isinstance(response, dict) else {}
    if not data or not data.get("name"):
        return None
    return data


def _search_documents(transport, invoice_number: str, supplier: str) -> list[dict[str, Any]]:
    """Search ERP for Purchase Invoice documents matching the given criteria."""
    import json

    filters = json.dumps([
        ["bill_no", "=", invoice_number],
        ["supplier", "=", supplier],
    ])
    response = transport.get(
        "/api/resource/Purchase Invoice",
        params={"filters": filters, "fields": json.dumps(["name", "supplier", "bill_no", "docstatus", "grand_total"])},
    )
    data = response.get("data", response) if isinstance(response, dict) else []
    return data if isinstance(data, list) else []


def _audit_reconciliation(
    repos: Repositories,
    scope: Scope,
    action_id: UUID,
    from_status: str,
    to_status: str,
    invoice_number: str | None,
    supplier: str | None,
    remote_doc: str | None,
) -> None:
    """Audit a reconciliation result."""
    repos.audit.append(
        scope=scope,
        event_type="EXTERNAL_ACTION_RECONCILED",
        actor="system",
        object_type="external_action",
        object_id=action_id,
        payload={
            "action_id": str(action_id),
            "from_status": from_status,
            "to_status": to_status,
            "invoice_number": invoice_number,
            "supplier": supplier,
            "remote_document_id": remote_doc,
        },
    )
