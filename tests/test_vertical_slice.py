"""The deterministic core: resolution, verification and policy.

None of this touches persistence, and none of it should ever need to.
"""
from construction_ai.domain.models import Invoice, PurchaseOrder, Quote, Project
from construction_ai.integrations.erpnext import ERPNextAdapter
from construction_ai.policy.engine import decide
from construction_ai.resolution.project import classification_band, resolve_projects
from construction_ai.verification.invoice import verify_invoice

ORG = "ORG-1"


def test_project_resolution_prefers_hard_identifiers():
    projects = [
        Project("PRJ-0042", ORG, "Wilson Residence", "421 8th St E", company_ids=["COMP-9"], identifiers={"po": ["1042-17"]}),
        Project("PRJ-0063", ORG, "Parker Residence", "900 Main St", company_ids=["COMP-9"]),
    ]
    c = resolve_projects(projects, {"po_number": "1042-17", "address": "421 8th St E", "vendor_company_id": "COMP-9"})
    assert c[0].project_id == "PRJ-0042"
    assert c[0].confidence >= 0.98
    assert classification_band(c[0].confidence) == "automatic"


def test_fully_corroborated_invoice_verifies():
    inv = Invoice("INV-8831", ORG, "8831", "ABC Electric", 4760, 4533.33, 226.67,
                  po_number="1042-17", quote_number="Q-8821", project_id="PRJ-0042", vendor_company_id="COMP-9")
    po = PurchaseOrder("PO-1", ORG, "1042-17", "PRJ-0042", "COMP-9", 4760, "Q-8821")
    q = Quote("Q-1", ORG, "Q-8821", "PRJ-0042", "COMP-9", 4760, True)
    result = verify_invoice(inv, po, q, duplicate=False, work_confirmed=True, require_vendor_identity=True)
    assert result.passed


def test_submitting_without_approval_is_refused_by_policy():
    decision = decide("SUBMIT_ERP_TRANSACTION", evidence_valid=True, confidence_satisfied=True, approval_satisfied=False)
    assert not decision.allowed and decision.requires_human


def test_duplicate_fails_closed():
    inv = Invoice("INV-1", ORG, "1", "ABC", 100, 95, 5, po_number="PO1", quote_number="Q1", project_id="P1")
    po = PurchaseOrder("PO", ORG, "PO1", "P1", "C1", 100, "Q1")
    q = Quote("Q", ORG, "Q1", "P1", "C1", 100, True)
    result = verify_invoice(inv, po, q, duplicate=True, work_confirmed=True)
    assert not result.passed
    assert "DUPLICATE_INVOICE" in result.exceptions


class FakeTransport:
    def __init__(self): self.calls = []
    def post(self, path, json): self.calls.append((path, json)); return {"ok": True, "json": json}


def test_erp_draft_and_submit_gate():
    t = FakeTransport()
    a = ERPNextAdapter(t)
    out = a.create_purchase_invoice_draft({"supplier": "ABC", "grand_total": 100})
    assert out["json"]["docstatus"] == 0
    # v0.5.0-rc2: The adapter is now dumb — it does not check authorization.
    # Authorization is the executor's responsibility. The adapter just submits.
    result = a.submit_purchase_invoice("PINV-1")
    assert result["ok"] is True
