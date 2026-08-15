"""Schedule-of-values repository (Phases 15, 16, 17).

contracts → sov_items → change_orders, plus invoice_allocations binding an
invoice to the scope it bills. All money is read/written as Decimal — the
numeric(16,2) columns never cross a float boundary here.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from construction_ai.domain.models import (
    ChangeOrder,
    ChangeOrderAllocation,
    Contract,
    InvoiceAllocation,
    SOVItem,
)
from construction_ai.persistence.db import Database, Scope, row_to_dict, rows_to_dicts


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


class ContractRepository:
    def __init__(self, db: Database):
        self.db = db

    def create(self, *, scope: Scope, project_id: UUID, company_id: UUID, reference: str,
               name: str = "", base_contract_value: Any = 0, currency: str = "CAD",
               status: str = "active") -> Contract:
        with self.db.scoped(scope) as cur:
            cur.execute(
                """INSERT INTO contracts(organization_id, project_id, company_id, reference,
                       name, base_contract_value, currency, status)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (organization_id, reference) DO UPDATE
                     SET name=EXCLUDED.name, base_contract_value=EXCLUDED.base_contract_value,
                         currency=EXCLUDED.currency, status=EXCLUDED.status, updated_at=now()
                   RETURNING contract_id, organization_id, project_id, company_id, reference,
                             name, base_contract_value, currency, status""",
                (scope.organization_id, project_id, company_id, reference, name,
                 Decimal(str(base_contract_value)), currency, status),
            )
            return _to_contract(row_to_dict(cur))

    def get(self, *, scope: Scope, contract_id: UUID) -> Contract | None:
        with self.db.scoped(scope) as cur:
            cur.execute(
                """SELECT contract_id, organization_id, project_id, company_id, reference,
                          name, base_contract_value, currency, status
                   FROM contracts WHERE organization_id=%s AND contract_id=%s""",
                (scope.organization_id, contract_id),
            )
            r = row_to_dict(cur)
            return _to_contract(r) if r else None

    def for_project(self, *, scope: Scope, project_id: UUID) -> list[Contract]:
        with self.db.scoped(scope) as cur:
            cur.execute(
                """SELECT contract_id, organization_id, project_id, company_id, reference,
                          name, base_contract_value, currency, status
                   FROM contracts WHERE organization_id=%s AND project_id=%s AND status='active'
                   ORDER BY reference""",
                (scope.organization_id, project_id),
            )
            return [_to_contract(r) for r in rows_to_dicts(cur)]


class SOVItemRepository:
    def __init__(self, db: Database):
        self.db = db

    def create(self, *, scope: Scope, contract_id: UUID, reference: str, name: str,
               base_value: Any = 0, currency: str = "CAD", sort_order: int = 0) -> SOVItem:
        with self.db.scoped(scope) as cur:
            cur.execute(
                """INSERT INTO sov_items(organization_id, contract_id, reference, name,
                       base_value, currency, sort_order)
                   VALUES(%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (organization_id, contract_id, reference) DO UPDATE
                     SET name=EXCLUDED.name, base_value=EXCLUDED.base_value,
                         currency=EXCLUDED.currency, sort_order=EXCLUDED.sort_order, updated_at=now()
                   RETURNING sov_item_id, organization_id, contract_id, reference, name,
                             base_value, currency, sort_order""",
                (scope.organization_id, contract_id, reference, name,
                 Decimal(str(base_value)), currency, sort_order),
            )
            return _to_sov_item(row_to_dict(cur))

    def get(self, *, scope: Scope, sov_item_id: UUID) -> SOVItem | None:
        with self.db.scoped(scope) as cur:
            cur.execute(
                """SELECT sov_item_id, organization_id, contract_id, reference, name,
                          base_value, currency, sort_order
                   FROM sov_items WHERE organization_id=%s AND sov_item_id=%s""",
                (scope.organization_id, sov_item_id),
            )
            r = row_to_dict(cur)
            return _to_sov_item(r) if r else None

    def for_contract(self, *, scope: Scope, contract_id: UUID) -> list[SOVItem]:
        with self.db.scoped(scope) as cur:
            cur.execute(
                """SELECT sov_item_id, organization_id, contract_id, reference, name,
                          base_value, currency, sort_order
                   FROM sov_items WHERE organization_id=%s AND contract_id=%s
                   ORDER BY sort_order, reference""",
                (scope.organization_id, contract_id),
            )
            return [_to_sov_item(r) for r in rows_to_dicts(cur)]


class ChangeOrderRepository:
    def __init__(self, db: Database):
        self.db = db

    def create(self, *, scope: Scope, contract_id: UUID, reference: str, amount: Any,
               name: str = "", currency: str = "CAD", status: str = "approved",
               sov_item_id: UUID | None = None) -> ChangeOrder:
        with self.db.scoped(scope) as cur:
            cur.execute(
                """INSERT INTO change_orders(organization_id, contract_id, reference, name,
                       amount, currency, status)
                   VALUES(%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (organization_id, reference) DO UPDATE
                     SET name=EXCLUDED.name, amount=EXCLUDED.amount,
                         currency=EXCLUDED.currency, status=EXCLUDED.status, updated_at=now()
                   RETURNING change_order_id, organization_id, contract_id, reference, name,
                             amount, currency, status""",
                (scope.organization_id, contract_id, reference, name,
                 Decimal(str(amount)), currency, status),
            )
            co = _to_change_order(row_to_dict(cur))

        if sov_item_id is not None:
            self.allocate(
                scope=scope,
                change_order_id=UUID(co.change_order_id),
                sov_item_id=sov_item_id,
                amount=amount,
                currency=currency,
            )
        return co

    def allocate(self, *, scope: Scope, change_order_id: UUID, sov_item_id: UUID,
                 amount: Any, currency: str = "CAD") -> ChangeOrderAllocation:
        """rc5 Phase 2: Explicitly bind a change order to a specific SOV item.

        rc7 Phase 20: Validates that the allocation currency matches the
        change order currency and the SOV item currency. No silent conversion.
        """
        # rc7 Phase 20: Currency consistency check.
        with self.db.scoped(scope) as cur:
            cur.execute(
                """SELECT currency FROM change_orders
                   WHERE organization_id = %s AND change_order_id = %s""",
                (scope.organization_id, change_order_id),
            )
            co_row = row_to_dict(cur)
            if co_row and co_row.get("currency") and co_row["currency"] != currency:
                raise ValueError(
                    f"currency mismatch: allocation currency={currency} "
                    f"does not match change order currency={co_row['currency']} — "
                    "rc7 Phase 20: no silent conversion"
                )

            cur.execute(
                """SELECT currency FROM sov_items
                   WHERE organization_id = %s AND sov_item_id = %s""",
                (scope.organization_id, sov_item_id),
            )
            sov_row = row_to_dict(cur)
            if sov_row and sov_row.get("currency") and sov_row["currency"] != currency:
                raise ValueError(
                    f"currency mismatch: allocation currency={currency} "
                    f"does not match SOV item currency={sov_row['currency']} — "
                    "rc7 Phase 20: no silent conversion"
                )

            cur.execute(
                """INSERT INTO change_order_allocations(organization_id, change_order_id, sov_item_id, amount, currency)
                   VALUES(%s,%s,%s,%s,%s)
                   ON CONFLICT (organization_id, change_order_id, sov_item_id) DO UPDATE
                     SET amount=EXCLUDED.amount, currency=EXCLUDED.currency
                   RETURNING allocation_id, organization_id, change_order_id, sov_item_id, amount, currency""",
                (scope.organization_id, change_order_id, sov_item_id, Decimal(str(amount)), currency),
            )
            return _to_change_order_allocation(row_to_dict(cur))

    def allocations_for_sov_item(self, *, scope: Scope, sov_item_id: UUID) -> list[ChangeOrderAllocation]:
        with self.db.scoped(scope) as cur:
            cur.execute(
                """SELECT allocation_id, organization_id, change_order_id, sov_item_id, amount, currency
                   FROM change_order_allocations WHERE organization_id=%s AND sov_item_id=%s""",
                (scope.organization_id, sov_item_id),
            )
            return [_to_change_order_allocation(r) for r in rows_to_dicts(cur)]

    def approved_allocated_amount_for_sov_item(self, *, scope: Scope, sov_item_id: UUID) -> Decimal:
        """Sum of approved change order amounts explicitly allocated to this SOV item."""
        with self.db.scoped(scope) as cur:
            cur.execute(
                """SELECT COALESCE(SUM(coa.amount), 0) AS total
                   FROM change_order_allocations coa
                   JOIN change_orders co ON coa.organization_id = co.organization_id AND coa.change_order_id = co.change_order_id
                   WHERE coa.organization_id = %s AND coa.sov_item_id = %s AND co.status = 'approved'""",
                (scope.organization_id, sov_item_id),
            )
            r = row_to_dict(cur)
            return Decimal(str(r["total"])) if r else Decimal("0")

    def approved_for_contract(self, *, scope: Scope, contract_id: UUID) -> list[ChangeOrder]:
        with self.db.scoped(scope) as cur:
            cur.execute(
                """SELECT change_order_id, organization_id, contract_id, reference, name,
                          amount, currency, status
                   FROM change_orders WHERE organization_id=%s AND contract_id=%s AND status='approved'
                   ORDER BY reference""",
                (scope.organization_id, contract_id),
            )
            return [_to_change_order(r) for r in rows_to_dicts(cur)]


class InvoiceAllocationRepository:
    def __init__(self, db: Database):
        self.db = db

    def create(self, *, scope: Scope, invoice_id: UUID, sov_item_id: UUID, amount: Any,
               currency: str = "CAD") -> InvoiceAllocation:
        with self.db.scoped(scope) as cur:
            cur.execute(
                """INSERT INTO invoice_allocations(organization_id, invoice_id, sov_item_id, amount, currency)
                   VALUES(%s,%s,%s,%s,%s)
                   ON CONFLICT (organization_id, invoice_id, sov_item_id) DO UPDATE
                     SET amount=EXCLUDED.amount, currency=EXCLUDED.currency
                   RETURNING allocation_id, organization_id, invoice_id, sov_item_id, amount, currency""",
                (scope.organization_id, invoice_id, sov_item_id, Decimal(str(amount)), currency),
            )
            return _to_allocation(row_to_dict(cur))

    def for_invoice(self, *, scope: Scope, invoice_id: UUID) -> list[InvoiceAllocation]:
        with self.db.scoped(scope) as cur:
            cur.execute(
                """SELECT allocation_id, organization_id, invoice_id, sov_item_id, amount, currency
                   FROM invoice_allocations WHERE organization_id=%s AND invoice_id=%s""",
                (scope.organization_id, invoice_id),
            )
            return [_to_allocation(r) for r in rows_to_dicts(cur)]

    def prior_approved_for_sov_item(self, *, scope: Scope, sov_item_id: UUID,
                                    exclude_invoice_id: UUID) -> Decimal:
        """Sum of allocation amounts for a SOV item from invoices other than the
        one under verification. Used as PreviouslyApprovedBilling (Phase 16)."""
        with self.db.scoped(scope) as cur:
            cur.execute(
                """SELECT COALESCE(SUM(amount), 0) AS total
                   FROM invoice_allocations
                   WHERE organization_id=%s AND sov_item_id=%s AND invoice_id <> %s""",
                (scope.organization_id, sov_item_id, exclude_invoice_id),
            )
            r = row_to_dict(cur)
            return Decimal(str(r["total"])) if r else Decimal("0")


def _to_contract(row: dict[str, Any]) -> Contract:
    return Contract(
        contract_id=str(row["contract_id"]), organization_id=str(row["organization_id"]),
        project_id=str(row["project_id"]), company_id=str(row["company_id"]),
        reference=row["reference"], name=row.get("name") or "",
        base_contract_value=_dec(row.get("base_contract_value")), currency=row.get("currency") or "CAD",
        status=row.get("status") or "active",
    )


def _to_sov_item(row: dict[str, Any]) -> SOVItem:
    return SOVItem(
        sov_item_id=str(row["sov_item_id"]), organization_id=str(row["organization_id"]),
        contract_id=str(row["contract_id"]), reference=row["reference"], name=row["name"],
        base_value=_dec(row.get("base_value")), currency=row.get("currency") or "CAD",
        sort_order=int(row.get("sort_order") or 0),
    )


def _to_change_order(row: dict[str, Any]) -> ChangeOrder:
    return ChangeOrder(
        change_order_id=str(row["change_order_id"]), organization_id=str(row["organization_id"]),
        contract_id=str(row["contract_id"]), reference=row["reference"], name=row.get("name") or "",
        amount=_dec(row.get("amount")), currency=row.get("currency") or "CAD",
        status=row.get("status") or "approved",
    )


def _to_change_order_allocation(row: dict[str, Any]) -> ChangeOrderAllocation:
    return ChangeOrderAllocation(
        allocation_id=str(row["allocation_id"]),
        organization_id=str(row["organization_id"]),
        change_order_id=str(row["change_order_id"]),
        sov_item_id=str(row["sov_item_id"]),
        amount=_dec(row.get("amount")),
        currency=row.get("currency") or "CAD",
    )


def _to_allocation(row: dict[str, Any]) -> InvoiceAllocation:
    return InvoiceAllocation(
        allocation_id=str(row["allocation_id"]), organization_id=str(row["organization_id"]),
        invoice_id=str(row["invoice_id"]), sov_item_id=str(row["sov_item_id"]),
        amount=_dec(row.get("amount")), currency=row.get("currency") or "CAD",
    )
