from __future__ import annotations
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import quote
from construction_ai.domain.models import Evidence, PurchaseOrder, Quote

ADAPTER_VERSION = "erpnext-adapter:v1"


class Transport(Protocol):
    def post(self, path: str, json: dict[str, Any]) -> Any: ...


class ReadTransport(Protocol):
    def get(self, path: str, params: dict[str, Any] | None = None) -> Any: ...


def _raw_hash(payload: Any) -> str:
    """SHA-256 of a canonical JSON rendering of the raw ERP response row.

    v0.5.0-rc2 (Phase 10): Replaced repr(sorted(payload.items())) with
    canonical JSON for deterministic hashing.
    """
    import json
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _snapshot_evidence(
    *,
    evidence_id: str,
    field: str,
    source_id: str,
    raw_row: dict[str, Any],
    normalized_fields: dict[str, Any],
    query: dict[str, Any],
    organization_id: str,
    confidence: float = 1.0,
    authority: float = 0.95,
) -> Evidence:
    """A first-class ERP observation (item 8).

    The ERP result is never an ephemeral Python value: it commits the source
    system, the query, the subject, the raw response hash, the normalized fields
    the decision will use, the retrieval timestamp, and the adapter version.
    """
    return Evidence(
        evidence_id=evidence_id,
        source_type="erpnext",
        source_id=source_id,
        field=field,
        value={
            "source_system": "ERPNext",
            "query": query,
            "subject_type": "purchase_order" if "PURCHASE_ORDER" in field else "quote",
            "subject_id": source_id,
            "raw_hash": _raw_hash(raw_row),
            "normalized_fields": normalized_fields,
            "adapter_version": ADAPTER_VERSION,
        },
        confidence=confidence,
        authority=authority,
        extractor=ADAPTER_VERSION,
        organization_id=organization_id,
        observed_at=datetime.now(timezone.utc),
    )

@dataclass
class ERPNextAdapter:
    """Dumb transport adapter for ERPNext write operations.

    v0.5.0-rc2 (Phase 13): All authorization logic removed. The adapter knows
    only how to call ERPNext. The ApprovedInvoiceExecutor knows whether it is
    permitted. This gives one authority boundary instead of two inconsistent ones.
    """
    transport: Transport

    def create_purchase_invoice_draft(self, payload: dict[str, Any]) -> Any:
        """Create a Purchase Invoice draft in ERP. No authorization checks."""
        data = dict(payload)
        data["docstatus"] = 0
        return self.transport.post("/api/resource/Purchase Invoice", json=data)

    def submit_purchase_invoice(self, docname: str) -> Any:
        """Submit a Purchase Invoice draft (docstatus 0 → 1). No authorization checks."""
        return self.transport.post(
            "/api/method/frappe.client.submit",
            json={"doctype": "Purchase Invoice", "name": docname},
        )

@dataclass
class ERPNextEvidenceResolver:
    """Exact-first read resolver. Expected transport is authenticated against ERPNext REST API."""
    transport: ReadTransport
    organization_id: str

    @staticmethod
    def _rows(data):
        if isinstance(data,dict): return data.get('data',data.get('message',[])) or []
        return data or []

    def _get_list(self, doctype: str, filters: list, fields: list[str]):
        import json
        return self._rows(self.transport.get(f"/api/resource/{quote(doctype)}",params={'filters':json.dumps(filters),'fields':json.dumps(fields),'limit_page_length':20}))

    def resolve_supplier(self, vendor_name: str):
        rows=self._get_list('Supplier',[["supplier_name","=",vendor_name]],["name","supplier_name"])
        return rows[0] if len(rows)==1 else None

    def resolve_purchase_order(self, po_number: str) -> tuple[PurchaseOrder|None,list[Evidence]]:
        # Phase 9/10: Fetch currency and complete fields for decisions.
        fields = ["name", "supplier", "grand_total", "net_total", "total_taxes", "currency", "project", "docstatus", "transaction_date"]
        query = {"doctype": "Purchase Order", "filters": [["name", "=", po_number]], "fields": fields}
        rows = self._get_list('Purchase Order', [["name", "=", po_number]], fields)
        if len(rows) != 1:
            return None, []
        r = rows[0]
        normalized = {
            "supplier_id": r.get("supplier"),
            "grand_total": r.get("grand_total"),
            "net_total": r.get("net_total"),
            "total_taxes": r.get("total_taxes"),
            "currency": r.get("currency"),
            "project": r.get("project"),
            "docstatus": r.get("docstatus"),
            "transaction_date": r.get("transaction_date"),
        }
        ev = [_snapshot_evidence(
            evidence_id=f"EVID-ERP-PO-{po_number}", field="ERP_PURCHASE_ORDER_SNAPSHOT", source_id=po_number,
            raw_row=r, normalized_fields=normalized, query=query, organization_id=self.organization_id,
        )]
        return PurchaseOrder('ERP-' + po_number, self.organization_id, po_number, r.get('project') or None, r.get('supplier') or None, float(r.get('grand_total') or 0)), ev

    def resolve_quote(self, quote_number: str) -> tuple[Quote|None,list[Evidence]]:
        # Phase 9/10: Fetch currency and complete fields for decisions.
        fields = ["name", "supplier", "grand_total", "net_total", "total_taxes", "currency", "project", "status", "docstatus", "transaction_date"]
        query = {"doctype": "Supplier Quotation", "filters": [["name", "=", quote_number]], "fields": fields}
        rows = self._get_list('Supplier Quotation', [["name", "=", quote_number]], fields)
        if len(rows) != 1:
            return None, []
        r = rows[0]
        approved = str(r.get('status', '')).lower() in {'submitted', 'ordered', 'approved'}
        normalized = {
            "supplier_id": r.get("supplier"),
            "grand_total": r.get("grand_total"),
            "net_total": r.get("net_total"),
            "total_taxes": r.get("total_taxes"),
            "currency": r.get("currency"),
            "project": r.get("project"),
            "status": r.get("status"),
            "docstatus": r.get("docstatus"),
            "transaction_date": r.get("transaction_date"),
        }
        ev = [_snapshot_evidence(
            evidence_id=f"EVID-ERP-Q-{quote_number}", field="ERP_QUOTE_SNAPSHOT", source_id=quote_number,
            raw_row=r, normalized_fields=normalized, query=query, organization_id=self.organization_id,
        )]
        return Quote('ERP-' + quote_number, self.organization_id, quote_number, r.get('project') or None, r.get('supplier') or None, float(r.get('grand_total') or 0), approved), ev
