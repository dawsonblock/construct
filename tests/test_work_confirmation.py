"""v0.4.2 — work completion is evidence, not a request field (item 12).

A caller cannot declare `work_confirmed` on the invoice job. The pipeline derives
it from `work_confirmations` records. Needs a real PostgreSQL.
"""
from __future__ import annotations

from uuid import UUID

from construction_ai.executive.invoice_pipeline import InvoicePipeline
from construction_ai.extraction.invoice import extract_invoice_deterministic

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
DEMO_INVOICE = (ROOT / "scripts" / "fixtures" / "demo_invoice_8831.txt").read_text()


class _ERP:
    def resolve_supplier(self, vendor_name):
        return {"name": "ABC Electric"} if vendor_name.strip() == "ABC Electric" else None

    def resolve_purchase_order(self, po_number):
        from construction_ai.domain.models import PurchaseOrder

        if po_number != "PO-1042-17":
            return None, []
        return PurchaseOrder("ERP-PO", "ERP", "PO-1042-17", "PRJ-0042", "ABC Electric", 4760.00, "Q-8821"), []

    def resolve_quote(self, quote_number):
        from construction_ai.domain.models import Quote

        if quote_number != "Q-8821":
            return None, []
        return Quote("ERP-Q", "ERP", "Q-8821", "PRJ-0042", "ABC Electric", 4760.00, True), []


def _extract():
    invoice, evidence, _ = extract_invoice_deterministic(organization_id="ORG", source_id="SRC", text=DEMO_INVOICE, filename="demo.txt")
    return invoice, evidence


def _signals(project):
    return {"po_number": "PO-1042-17", "address": project.address}


def test_without_a_work_confirmation_the_invoice_holds(repos, org_a):
    """No work_confirmations record -> work_confirmed derives to False -> HOLD."""
    extracted, evidence = _extract()
    result = InvoicePipeline(repositories=repos, erp_resolver=_ERP()).process(
        scope=org_a["scope"], extracted=extracted, signals=_signals(org_a["project"]), evidence=evidence, work_confirmed=None
    )
    assert result["recommended_action"] == "HOLD"
    assert "WORK_NOT_CONFIRMED" in result["exceptions"]


def test_a_caller_supplied_work_confirmed_is_not_the_api_path(repos, org_a):
    """The pipeline still accepts an explicit override for tests, but the HTTP API
    never sets it (item 12). An explicit True here stands in for a record."""
    extracted, evidence = _extract()
    result = InvoicePipeline(repositories=repos, erp_resolver=_ERP()).process(
        scope=org_a["scope"], extracted=extracted, signals=_signals(org_a["project"]), evidence=evidence, work_confirmed=True
    )
    assert "WORK_NOT_CONFIRMED" not in result["exceptions"]


def test_a_work_confirmation_record_lets_the_invoice_proceed(repos, org_a):
    """With a work_confirmations record for the project, the verifier reads work
    as confirmed even though the job submitted no work_confirmed field."""
    project_id = UUID(org_a["project"].project_id)
    repos.work_confirmations.record(
        scope=org_a["scope"], project_id=project_id, confirmation_type="superintendent", percent_complete=100.0
    )
    extracted, evidence = _extract()
    result = InvoicePipeline(repositories=repos, erp_resolver=_ERP()).process(
        scope=org_a["scope"], extracted=extracted, signals=_signals(org_a["project"]), evidence=evidence, work_confirmed=None
    )
    assert "WORK_NOT_CONFIRMED" not in result["exceptions"]
    assert result["recommended_action"] == "APPROVE"


def test_a_retracted_confirmation_does_not_count(repos, org_a):
    """A retracted confirmation is not active evidence."""
    project_id = UUID(org_a["project"].project_id)
    confirmation = repos.work_confirmations.record(
        scope=org_a["scope"], project_id=project_id, confirmation_type="superintendent", percent_complete=100.0
    )
    with repos.db.scoped(org_a["scope"]) as cur:
        cur.execute(
            "UPDATE work_confirmations SET status = 'retracted' WHERE organization_id = %s AND confirmation_id = %s",
            (org_a["organization_id"], confirmation.confirmation_id),
        )
    extracted, evidence = _extract()
    result = InvoicePipeline(repositories=repos, erp_resolver=_ERP()).process(
        scope=org_a["scope"], extracted=extracted, signals=_signals(org_a["project"]), evidence=evidence, work_confirmed=None
    )
    assert "WORK_NOT_CONFIRMED" in result["exceptions"]
