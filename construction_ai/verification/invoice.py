from __future__ import annotations
from construction_ai.domain.models import Invoice, PurchaseOrder, Quote, VerificationResult

def _close(a,b,tol=0.02): return a is not None and b is not None and abs(float(a)-float(b))<=tol

def verify_invoice(invoice: Invoice, po: PurchaseOrder|None, quote: Quote|None, *, duplicate: bool, work_confirmed: bool, require_vendor_identity: bool=False) -> VerificationResult:
    checks={
      'vendor_match': bool(po and ((invoice.vendor_company_id and po.vendor_company_id==invoice.vendor_company_id) if require_vendor_identity else (not invoice.vendor_company_id or po.vendor_company_id==invoice.vendor_company_id))),
      'project_match': bool(po and invoice.project_id and po.project_id==invoice.project_id),
      'po_match': bool(po and invoice.po_number==po.po_number),
      'quote_match': bool(quote and invoice.quote_number==quote.quote_number and quote.approved),
      'amount_match': bool(po and _close(invoice.total,po.amount) and (not quote or _close(invoice.total,quote.amount))),
      'tax_math': bool(invoice.subtotal is None or invoice.tax is None or _close(invoice.subtotal+invoice.tax,invoice.total)),
      'not_duplicate': not duplicate,
      'work_confirmed': bool(work_confirmed),
    }
    exceptions=[]
    mapping={'vendor_match':'UNKNOWN_OR_MISMATCHED_VENDOR','project_match':'INVALID_PROJECT','po_match':'NO_OR_MISMATCHED_PO','quote_match':'NO_OR_UNAPPROVED_QUOTE','amount_match':'AMOUNT_MISMATCH','tax_math':'TAX_MISMATCH','not_duplicate':'DUPLICATE_INVOICE','work_confirmed':'WORK_NOT_CONFIRMED'}
    for k,v in checks.items():
        if not v: exceptions.append(mapping[k])
    return VerificationResult(invoice.invoice_id,checks,exceptions,[])
