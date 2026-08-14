from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote
from construction_ai.policy.engine import decide
from construction_ai.domain.models import Evidence, PurchaseOrder, Quote

class Transport(Protocol):
    def post(self, path: str, json: dict[str, Any]) -> Any: ...

class ReadTransport(Protocol):
    def get(self, path: str, params: dict[str, Any]|None=None) -> Any: ...

@dataclass
class ERPNextAdapter:
    transport: Transport

    def create_purchase_invoice_draft(self, payload: dict[str, Any], *, evidence_valid: bool=True, confidence_satisfied: bool=True) -> Any:
        policy=decide("PREPARE_TRANSACTION",evidence_valid=evidence_valid,confidence_satisfied=confidence_satisfied)
        if not policy.allowed: raise PermissionError(policy.reason)
        data=dict(payload); data["docstatus"]=0
        return self.transport.post("/api/resource/Purchase Invoice", json=data)

    def submit_purchase_invoice(self, *, docname: str, approval_status: str | None = None, approved_by: str|None=None, approved: bool | None = None):
        if approval_status is None: approval_status = "approved" if approved else "pending"
        approval_satisfied=approval_status=="approved" and bool(approved_by)
        policy=decide("SUBMIT_ERP_TRANSACTION",evidence_valid=True,confidence_satisfied=True,approval_satisfied=approval_satisfied)
        if not policy.allowed: raise PermissionError(policy.reason)
        return self.transport.post("/api/method/frappe.client.submit", json={"doctype":"Purchase Invoice","name":docname})

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
        rows=self._get_list('Purchase Order',[["name","=",po_number]],["name","supplier","grand_total","project"])
        if len(rows)!=1: return None,[]
        r=rows[0]; ev=[Evidence(f'EVID-ERP-PO-{po_number}-PROJECT','erpnext',po_number,'project_id',r.get('project'),1.0,0.95,organization_id=self.organization_id),Evidence(f'EVID-ERP-PO-{po_number}-AMOUNT','erpnext',po_number,'po_amount',float(r.get('grand_total') or 0),1.0,0.95,organization_id=self.organization_id)]
        return PurchaseOrder('ERP-'+po_number,self.organization_id,po_number,r.get('project') or None,r.get('supplier') or None,float(r.get('grand_total') or 0)),ev

    def resolve_quote(self, quote_number: str) -> tuple[Quote|None,list[Evidence]]:
        rows=self._get_list('Supplier Quotation',[["name","=",quote_number]],["name","supplier","grand_total","project","status"])
        if len(rows)!=1: return None,[]
        r=rows[0]; approved=str(r.get('status','')).lower() in {'submitted','ordered','approved'}
        ev=[Evidence(f'EVID-ERP-Q-{quote_number}-AMOUNT','erpnext',quote_number,'quote_amount',float(r.get('grand_total') or 0),1.0,0.95,organization_id=self.organization_id)]
        return Quote('ERP-'+quote_number,self.organization_id,quote_number,r.get('project') or None,r.get('supplier') or None,float(r.get('grand_total') or 0),approved),ev
