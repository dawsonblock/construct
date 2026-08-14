"""Tri-state duplicate detection (Phase 18).

Replaces the single boolean "same vendor + same invoice number" check with a
multi-signal evaluation returning one of:

    NOT_DUPLICATE        — no signals matched
    POSSIBLE_DUPLICATE   — a weak signal matched (vendor+date+amount,
                            PO+amount+date, near-duplicate fields). A possible
                            duplicate places the invoice on HOLD for review, not
                            an automatic rejection.
    CONFIRMED_DUPLICATE  — a strong signal matched (vendor+invoice number,
                            ERP supplier ID+invoice number, document content
                            hash). This is a hard FAIL.

The service reads only persisted authoritative records. It never accepts a
caller's assertion that an invoice is or is not a duplicate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories import Repositories

NOT_DUPLICATE = "NOT_DUPLICATE"
POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"
CONFIRMED_DUPLICATE = "CONFIRMED_DUPLICATE"

#: Amount comparison tolerance for the weak signals.
_AMOUNT_TOLERANCE = Decimal("0.01")


@dataclass(frozen=True)
class DuplicateMatch:
    """A single matching signal."""
    signal: str
    matched_invoice_id: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DuplicateResult:
    status: str  # NOT_DUPLICATE / POSSIBLE_DUPLICATE / CONFIRMED_DUPLICATE
    matches: list[DuplicateMatch] = field(default_factory=list)

    @property
    def is_duplicate(self) -> bool:
        return self.status != NOT_DUPLICATE


def _dec(v: Any) -> Decimal | None:
    return Decimal(str(v)) if v is not None else None


def _amounts_match(a: Any, b: Any) -> bool:
    da, db = _dec(a), _dec(b)
    if da is None or db is None:
        return False
    return abs(da - db) <= _AMOUNT_TOLERANCE


def _dates_match(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return False
    # Compare the date portion only (both may be datetime or date or string).
    sa, sb = str(a)[:10], str(b)[:10]
    return sa == sb and sa != "None"


def detect_duplicates(
    repos: Repositories,
    *,
    scope: Scope,
    invoice_id: UUID,
) -> DuplicateResult:
    """Evaluate every duplicate signal for an invoice.

    The invoice under evaluation is excluded from its own candidate set.
    """
    invoice = repos.invoices.get(scope=scope, invoice_id=invoice_id)
    if invoice is None:
        return DuplicateResult(status=NOT_DUPLICATE)

    matches: list[DuplicateMatch] = []
    confirmed = False

    # --- Strong signals (CONFIRMED_DUPLICATE) ---

    # 1. vendor + invoice number (the existing unique constraint, evaluated
    #    across the tenant — a second row with the same vendor+number).
    if invoice.vendor_company_id:
        same_id = _find_by_vendor_and_number(
            repos, scope, UUID(invoice.vendor_company_id), invoice.invoice_number, exclude=invoice_id
        )
        if same_id is not None:
            matches.append(DuplicateMatch(
                signal="vendor+invoice_number", matched_invoice_id=same_id,
                detail={"vendor_company_id": invoice.vendor_company_id, "invoice_number": invoice.invoice_number},
            ))
            confirmed = True

    # 2. ERP supplier ID + invoice number.
    if invoice.vendor_company_id:
        company = repos.companies.get(scope=scope.organization_only, company_id=UUID(invoice.vendor_company_id))
        if company and company.erp_supplier_id:
            erp_match = _find_by_erp_supplier_and_number(
                repos, scope, company.erp_supplier_id, invoice.invoice_number, exclude=invoice_id
            )
            if erp_match is not None:
                matches.append(DuplicateMatch(
                    signal="erp_supplier_id+invoice_number", matched_invoice_id=str(erp_match),
                    detail={"erp_supplier_id": company.erp_supplier_id, "invoice_number": invoice.invoice_number},
                ))
                confirmed = True

    # 3. Document content hash — same underlying document already ingested.
    if invoice.source_version_id:
        content_match = _find_by_content_hash(repos, scope, UUID(invoice.source_version_id), exclude=invoice_id)
        if content_match is not None:
            matches.append(DuplicateMatch(
                signal="document_content_hash", matched_invoice_id=str(content_match),
                detail={"source_version_id": invoice.source_version_id},
            ))
            confirmed = True

    # --- Weak signals (POSSIBLE_DUPLICATE) ---

    # 4. vendor + date + amount.
    if not confirmed:
        for candidate in _tenant_invoices(repos, scope, exclude=invoice_id):
            if (invoice.vendor_company_id
                    and str(candidate.get("vendor_company_id")) == str(invoice.vendor_company_id)
                    and _dates_match(invoice.invoice_date, candidate.get("invoice_date"))
                    and _amounts_match(invoice.total, candidate.get("total"))):
                matches.append(DuplicateMatch(
                    signal="vendor+date+amount", matched_invoice_id=str(candidate["invoice_id"]),
                    detail={"vendor_company_id": invoice.vendor_company_id,
                            "invoice_date": str(invoice.invoice_date), "total": str(invoice.total)},
                ))
                break

    # 5. same PO + amount + date.
    if not confirmed and invoice.po_number:
        for candidate in _tenant_invoices(repos, scope, exclude=invoice_id):
            if (invoice.po_number == candidate.get("po_reference")
                    and _dates_match(invoice.invoice_date, candidate.get("invoice_date"))
                    and _amounts_match(invoice.total, candidate.get("total"))):
                matches.append(DuplicateMatch(
                    signal="po+amount+date", matched_invoice_id=str(candidate["invoice_id"]),
                    detail={"po_reference": invoice.po_number,
                            "invoice_date": str(invoice.invoice_date), "total": str(invoice.total)},
                ))
                break

    if confirmed:
        return DuplicateResult(status=CONFIRMED_DUPLICATE, matches=matches)
    if matches:
        return DuplicateResult(status=POSSIBLE_DUPLICATE, matches=matches)
    return DuplicateResult(status=NOT_DUPLICATE, matches=[])


# -- internal queries ------------------------------------------------------

def _find_by_vendor_and_number(
    repos: Repositories, scope: Scope, vendor_company_id: UUID, invoice_number: str, *, exclude: UUID
) -> str | None:
    """Another invoice with the same vendor company + invoice number, excluding
    the invoice under evaluation."""
    with repos.db.scoped(scope.organization_only) as cur:
        cur.execute(
            """SELECT invoice_id FROM invoices
               WHERE organization_id = %s AND vendor_company_id = %s
                 AND invoice_number = %s AND invoice_id <> %s
               LIMIT 1""",
            (scope.organization_id, vendor_company_id, invoice_number, exclude),
        )
        row = cur.fetchone()
        return str(row[0]) if row else None


def _find_by_erp_supplier_and_number(
    repos: Repositories, scope: Scope, erp_supplier_id: str, invoice_number: str, *, exclude: UUID
) -> str | None:
    """Another invoice from a company with the same ERP supplier ID and same
    invoice number, excluding the invoice under evaluation."""
    with repos.db.scoped(scope.organization_only) as cur:
        cur.execute(
            """SELECT i.invoice_id
               FROM invoices i
               JOIN companies c ON c.organization_id = i.organization_id AND c.company_id = i.vendor_company_id
               WHERE i.organization_id = %s
                 AND c.erp_supplier_id = %s
                 AND i.invoice_number = %s
                 AND i.invoice_id <> %s
               LIMIT 1""",
            (scope.organization_id, erp_supplier_id, invoice_number, exclude),
        )
        row = cur.fetchone()
        return str(row[0]) if row else None


def _find_by_content_hash(
    repos: Repositories, scope: Scope, source_version_id: UUID, *, exclude: UUID
) -> str | None:
    """Another invoice whose source document shares the same content hash."""
    with repos.db.scoped(scope.organization_only) as cur:
        cur.execute(
            """SELECT i.invoice_id
               FROM invoices i
               JOIN document_versions dv
                 ON dv.organization_id = i.organization_id AND dv.document_version_id = i.source_version_id
               WHERE i.organization_id = %s
                 AND i.source_version_id IS NOT NULL
                 AND dv.content_hash = (SELECT content_hash FROM document_versions
                                        WHERE organization_id = %s AND document_version_id = %s)
                 AND i.invoice_id <> %s
               LIMIT 1""",
            (scope.organization_id, scope.organization_id, source_version_id, exclude),
        )
        row = cur.fetchone()
        return str(row[0]) if row else None


def _tenant_invoices(repos: Repositories, scope: Scope, *, exclude: UUID) -> list[dict[str, Any]]:
    """All invoices in the tenant except the one under evaluation (for weak
    signal scanning)."""
    with repos.db.scoped(scope.organization_only) as cur:
        cur.execute(
            """SELECT invoice_id, vendor_company_id, invoice_number, total, currency,
                      po_reference, invoice_date
               FROM invoices
               WHERE organization_id = %s AND invoice_id <> %s""",
            (scope.organization_id, exclude),
        )
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
