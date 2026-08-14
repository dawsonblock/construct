"""v0.3.0 behaviour that must survive every later refactor.

Attachment ingestion, mail sync and the invoice pipeline moved to
tests/test_integration_pipeline.py: they write tenant-scoped state now.
"""
import base64

from construction_ai.evaluation.benchmark import run
from construction_ai.ingestion.connectors import GmailConnector, StaticTokenProvider
from construction_ai.integrations.erpnext import ERPNextEvidenceResolver


class JSONTransport:
    def __init__(self, data): self.data = data; self.calls = []
    def get(self, url, headers=None, params=None):
        self.calls.append((url, params))
        key = url.split('/')[-1]
        return self.data[key] if key in self.data else self.data.get('default', {})


def test_gmail_live_connector_parses_and_lists_attachment():
    body = base64.urlsafe_b64encode(b'Invoice attached').decode().rstrip('=')
    m = {'id': 'm1', 'threadId': 't1', 'internalDate': '1786636800000', 'payload': {
        'headers': [{'name': 'From', 'value': 'bob@abc.ca'}, {'name': 'To', 'value': 'pm@co.ca'}, {'name': 'Subject', 'value': 'Invoice'}],
        'parts': [{'mimeType': 'text/plain', 'body': {'data': body}},
                  {'filename': 'inv.pdf', 'mimeType': 'application/pdf', 'body': {'attachmentId': 'a1'}}]}}
    t = JSONTransport({'m1': m})
    c, a = GmailConnector(StaticTokenProvider('x'), t).fetch_message('m1', 'ORG')
    assert c.sender == 'bob@abc.ca' and a[0]['attachment_id'] == 'a1' and 'Invoice attached' in c.body


class ERPRead:
    def get(self, path, params=None):
        if 'Purchase%20Order' in path:
            return {'data': [{'name': 'PO-0042', 'supplier': 'COMP-9', 'grand_total': 4760, 'project': 'PRJ-0042'}]}
        if 'Supplier%20Quotation' in path:
            return {'data': [{'name': 'Q-8821', 'supplier': 'COMP-9', 'grand_total': 4760, 'project': 'PRJ-0042', 'status': 'Submitted'}]}
        return {'data': []}


def test_erp_evidence_resolution_carries_provenance():
    r = ERPNextEvidenceResolver(ERPRead(), 'ORG')
    po, po_evidence = r.resolve_purchase_order('PO-0042')
    quote, quote_evidence = r.resolve_quote('Q-8821')
    assert po.project_id == 'PRJ-0042' and po.amount == 4760
    assert quote.approved is True
    assert {e.field for e in po_evidence + quote_evidence} == {'project_id', 'po_amount', 'quote_amount'}
    assert all(e.authority >= 0.9 for e in po_evidence + quote_evidence)


def test_benchmark_has_no_unsafe_automatic_assignments():
    r = run(n_cases=300)
    assert r.top1_accuracy == 1.0
    assert r.auto_precision == 1.0
    assert r.unsafe_auto == 0
