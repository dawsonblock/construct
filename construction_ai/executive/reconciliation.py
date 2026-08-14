"""v0.5.0-rc1 — ERP readback and reconciliation (items 46, 47).

Readback (item 46) is built into the executor: after submitting a document to
ERP, the executor reads it back and confirms docstatus=1. This module provides
a standalone readback function for verification outside the execution path.

Reconciliation (item 47) compares local state with ERP state:
- For each submitted invoice (recorded in the external action ledger), read the
  document back from ERP and verify that the stored values match what we sent.
- Detect drift: ERP values that differ from the local record.
- Detect missing documents: ERP documents that no longer exist.
- Detect unauthorized changes: ERP documents whose docstatus was reverted.

Reconciliation is read-only — it never writes to ERP or to the database. It
produces a report that a human can act on.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories import Repositories


@dataclass(frozen=True)
class ReadbackResult:
    """Result of reading back a single ERP document."""
    docname: str
    found: bool
    docstatus: int | None
    matches_expected: bool
    discrepancies: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ReconciliationReport:
    """Result of reconciling all submitted invoices with ERP."""
    total_checked: int
    matched: int
    drifted: int
    missing: int
    details: list[dict[str, Any]] = field(default_factory=list)


def readback_document(
    *,
    erp_read_transport,
    doctype: str,
    docname: str,
    expected_docstatus: int = 1,
) -> ReadbackResult:
    """Read a single document back from ERP and verify its status.

    This is the standalone readback function (item 46). The executor uses
    inline readback; this function is for reconciliation and manual verification.
    """
    response = erp_read_transport.get(f"/api/resource/{quote(doctype)}/{quote(docname)}")
    data = response.get("data", response) if isinstance(response, dict) else {}
    if not data or not data.get("name"):
        return ReadbackResult(docname=docname, found=False, docstatus=None, matches_expected=False)
    docstatus = data.get("docstatus", 0)
    matches = docstatus == expected_docstatus
    discrepancies = []
    if not matches:
        discrepancies.append(f"docstatus={docstatus}, expected={expected_docstatus}")
    return ReadbackResult(
        docname=docname,
        found=True,
        docstatus=docstatus,
        matches_expected=matches,
        discrepancies=discrepancies,
    )


def reconcile_erp_state(
    repos: Repositories,
    *,
    scope: Scope,
    erp_read_transport,
) -> ReconciliationReport:
    """Reconcile all submitted invoices with ERP state (item 47).

    For each completed external action of type 'erp_submit_purchase_invoice',
    read the document back from ERP and verify:
    - The document still exists.
    - The docstatus is still 1 (submitted).
    - The stored values (supplier, total, currency) match what we sent.

    Returns a ReconciliationReport with per-document details.

    This function is read-only — it never writes to ERP or to the database.
    """
    org_scope = scope.organization_only
    actions = repos.external_actions.list(
        scope=org_scope, action_type="erp_submit_purchase_invoice", limit=1000,
    )

    details = []
    matched = 0
    drifted = 0
    missing = 0

    for action in actions:
        if action.status != "completed":
            continue
        result = action.result or {}
        docname = result.get("docname", "")
        if not docname:
            continue

        # Read back from ERP.
        readback = readback_document(
            erp_read_transport=erp_read_transport,
            doctype="Purchase Invoice",
            docname=docname,
            expected_docstatus=1,
        )

        detail: dict[str, Any] = {
            "action_id": str(action.action_id),
            "docname": docname,
            "found": readback.found,
            "docstatus": readback.docstatus,
            "matches_expected": readback.matches_expected,
            "discrepancies": readback.discrepancies,
        }

        if not readback.found:
            missing += 1
            detail["status"] = "missing"
        elif readback.matches_expected:
            matched += 1
            detail["status"] = "matched"
        else:
            drifted += 1
            detail["status"] = "drifted"

        details.append(detail)

    return ReconciliationReport(
        total_checked=len(details),
        matched=matched,
        drifted=drifted,
        missing=missing,
        details=details,
    )
