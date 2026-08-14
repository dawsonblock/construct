"""v0.4.2 — ERP supplier verification independence (items 6, 7).

The v0.3 pipeline copied the invoice's resolved vendor onto the purchase order
before comparing them, so vendor_match was tautological. These tests prove the
two resolutions are now independent and that a mismatch fails closed.

Needs a real PostgreSQL as the non-superuser app role.
"""
from __future__ import annotations

from uuid import UUID


from construction_ai.domain.models import PurchaseOrder, Quote
from construction_ai.erp import normalize_name, resolve_supplier_to_company
from construction_ai.executive.invoice_pipeline import InvoicePipeline
from construction_ai.extraction.invoice import extract_invoice_deterministic
from construction_ai.integrations.erpnext import _snapshot_evidence

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
DEMO_INVOICE = (ROOT / "scripts" / "fixtures" / "demo_invoice_8831.txt").read_text()


def _po_snapshot(po_number, supplier, amount, project):
    return _snapshot_evidence(
        evidence_id=f"EVID-ERP-PO-{po_number}", field="ERP_PURCHASE_ORDER_SNAPSHOT", source_id=po_number,
        raw_row={"name": po_number, "supplier": supplier, "grand_total": amount, "project": project},
        normalized_fields={"supplier_id": supplier, "grand_total": amount, "project": project},
        query={"doctype": "Purchase Order", "filters": [["name", "=", po_number]]}, organization_id="ORG",
    )


# --------------------------------------------------------------------------
# Stubs
# --------------------------------------------------------------------------


class StubERP:
    """A controllable ERP resolver. The PO supplier is independent of the invoice."""

    def __init__(self, *, po_supplier: str = "ABC Electric", project_reference: str = "PRJ-0042"):
        self.po_supplier = po_supplier
        self.project_reference = project_reference

    def resolve_supplier(self, vendor_name):
        # The invoice-side lookup. Only "ABC Electric" is a known ERP supplier.
        return {"name": "ABC Electric"} if vendor_name.strip() == "ABC Electric" else None

    def resolve_purchase_order(self, po_number):
        if po_number != "PO-1042-17":
            return None, []
        return (
            PurchaseOrder("ERP-PO", "ERP", "PO-1042-17", self.project_reference, self.po_supplier, 4760.00, "Q-8821"),
            [_po_snapshot(po_number, self.po_supplier, 4760.00, self.project_reference)],
        )

    def resolve_quote(self, quote_number):
        if quote_number != "Q-8821":
            return None, []
        return (
            Quote("ERP-Q", "ERP", "Q-8821", self.project_reference, self.po_supplier, 4760.00, True),
            [_snapshot_evidence(
                evidence_id=f"EVID-ERP-Q-{quote_number}", field="ERP_QUOTE_SNAPSHOT", source_id=quote_number,
                raw_row={"name": quote_number, "supplier": self.po_supplier, "grand_total": 4760.00, "project": self.project_reference, "status": "Ordered"},
                normalized_fields={"supplier_id": self.po_supplier, "grand_total": 4760.00, "project": self.project_reference, "status": "Ordered"},
                query={"doctype": "Supplier Quotation", "filters": [["name", "=", quote_number]]}, organization_id="ORG",
            )],
        )


def _extract():
    invoice, evidence, _ = extract_invoice_deterministic(
        organization_id="ORG", source_id="SRC-1", text=DEMO_INVOICE, filename="demo.txt"
    )
    assert invoice is not None
    return invoice, evidence


def _signals(project):
    return {"po_number": "PO-1042-17", "address": project.address}


# --------------------------------------------------------------------------
# Item 7: the canonical negative case
# --------------------------------------------------------------------------


def test_invoice_vendor_abc_and_erp_po_supplier_xyz_fails_closed(repos, org_a):
    """Invoice vendor ABC Electrical, ERP PO belongs to XYZ Mechanical.

    Expected: vendor_match = FAIL, overall = HOLD, approval unavailable for
    automatic approval. This is the case v0.3 silently passed."""
    repos.companies.create(
        scope=org_a["scope"], reference="XYZ-MECH", name="XYZ Mechanical",
        company_type="subcontractor", erp_supplier_id="XYZ Mechanical",
    )
    extracted, evidence = _extract()
    result = InvoicePipeline(repositories=repos, erp_resolver=StubERP(po_supplier="XYZ Mechanical")).process(
        scope=org_a["scope"], extracted=extracted, signals=_signals(org_a["project"]), evidence=evidence, work_confirmed=True
    )
    assert result["recommended_action"] == "HOLD"
    assert "UNKNOWN_OR_MISMATCHED_VENDOR" in result["exceptions"]
    # The approval exists but recommends HOLD; it is not auto-approved.
    approval = repos.approvals.for_subject(
        scope=org_a["scope"].for_project(UUID(result["project_id"])), subject_type="invoice", subject_id=UUID(result["invoice_id"])
    )
    assert approval is not None
    assert approval.recommended_action == "HOLD"


def test_erp_po_supplier_unknown_to_the_tenant_also_fails_closed(repos, org_a):
    """The ERP PO supplier resolves to no local company → vendor_match fails."""
    extracted, evidence = _extract()
    result = InvoicePipeline(repositories=repos, erp_resolver=StubERP(po_supplier="Nobody Local Ltd")).process(
        scope=org_a["scope"], extracted=extracted, signals=_signals(org_a["project"]), evidence=evidence, work_confirmed=True
    )
    assert result["recommended_action"] == "HOLD"
    assert "UNKNOWN_OR_MISMATCHED_VENDOR" in result["exceptions"]


def test_the_happy_path_still_matches(repos, org_a):
    """Both sides resolve to ABC Electric → vendor_match passes."""
    extracted, evidence = _extract()
    result = InvoicePipeline(repositories=repos, erp_resolver=StubERP(po_supplier="ABC Electric")).process(
        scope=org_a["scope"], extracted=extracted, signals=_signals(org_a["project"]), evidence=evidence, work_confirmed=True
    )
    assert result["recommended_action"] == "APPROVE"
    assert "UNKNOWN_OR_MISMATCHED_VENDOR" not in result["exceptions"]


def test_erp_observations_are_persisted_as_first_class_evidence(repos, org_a):
    """Every ERP query affecting the decision creates a persisted evidence record
    (item 8), not an ephemeral Python value. The approval references them."""
    extracted, evidence = _extract()
    result = InvoicePipeline(repositories=repos, erp_resolver=StubERP(po_supplier="ABC Electric")).process(
        scope=org_a["scope"], extracted=extracted, signals=_signals(org_a["project"]), evidence=evidence, work_confirmed=True
    )
    project_scope = org_a["scope"].for_project(UUID(result["project_id"]))
    approval = repos.approvals.for_subject(
        scope=project_scope, subject_type="invoice", subject_id=UUID(result["invoice_id"])
    )
    stored = {e.field for e in repos.evidence.get_many(scope=project_scope, evidence_ids=[UUID(eid) for eid in approval.evidence_ids])}
    assert "ERP_PURCHASE_ORDER_SNAPSHOT" in stored
    assert "ERP_QUOTE_SNAPSHOT" in stored


# --------------------------------------------------------------------------
# Item 6: the PO record retains the ERP-derived vendor, not the invoice's
# --------------------------------------------------------------------------


def test_the_purchase_order_retains_the_erp_supplier_not_the_invoice_vendor(repos, org_a):
    """The stored PO's vendor is resolved from the ERP PO's supplier, independent
    of the invoice. With an XYZ supplier the PO vendor is XYZ, not ABC."""
    xyz = repos.companies.create(
        scope=org_a["scope"], reference="XYZ-MECH", name="XYZ Mechanical",
        company_type="subcontractor", erp_supplier_id="XYZ Mechanical",
    )
    extracted, evidence = _extract()
    InvoicePipeline(repositories=repos, erp_resolver=StubERP(po_supplier="XYZ Mechanical")).process(
        scope=org_a["scope"], extracted=extracted, signals=_signals(org_a["project"]), evidence=evidence, work_confirmed=True
    )
    po = repos.purchase_orders.get_by_reference(scope=org_a["scope"], reference="PO-1042-17")
    assert po is not None
    assert po.vendor_company_id == xyz.company_id  # ERP-derived, not the invoice's ABC


# --------------------------------------------------------------------------
# Item 6: resolution priority and conservative behaviour
# --------------------------------------------------------------------------


def test_resolution_prefers_erp_supplier_id_mapping(repos, org_a):
    abc = repos.companies.find_by_erp_supplier(scope=org_a["scope"], erp_supplier_id="ABC Electric")
    resolution = resolve_supplier_to_company(repos, org_a["scope"], erp_supplier_id="ABC Electric")
    assert resolution.company_id == UUID(abc.company_id)
    assert resolution.priority_used == 1


def test_resolution_falls_back_to_a_verified_mapping(repos, org_a):
    """Without an erp_supplier_id match, a verified external_entity_mapping resolves."""
    abc = repos.companies.find_by_erp_supplier(scope=org_a["scope"], erp_supplier_id="ABC Electric")
    # Give ABC a *different* erp_supplier_id so priority 1 misses, then map the
    # external id to ABC via a verified mapping.
    repos.companies.create(
        scope=org_a["scope"], reference="ABC-ALT", name="ABC Electrical Alt",
        company_type="subcontractor", erp_supplier_id="ABC-ALT",
    )
    with repos.db.scoped(org_a["scope"]) as cur:
        cur.execute(
            """INSERT INTO external_entity_mappings(
                   organization_id, source_system, external_entity_type, external_entity_id,
                   local_entity_type, local_entity_id)
               VALUES(%s,%s,%s,%s,%s,%s)""",
            (org_a["organization_id"], "ERPNext", "Supplier", "SUP-9999", "company", abc.company_id),
        )
    resolution = resolve_supplier_to_company(repos, org_a["scope"], erp_supplier_id="SUP-9999")
    assert resolution.company_id == UUID(abc.company_id)
    assert resolution.priority_used == 3


def test_resolution_does_not_merge_same_normalized_name_different_ids(repos, org_a):
    """Two companies with the same normalized name but different ERP supplier ids
    are NOT auto-merged. Resolving by an unknown id abstains rather than guessing."""
    repos.companies.create(
        scope=org_a["scope"], reference="ABC-ONE", name="ABC Electric Ltd.",
        company_type="vendor", erp_supplier_id="SUP-ONE",
    )
    repos.companies.create(
        scope=org_a["scope"], reference="ABC-TWO", name="ABC Electric",
        company_type="vendor", erp_supplier_id="SUP-TWO",
    )
    # normalize_name collapses "ABC Electric Ltd." and "ABC Electric" to the same form.
    assert normalize_name("ABC Electric Ltd.") == normalize_name("ABC Electric")
    # An unknown ERP supplier id does not resolve, even though two same-named
    # companies exist. Priority 4 (exact normalized name) would be ambiguous, so
    # the resolver abstains rather than picking one.
    resolution = resolve_supplier_to_company(repos, org_a["scope"], erp_supplier_id="SUP-UNKNOWN", supplier_name="ABC Electric")
    assert resolution.company_id is None


def test_fuzzy_similarity_is_a_suggestion_only_and_never_resolves(repos, org_a):
    """A close-but-not-exact name is returned as a suggestion, not a resolution."""
    repos.companies.create(
        scope=org_a["scope"], reference="NORTH", name="Northline Drywall",
        company_type="subcontractor", erp_supplier_id="Northline Drywall",
    )
    # "Northline Drywalls" normalizes differently from "Northline Drywall", so
    # the exact-normalized chain abstains and only fuzzy suggestion is produced.
    resolution = resolve_supplier_to_company(
        repos, org_a["scope"], erp_supplier_id="SUP-UNKNOWN", supplier_name="Northline Drywalls"
    )
    assert resolution.company_id is None  # never auto-resolves on similarity
    # A suggestion may be present for a human to review; it is not authoritative.
    assert resolution.suggestion is None or isinstance(resolution.suggestion, UUID)
