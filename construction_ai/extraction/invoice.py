from __future__ import annotations
import re
from uuid import uuid4
from construction_ai.domain.models import Invoice, Evidence

MONEY=re.compile(r'\$?\s*([0-9][0-9,]*\.\d{2})')

# Document identifiers must contain a digit. Without that, the bare "INVOICE"
# heading every invoice carries is captured as the invoice number.
IDENT=r'((?=[A-Z0-9-]*\d)[A-Z0-9-]+)'

def _money_after(labels: str | list[str], text: str):
    """First money value following one of `labels`, tried in priority order.

    Labels are word-anchored: without the boundary, 'total' matches inside
    'Subtotal' and the subtotal is read as the invoice total.
    """
    if isinstance(labels,str): labels=[labels]
    for label in labels:
        m=re.search(r'\b(?:'+label+r')\b\s*[:#-]?\s*\$?\s*([0-9][0-9,]*\.\d{2})',text,re.I)
        if m: return float(m.group(1).replace(',',''))
    return None

def extract_invoice_deterministic(*, organization_id: str, source_id: str, text: str, filename: str=''):
    """Conservative extraction for common text invoices. Missing required fields remain unresolved.
    A structured LLM/document model can be added behind the same schema, but may not fabricate fields.
    """
    invno=None
    for pat in [r'invoice\s*(?:number|no\.?|#)?\s*[:#-]?\s*'+IDENT,r'inv\s*#\s*'+IDENT]:
        m=re.search(pat,text,re.I)
        if m: invno=m.group(1); break
    po=None; m=re.search(r'(?:purchase\s*order|p\.?o\.?)\s*(?:number|no\.?|#)?\s*[:#-]?\s*'+IDENT,text,re.I)
    if m: po=m.group(1)
    quote=None; m=re.search(r'(?:quote|quotation)\s*(?:number|no\.?|#)?\s*[:#-]?\s*'+IDENT,text,re.I)
    if m: quote=m.group(1)
    subtotal=_money_after(r'sub\s*-?\s*total',text)
    tax=_money_after([r'gst\s*/\s*hst',r'gst',r'hst',r'pst',r'sales\s+tax',r'tax'],text)
    total=_money_after([r'amount\s+due',r'grand\s+total',r'total\s+due',r'invoice\s+total',r'total'],text)
    vendor=''
    lines=[x.strip() for x in text.splitlines() if x.strip()]
    if lines: vendor=lines[0][:120]
    ev=[]
    fields={'invoice_number':invno,'po_number':po,'quote_number':quote,'subtotal':subtotal,'tax':tax,'total':total,'vendor_name':vendor or None}
    for k,v in fields.items():
        if v is not None:
            ev.append(Evidence('EVID-'+uuid4().hex[:16],'document',source_id,k,v,0.97 if k in {'invoice_number','po_number','quote_number'} else 0.90,0.75,extractor='invoice_regex_v1',organization_id=organization_id))
    if not invno or total is None:
        return None,ev,['invoice_number_missing' if not invno else None,'total_missing' if total is None else None]
    warnings=[]
    inv=Invoice('INV-'+uuid4().hex[:16],organization_id,invno,vendor,float(total),subtotal,tax,po_number=po,quote_number=quote,source_id=source_id)
    return inv,ev,warnings
