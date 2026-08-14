"""v0.2.0 behaviour that must survive every later refactor.

The store-backed cases from this file moved to tests/test_integration_*.py when
persistence became tenant-scoped: they need a real PostgreSQL to mean anything.
"""
import tempfile
from pathlib import Path

from construction_ai.documents.extract import extract_document
from construction_ai.domain.models import Project
from construction_ai.evaluation.adversarial import ResolutionCase, run_cases
from construction_ai.integrations.erpnext import ERPNextAdapter


def test_doc_text_classification():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / 'invoice-8831.txt'
        p.write_text('INVOICE 8831 Amount Due $100')
        e = extract_document(p)
        assert e.document_type == 'invoice' and e.text


def test_adversarial_shared_vendor_not_auto_without_hard_evidence():
    projects = [
        Project('PRJ-0042', 'O', 'Wilson', '421 8th St E', company_ids=['COMP-9'], identifiers={'po': ['1042-17']}),
        Project('PRJ-0063', 'O', 'Parker', '900 Main', company_ids=['COMP-9']),
    ]
    cases = [
        ResolutionCase('shared', 'PRJ-0042', {'vendor_company_id': 'COMP-9', 'person_active_projects': ['PRJ-0042']}, False),
        ResolutionCase('hard', 'PRJ-0042', {'po_number': '1042-17', 'address': '421 8th St E'}, True),
    ]
    r = run_cases(projects, cases)
    assert r['top1_accuracy'] == 1 and r['auto_precision'] == 1 and not r['failures']


class T:
    def __init__(self): self.calls = []
    def post(self, path, json): self.calls.append((path, json)); return {'ok': True}


def test_erp_submit_requires_real_approval_identity():
    # v0.5.0-rc2: The adapter is now dumb — it does not check authorization.
    # Authorization is the executor's responsibility. The adapter just submits.
    a = ERPNextAdapter(T())
    result = a.submit_purchase_invoice('PINV-1')
    assert result['ok'] is True
