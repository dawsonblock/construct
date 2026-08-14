"""Purchase orders, quotes, invoices and invoice lines.

Money is `numeric` in the database and `float` nowhere near a decision. Amounts
come back as `Decimal` and are converted only at the domain boundary, where the
existing verifier compares them with an explicit tolerance.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from construction_ai.domain.models import Invoice, PurchaseOrder, Quote
from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories.base import Repository

PO_COLUMNS = "organization_id, purchase_order_id, project_id, vendor_company_id, reference, amount, currency, quote_reference, status, erp_docname, ordered_on"
QUOTE_COLUMNS = "organization_id, quote_id, project_id, vendor_company_id, reference, amount, currency, revision, approved, erp_docname"
INVOICE_COLUMNS = (
    "organization_id, invoice_id, project_id, vendor_company_id, reference, invoice_number, vendor_name, "
    "subtotal, tax, total, currency, po_reference, quote_reference, status, source_id, source_version_id, observed_at, invoice_date"
)


def _f(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _to_po(row: dict[str, Any]) -> PurchaseOrder:
    return PurchaseOrder(
        po_id=str(row["purchase_order_id"]),
        organization_id=str(row["organization_id"]),
        po_number=row["reference"],
        project_id=str(row["project_id"]) if row.get("project_id") else None,
        vendor_company_id=str(row["vendor_company_id"]) if row.get("vendor_company_id") else None,
        amount=_f(row["amount"]),
        quote_number=row.get("quote_reference"),
        reference=row["reference"],
        ordered_on=row.get("ordered_on"),
    )


def _to_quote(row: dict[str, Any]) -> Quote:
    return Quote(
        quote_id=str(row["quote_id"]),
        organization_id=str(row["organization_id"]),
        quote_number=row["reference"],
        project_id=str(row["project_id"]) if row.get("project_id") else None,
        vendor_company_id=str(row["vendor_company_id"]) if row.get("vendor_company_id") else None,
        amount=_f(row["amount"]),
        approved=row["approved"],
        reference=row["reference"],
    )


def _to_invoice(row: dict[str, Any]) -> Invoice:
    return Invoice(
        invoice_id=str(row["invoice_id"]),
        organization_id=str(row["organization_id"]),
        invoice_number=row["invoice_number"],
        vendor_name=row["vendor_name"],
        total=_f(row["total"]),
        subtotal=_f(row.get("subtotal")),
        tax=_f(row.get("tax")),
        currency=row["currency"],
        po_number=row.get("po_reference"),
        quote_number=row.get("quote_reference"),
        project_id=str(row["project_id"]) if row.get("project_id") else None,
        source_id=str(row["source_id"]) if row.get("source_id") else None,
        vendor_company_id=str(row["vendor_company_id"]) if row.get("vendor_company_id") else None,
        reference=row["reference"],
        source_version_id=str(row["source_version_id"]) if row.get("source_version_id") else None,
        invoice_date=row.get("invoice_date"),
    )


class PurchaseOrderRepository(Repository):
    table = "purchase_orders"
    id_column = "purchase_order_id"

    def upsert(self, *, scope: Scope, reference: str, vendor_company_id: UUID | None, amount: float | None,
               quote_reference: str | None = None, erp_docname: str | None = None,
               ordered_on=None, created_by: str = "system") -> PurchaseOrder:
        with self.db.scoped(scope) as cur:
            cur.execute(
                f"""INSERT INTO purchase_orders(organization_id, project_id, vendor_company_id, reference, amount, quote_reference, erp_docname, ordered_on, created_by)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (organization_id, reference) DO UPDATE
                      SET amount = EXCLUDED.amount, vendor_company_id = EXCLUDED.vendor_company_id,
                          project_id = EXCLUDED.project_id, quote_reference = EXCLUDED.quote_reference
                    RETURNING {PO_COLUMNS}""",
                (scope.organization_id, scope.project_id, vendor_company_id, reference, Decimal(str(amount)) if amount is not None else None, quote_reference, erp_docname, ordered_on, created_by),
            )
            columns = [c.name for c in cur.description]
            return _to_po(dict(zip(columns, cur.fetchone(), strict=True)))

    def get_by_reference(self, *, scope: Scope, reference: str) -> PurchaseOrder | None:
        clause, params = self._tenant_clause(scope)
        row = self._fetch_one(scope, f"SELECT {PO_COLUMNS} FROM purchase_orders WHERE {clause} AND reference = %s", [*params, reference])
        return _to_po(row) if row else None

    def for_project(self, *, scope: Scope) -> list[PurchaseOrder]:
        project_id = scope.require_project()
        rows = self._fetch_all(
            scope,
            f"SELECT {PO_COLUMNS} FROM purchase_orders WHERE organization_id = %s AND project_id = %s ORDER BY reference",
            [scope.organization_id, project_id],
        )
        return [_to_po(row) for row in rows]


class QuoteRepository(Repository):
    table = "quotes"
    id_column = "quote_id"

    def upsert(self, *, scope: Scope, reference: str, vendor_company_id: UUID | None, amount: float | None,
               approved: bool = False, revision: int = 1, erp_docname: str | None = None, created_by: str = "system") -> Quote:
        with self.db.scoped(scope) as cur:
            cur.execute(
                f"""INSERT INTO quotes(organization_id, project_id, vendor_company_id, reference, amount, approved, revision, erp_docname, created_by)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (organization_id, reference, revision) DO UPDATE
                      SET amount = EXCLUDED.amount, approved = EXCLUDED.approved,
                          vendor_company_id = EXCLUDED.vendor_company_id, project_id = EXCLUDED.project_id
                    RETURNING {QUOTE_COLUMNS}""",
                (scope.organization_id, scope.project_id, vendor_company_id, reference, Decimal(str(amount)) if amount is not None else None, approved, revision, erp_docname, created_by),
            )
            columns = [c.name for c in cur.description]
            return _to_quote(dict(zip(columns, cur.fetchone(), strict=True)))

    def get_by_reference(self, *, scope: Scope, reference: str) -> Quote | None:
        clause, params = self._tenant_clause(scope)
        row = self._fetch_one(
            scope,
            f"SELECT {QUOTE_COLUMNS} FROM quotes WHERE {clause} AND reference = %s ORDER BY revision DESC LIMIT 1",
            [*params, reference],
        )
        return _to_quote(row) if row else None

    def for_project(self, *, scope: Scope) -> list[Quote]:
        project_id = scope.require_project()
        rows = self._fetch_all(
            scope,
            f"SELECT {QUOTE_COLUMNS} FROM quotes WHERE organization_id = %s AND project_id = %s ORDER BY reference, revision",
            [scope.organization_id, project_id],
        )
        return [_to_quote(row) for row in rows]


class InvoiceRepository(Repository):
    table = "invoices"
    id_column = "invoice_id"

    def create(
        self,
        *,
        scope: Scope,
        reference: str,
        invoice_number: str,
        vendor_name: str,
        total: float | None = None,
        subtotal: float | None = None,
        tax: float | None = None,
        currency: str = "CAD",
        po_reference: str | None = None,
        quote_reference: str | None = None,
        vendor_company_id: UUID | None = None,
        source_id: UUID | None = None,
        source_version_id: UUID | None = None,
        invoice_date=None,
        created_by: str = "system",
    ) -> Invoice:
        with self.db.scoped(scope) as cur:
            cur.execute(
                f"""INSERT INTO invoices(organization_id, project_id, vendor_company_id, reference, invoice_number,
                        vendor_name, subtotal, tax, total, currency, po_reference, quote_reference,
                        source_id, source_version_id, invoice_date, created_by)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING {INVOICE_COLUMNS}""",
                (
                    scope.organization_id, scope.project_id, vendor_company_id, reference, invoice_number,
                    vendor_name,
                    Decimal(str(subtotal)) if subtotal is not None else None,
                    Decimal(str(tax)) if tax is not None else None,
                    Decimal(str(total)) if total is not None else None, currency, po_reference, quote_reference,
                    source_id, source_version_id, invoice_date, created_by,
                ),
            )
            columns = [c.name for c in cur.description]
            return _to_invoice(dict(zip(columns, cur.fetchone(), strict=True)))

    def get(self, *, scope: Scope, invoice_id: UUID) -> Invoice | None:
        row = self.get_row(scope=scope, record_id=invoice_id, columns=INVOICE_COLUMNS)
        return _to_invoice(row) if row else None

    def get_by_reference(self, *, scope: Scope, reference: str) -> Invoice | None:
        row = self._fetch_one(
            scope,
            f"SELECT {INVOICE_COLUMNS} FROM invoices WHERE organization_id = %s AND reference = %s",
            [scope.organization_id, reference],
        )
        return _to_invoice(row) if row else None

    def for_project(self, *, scope: Scope) -> list[Invoice]:
        project_id = scope.require_project()
        rows = self._fetch_all(
            scope,
            f"SELECT {INVOICE_COLUMNS} FROM invoices WHERE organization_id = %s AND project_id = %s ORDER BY observed_at",
            [scope.organization_id, project_id],
        )
        return [_to_invoice(row) for row in rows]

    def list(self, *, scope: Scope, status: str | None = None) -> list[Invoice]:
        clause, params = self._tenant_clause(scope)
        sql = f"SELECT {INVOICE_COLUMNS} FROM invoices WHERE {clause}"
        if status:
            sql += " AND status = %s"
            params.append(status)
        return [_to_invoice(row) for row in self._fetch_all(scope, sql + " ORDER BY observed_at", params)]

    def find_duplicate(self, *, scope: Scope, vendor_company_id: UUID | None, invoice_number: str) -> Invoice | None:
        """Same vendor, same invoice number, same tenant. Phase 15 extends this."""
        if vendor_company_id is None:
            return None
        row = self._fetch_one(
            scope,
            f"SELECT {INVOICE_COLUMNS} FROM invoices WHERE organization_id = %s AND vendor_company_id = %s AND invoice_number = %s",
            [scope.organization_id, vendor_company_id, invoice_number],
        )
        return _to_invoice(row) if row else None

    def assign_project(self, *, scope: Scope, invoice_id: UUID, project_id: UUID) -> bool:
        """File an invoice, taking its lines with it.

        `ON UPDATE CASCADE` on the scope-preserving key re-files children when the
        parent moves between two projects — but a foreign key with a NULL
        component is not enforced at all, so it cannot fill in the NULL on the
        first filing. Both statements run in one transaction; an invoice and its
        lines are never observably on different projects.
        """
        with self.db.scoped(scope.organization_only) as cur:
            cur.execute(
                "UPDATE invoices SET project_id = %s WHERE organization_id = %s AND invoice_id = %s RETURNING invoice_id",
                (project_id, scope.organization_id, invoice_id),
            )
            if cur.fetchone() is None:
                return False
            cur.execute(
                "UPDATE invoice_lines SET project_id = %s WHERE organization_id = %s AND invoice_id = %s AND project_id IS DISTINCT FROM %s",
                (project_id, scope.organization_id, invoice_id, project_id),
            )
            return True

    def set_status(self, *, scope: Scope, invoice_id: UUID, status: str) -> bool:
        clause, params = self._tenant_clause(scope)
        with self.db.scoped(scope) as cur:
            cur.execute(
                f"UPDATE invoices SET status = %s WHERE {clause} AND invoice_id = %s RETURNING invoice_id",  # noqa: S608
                [status, *params, invoice_id],
            )
            return cur.fetchone() is not None

    # -- lines --------------------------------------------------------------

    def add_line(self, *, scope: Scope, invoice_id: UUID, line_number: int, description: str,
                 amount: float, quantity: float | None = None, unit_amount: float | None = None) -> UUID:
        with self.db.scoped(scope.organization_only) as cur:
            cur.execute(
                """INSERT INTO invoice_lines(organization_id, invoice_id, project_id, line_number, description, quantity, unit_amount, amount)
                   SELECT %s, %s, i.project_id, %s, %s, %s, %s, %s
                   FROM invoices i WHERE i.organization_id = %s AND i.invoice_id = %s
                   ON CONFLICT (organization_id, invoice_id, line_number) DO UPDATE
                     SET description = EXCLUDED.description, amount = EXCLUDED.amount
                   RETURNING line_id""",
                (
                    scope.organization_id, invoice_id, line_number, description,
                    Decimal(str(quantity)) if quantity is not None else None,
                    Decimal(str(unit_amount)) if unit_amount is not None else None,
                    Decimal(str(amount)), scope.organization_id, invoice_id,
                ),
            )
            row = cur.fetchone()
            if row is None:
                raise LookupError(f"invoice {invoice_id} not found in this organization")
            return row[0]

    def lines(self, *, scope: Scope, invoice_id: UUID) -> list[dict[str, Any]]:
        clause, params = self._tenant_clause(scope)
        return self._fetch_all(
            scope,
            f"SELECT line_id, line_number, description, quantity, unit_amount, amount FROM invoice_lines WHERE {clause} AND invoice_id = %s ORDER BY line_number",
            [*params, invoice_id],
        )
