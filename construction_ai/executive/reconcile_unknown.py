"""rc4 Phase 4 — Bounded UNKNOWN-state reconciliation.

When an external call returns an ambiguous result (timeout, 502, crash after
submit), the external action is marked UNKNOWN. The invariant:

    UNKNOWN ⇒ RECONCILE

not:

    UNKNOWN ⇒ RETRY

reconcile_external_action() searches ERP using the strongest identifier
available, in priority order:

1. remote_document_id (if known from the EXECUTING phase — direct read)
2. erp_idempotency_key (deterministic ERP-visible field)
3. exact local transaction reference (invoice number + supplier)
4. tightly constrained composite lookup (fallback only)

The result is classified as:

- PROVEN_ABSENT   — no remote document exists after exhaustive search → safe retry
- REMOTE_DRAFT    — document exists but docstatus=0 (draft, not submitted)
- REMOTE_SUBMITTED — document exists with docstatus=1 and fields match → CONFIRMED
- REMOTE_MISMATCH — document exists but fields don't match → MANUAL_RECONCILIATION
- AMBIGUOUS       — multiple documents found → MANUAL_RECONCILIATION

Only PROVEN_ABSENT may become safely retryable (FAILED_RETRYABLE).

NoSearchResult ≠ ProvenAbsent — a zero result from one query is not proof of
absence. The reconciliation must try all available identifiers before declaring
PROVEN_ABSENT.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote
from uuid import UUID

from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories import Repositories


# Reconciliation outcome classifications.
PROVEN_ABSENT = "proven_absent"
OBSERVATION_IN_PROGRESS = "observation_in_progress"
REMOTE_DRAFT = "remote_draft"
REMOTE_SUBMITTED = "remote_submitted"
REMOTE_MISMATCH = "remote_mismatch"
AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ReconciliationOutcome:
    action_id: UUID
    classification: str  # PROVEN_ABSENT, OBSERVATION_IN_PROGRESS, REMOTE_DRAFT, REMOTE_SUBMITTED, REMOTE_MISMATCH, AMBIGUOUS
    new_status: str  # confirmed, failed_retryable, failed_terminal, or unknown
    remote_document_id: str | None
    reason: str
    searched_by: list[str]  # which identifiers were tried


def reconcile_external_action(
    repos: Repositories,
    *,
    scope: Scope,
    action_id: UUID,
    erp_read_transport,
    invoice_number: str | None = None,
    supplier: str | None = None,
    erp_idempotency_key: str | None = None,
    expected_payload: dict[str, Any] | None = None,
    negative_confirmation_threshold: int = 1,
) -> ReconciliationOutcome:
    """Reconcile an UNKNOWN external action by searching ERP.

    Uses bounded reconciliation: tries each identifier in priority order, then
    classifies the result. Only PROVEN_ABSENT after meeting the negative confirmation
    observation threshold may become safely retryable.

    Args:
        repos: Repository bundle.
        scope: Organization scope.
        action_id: The external action to reconcile.
        erp_read_transport: Read transport for ERP queries.
        invoice_number: The invoice number to search for (bill_no in ERP).
        supplier: The supplier name/ID to search for.
        erp_idempotency_key: The ERP-visible idempotency key (rc4 Phase 5).
        expected_payload: The original ERP payload sent during execution, used
            to verify field match on reconciliation (rc4 Phase 8 / rc5 Phase 1).
        negative_confirmation_threshold: Number of search observation rounds required
            before declaring PROVEN_ABSENT.

    Returns:
        ReconciliationOutcome with classification and new status.
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

    # rc5 Phase 1: Reconstruct expected payload if not supplied.
    if expected_payload is None:
        from construction_ai.executive.executor import reconstruct_expected_erp_payload
        expected_payload = reconstruct_expected_erp_payload(repos, scope=org_scope, action=action)

    if expected_payload:
        erp_idempotency_key = erp_idempotency_key or expected_payload.get("construct_idempotency_key") or action.erp_idempotency_key
        invoice_number = invoice_number or expected_payload.get("bill_no")
        supplier = supplier or expected_payload.get("supplier")
    else:
        erp_idempotency_key = erp_idempotency_key or action.erp_idempotency_key

    searched_by: list[str] = []
    all_matches: list[dict[str, Any]] = []
    # Track unique documents by name to avoid duplicates from different searches.
    seen_names: set[str] = set()

    # 1. If we have a remote_document_id, try a direct read.
    if action.remote_document_id:
        searched_by.append(f"remote_document_id:{action.remote_document_id}")
        doc = _read_document(erp_read_transport, "Purchase Invoice", action.remote_document_id)
        if doc is not None:
            name = doc.get("name", "")
            if name and name not in seen_names:
                seen_names.add(name)
                all_matches.append(doc)

    # 2. If no direct read found anything, try the ERP idempotency key.
    if not all_matches and erp_idempotency_key:
        searched_by.append(f"erp_idempotency_key:{erp_idempotency_key}")
        docs = _search_by_idempotency_key(erp_read_transport, erp_idempotency_key)
        for doc in docs:
            name = doc.get("name", "")
            if name and name not in seen_names:
                seen_names.add(name)
                all_matches.append(doc)

    # 3. Try exact local reference (invoice number + supplier).
    if not all_matches and invoice_number and supplier:
        searched_by.append(f"invoice_number+supplier:{invoice_number}/{supplier}")
        docs = _search_documents(erp_read_transport, invoice_number, supplier)
        for doc in docs:
            name = doc.get("name", "")
            if name and name not in seen_names:
                seen_names.add(name)
                all_matches.append(doc)

    # 4. Classify the result.
    if len(all_matches) == 0:
        # rc5 Phase 2: Bounded negative confirmation.
        current_attempts = (getattr(action, "recovery_attempts", 0) or 0) + 1
        if current_attempts < negative_confirmation_threshold:
            _transition_to(repos, org_scope, action_id, "unknown",
                           last_error=f"reconciliation: no matching ERP document found (observation {current_attempts}/{negative_confirmation_threshold})",
                           remote_state="remote_unknown",
                           recovery_attempts=current_attempts)
            _audit_reconciliation(repos, org_scope, action_id, "unknown", "unknown",
                                  OBSERVATION_IN_PROGRESS, searched_by, None)
            return ReconciliationOutcome(
                action_id=action_id,
                classification=OBSERVATION_IN_PROGRESS,
                new_status="unknown",
                remote_document_id=None,
                reason=f"no matching ERP document found (observation {current_attempts}/{negative_confirmation_threshold}) — awaiting negative confirmation",
                searched_by=searched_by,
            )

        _transition_to(repos, org_scope, action_id, "failed_retryable",
                        last_error="reconciliation: no matching ERP document found after exhaustive search",
                        remote_state="no_remote_effect",
                        recovery_attempts=current_attempts)
        _audit_reconciliation(repos, org_scope, action_id, "unknown", "failed_retryable",
                              PROVEN_ABSENT, searched_by, None)
        return ReconciliationOutcome(
            action_id=action_id,
            classification=PROVEN_ABSENT,
            new_status="failed_retryable",
            remote_document_id=None,
            reason="no matching ERP document found after exhaustive search — safe to retry",
            searched_by=searched_by,
        )

    if len(all_matches) > 1:
        # AMBIGUOUS — manual reconciliation required.
        match_names = [m.get("name", "?") for m in all_matches]
        _transition_to(repos, org_scope, action_id, "failed_terminal",
                        last_error=f"reconciliation: multiple matching ERP documents: {match_names}",
                        remote_state="remote_unknown")
        _audit_reconciliation(repos, org_scope, action_id, "unknown", "failed_terminal",
                              AMBIGUOUS, searched_by, None)
        return ReconciliationOutcome(
            action_id=action_id,
            classification=AMBIGUOUS,
            new_status="failed_terminal",
            remote_document_id=None,
            reason=f"multiple matching ERP documents: {match_names} — manual reconciliation required",
            searched_by=searched_by,
        )

    # Exactly one match — classify by docstatus and field comparison.
    doc = all_matches[0]
    docname = doc.get("name", "")
    docstatus = doc.get("docstatus", 0)

    if docstatus == 0:
        # REMOTE_DRAFT — document exists but is not submitted.
        _transition_to(repos, org_scope, action_id, "unknown",
                        remote_document_id=docname,
                        last_error="reconciliation: remote document is a draft (docstatus=0)",
                        remote_state="remote_draft")
        _audit_reconciliation(repos, org_scope, action_id, "unknown", "unknown",
                              REMOTE_DRAFT, searched_by, docname)
        return ReconciliationOutcome(
            action_id=action_id,
            classification=REMOTE_DRAFT,
            new_status="unknown",
            remote_document_id=docname,
            reason=f"remote document {docname} is a draft (docstatus=0) — not a confirmed financial effect",
            searched_by=searched_by,
        )

    if docstatus == 1:
        # REMOTE_SUBMITTED — verify fields match if we have the expected payload.
        if expected_payload is not None:
            from construction_ai.executive.executor import (
                _canonical_erp_invoice, _compare_canonical,
            )
            canonical_expected = _canonical_erp_invoice(expected_payload)
            canonical_actual = _canonical_erp_invoice(doc)
            mismatches = _compare_canonical(canonical_expected, canonical_actual)
            if mismatches:
                _transition_to(repos, org_scope, action_id, "failed_terminal",
                                remote_document_id=docname,
                                last_error=f"reconciliation: field mismatch: {mismatches}",
                                remote_state="remote_mismatch")
                _audit_reconciliation(repos, org_scope, action_id, "unknown",
                                      "failed_terminal",
                                      REMOTE_MISMATCH, searched_by, docname)
                return ReconciliationOutcome(
                    action_id=action_id,
                    classification=REMOTE_MISMATCH,
                    new_status="failed_terminal",
                    remote_document_id=docname,
                    reason=f"field mismatch: {mismatches}",
                    searched_by=searched_by,
                )

        result = {"docname": docname, "docstatus": docstatus}
        repos.external_actions.transition(
            scope=org_scope,
            action_id=action_id,
            from_status="unknown",
            to_status="confirmed",
            remote_document_id=docname,
            remote_state="remote_submitted",
            result=result,
        )
        _audit_reconciliation(repos, org_scope, action_id, "unknown", "confirmed",
                              REMOTE_SUBMITTED, searched_by, docname)
        return ReconciliationOutcome(
            action_id=action_id,
            classification=REMOTE_SUBMITTED,
            new_status="confirmed",
            remote_document_id=docname,
            reason=f"found one submitted ERP document: {docname}",
            searched_by=searched_by,
        )

    # docstatus is something else (e.g. 2 = cancelled) — mismatch.
    _transition_to(repos, org_scope, action_id, "failed_terminal",
                    remote_document_id=docname,
                    last_error=f"reconciliation: remote document {docname} has unexpected docstatus={docstatus}",
                    remote_state="remote_mismatch")
    _audit_reconciliation(repos, org_scope, action_id, "unknown", "failed_terminal",
                          REMOTE_MISMATCH, searched_by, docname)
    return ReconciliationOutcome(
        action_id=action_id,
        classification=REMOTE_MISMATCH,
        new_status="failed_terminal",
        remote_document_id=docname,
        reason=f"remote document {docname} has unexpected docstatus={docstatus} — manual reconciliation required",
        searched_by=searched_by,
    )


def _transition_to(
    repos: Repositories, scope: Scope, action_id: UUID, to_status: str,
    *, remote_document_id: str | None = None, last_error: str | None = None,
    remote_state: str | None = None, recovery_attempts: int | None = None,
) -> None:
    """Transition an UNKNOWN action to a new status."""
    repos.external_actions.transition(
        scope=scope, action_id=action_id, from_status="unknown", to_status=to_status,
        remote_document_id=remote_document_id, last_error=last_error,
        remote_state=remote_state, recovery_attempts=recovery_attempts,
    )


def _read_document(transport, doctype: str, docname: str) -> dict[str, Any] | None:
    """Read a single document from ERP by name. Returns None if not found."""
    response = transport.get(f"/api/resource/{quote(doctype)}/{quote(docname)}")
    data = response.get("data", response) if isinstance(response, dict) else {}
    if not data or not data.get("name"):
        return None
    return data


def _search_by_idempotency_key(transport, key: str) -> list[dict[str, Any]]:
    """Search ERP for Purchase Invoices with a matching construct_idempotency_key."""
    import json

    filters = json.dumps([["construct_idempotency_key", "=", key]])
    response = transport.get(
        "/api/resource/Purchase Invoice",
        params={"filters": filters, "fields": json.dumps(["name", "supplier", "bill_no", "docstatus", "grand_total", "construct_idempotency_key"])},
    )
    data = response.get("data", response) if isinstance(response, dict) else []
    return data if isinstance(data, list) else []


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
    classification: str,
    searched_by: list[str],
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
            "classification": classification,
            "searched_by": searched_by,
            "remote_document_id": remote_doc,
        },
    )
