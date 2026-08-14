"""The typed graph, deterministic reconstruction, and conflict detection.

The invariant under test throughout:

    same database state ⇒ same reconstructed project state
"""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from construction_ai.executive.invoice_pipeline import InvoicePipeline
from construction_ai.extraction.invoice import extract_invoice_deterministic
from construction_ai.graph.projection import ProjectRows, project_graph, structural_graph
from construction_ai.persistence.db import Scope, ScopeError
from construction_ai.persistence.repositories import Repositories
from tests.test_integration_pipeline import DEMO_INVOICE, StubERP


def _project_scope(org) -> Scope:
    return org["scope"].for_project(UUID(org["project"].project_id))


def _prepare_invoice(repos, org, *, erp=None, invoice_number: str | None = None):
    text = DEMO_INVOICE if invoice_number is None else DEMO_INVOICE.replace("8831", invoice_number)
    extracted, evidence, _ = extract_invoice_deterministic(
        organization_id=str(org["organization_id"]), source_id="SRC-1", text=text, filename="demo.txt"
    )
    return InvoicePipeline(repositories=repos, erp_resolver=erp or StubERP()).process(
        scope=org["scope"],
        extracted=extracted,
        signals={"po_number": "PO-1042-17", "address": org["project"].address},
        evidence=evidence,
        work_confirmed=True,
    )


# --------------------------------------------------------------------------
# Projection
# --------------------------------------------------------------------------

def test_preparing_an_invoice_projects_its_subgraph(repos, org_a):
    result = _prepare_invoice(repos, org_a)
    assert result["graph"]["nodes"] > 0

    scope = _project_scope(org_a)
    entities = repos.entities.for_project(scope=scope)
    types = {e.entity_type for e in entities}
    assert {"PROJECT", "INVOICE", "PURCHASE_ORDER", "VENDOR", "APPROVAL"} <= types

    relations = {r.relation for r in repos.relationships.for_project(scope=scope)}
    assert {"BELONGS_TO", "BILLED_BY", "REFERENCES", "APPROVES", "ORDERED_FROM"} <= relations


def test_projection_is_idempotent(repos, org_a):
    _prepare_invoice(repos, org_a)
    scope = _project_scope(org_a)

    before_nodes = len(repos.entities.for_project(scope=scope))
    before_edges = len(repos.relationships.for_project(scope=scope))
    project_graph(repos, scope)
    project_graph(repos, scope)
    assert len(repos.entities.for_project(scope=scope)) == before_nodes
    assert len(repos.relationships.for_project(scope=scope)) == before_edges


def test_structural_edges_are_a_pure_function_of_the_rows(repos, org_a):
    _prepare_invoice(repos, org_a)
    scope = _project_scope(org_a)
    rows = ProjectRows.load(repos, scope)
    assert structural_graph(rows).edges == structural_graph(rows).edges


def test_structural_edges_are_all_observed_and_approved(repos, org_a):
    """Nothing the projection writes is an inference."""
    _prepare_invoice(repos, org_a)
    for relationship in repos.relationships.for_project(scope=_project_scope(org_a)):
        assert relationship.origin == "observed"
        assert relationship.status == "approved"
        assert relationship.decided_by is None


# --------------------------------------------------------------------------
# Model proposals cannot become fact on their own
# --------------------------------------------------------------------------

def _two_entities(repos, org):
    scope = _project_scope(org)
    project_entity = repos.entities.ensure(
        scope=scope, entity_type="PROJECT", record_table="projects", record_id=UUID(org["project"].project_id), label="project"
    )
    vendor_entity = repos.entities.ensure(
        scope=scope, entity_type="VENDOR", record_table="companies", record_id=UUID(org["company"].company_id), label="vendor"
    )
    return scope, project_entity, vendor_entity


def test_a_proposal_is_not_authoritative(repos, org_a):
    scope, source, target = _two_entities(repos, org_a)
    proposed = repos.relationships.propose(
        scope=scope, source_entity_id=source.entity_id, target_entity_id=target.entity_id,
        relation="MATCHES", confidence=0.94,
    )
    assert proposed.status == "proposed"
    assert proposed.origin == "derived"
    assert proposed.is_authoritative is False


def test_ai_cannot_promote_its_own_proposal(repos, org_a):
    scope, source, target = _two_entities(repos, org_a)
    proposed = repos.relationships.propose(
        scope=scope, source_entity_id=source.entity_id, target_entity_id=target.entity_id, relation="MATCHES", confidence=0.99
    )
    with pytest.raises(PermissionError):
        repos.relationships.decide(scope=scope, relationship_id=proposed.relationship_id, status="approved", decided_by="ai")


def test_the_database_refuses_an_ai_approved_relationship(repos, org_a):
    """The CHECK, not the repository. A direct INSERT cannot get around it."""
    import psycopg

    scope, source, target = _two_entities(repos, org_a)
    with pytest.raises(psycopg.errors.CheckViolation):
        with repos.db.scoped(scope) as cur:
            cur.execute(
                """INSERT INTO relationships(organization_id, project_id, source_entity_id, target_entity_id,
                       relation, status, origin, decided_by)
                   VALUES(%s,%s,%s,%s,'MATCHES','approved','derived','ai')""",
                (org_a["organization_id"], scope.project_id, source.entity_id, target.entity_id),
            )


def test_a_human_can_promote_a_proposal(repos, org_a):
    scope, source, target = _two_entities(repos, org_a)
    proposed = repos.relationships.propose(
        scope=scope, source_entity_id=source.entity_id, target_entity_id=target.entity_id, relation="MATCHES", confidence=0.9
    )
    promoted = repos.relationships.decide(
        scope=scope, relationship_id=proposed.relationship_id, status="approved", decided_by="pm@example.com"
    )
    assert promoted.status == "approved"
    assert promoted.is_authoritative is True


def test_a_named_rule_can_promote_and_is_distinguishable_from_a_person(repos, org_a):
    scope, source, target = _two_entities(repos, org_a)
    proposed = repos.relationships.propose(
        scope=scope, source_entity_id=source.entity_id, target_entity_id=target.entity_id, relation="MATCHES", confidence=0.9
    )
    promoted = repos.relationships.promote_by_rule(
        scope=scope, relationship_id=proposed.relationship_id, rule="exact_po_match"
    )
    assert promoted.decided_by == "rule:exact_po_match"
    assert promoted.status == "approved"


def test_re_proposing_does_not_undo_a_decision(repos, org_a):
    """Re-running inference must not un-reject what a human settled."""
    scope, source, target = _two_entities(repos, org_a)
    proposed = repos.relationships.propose(
        scope=scope, source_entity_id=source.entity_id, target_entity_id=target.entity_id, relation="MATCHES", confidence=0.5
    )
    repos.relationships.decide(scope=scope, relationship_id=proposed.relationship_id, status="rejected", decided_by="pm@example.com")

    again = repos.relationships.propose(
        scope=scope, source_entity_id=source.entity_id, target_entity_id=target.entity_id, relation="MATCHES", confidence=0.99
    )
    assert again.status == "rejected"
    assert again.decided_by == "pm@example.com"
    assert again.confidence == 0.99, "the new score should still be recorded"


def test_relationships_are_tenant_scoped(repos, org_a, org_b):
    scope, source, target = _two_entities(repos, org_a)
    proposed = repos.relationships.propose(
        scope=scope, source_entity_id=source.entity_id, target_entity_id=target.entity_id, relation="MATCHES", confidence=0.9
    )
    assert repos.relationships.get(scope=org_b["scope"], relationship_id=proposed.relationship_id) is None
    assert repos.relationships.decide(
        scope=org_b["scope"], relationship_id=proposed.relationship_id, status="approved", decided_by="attacker@b"
    ) is None


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------

def test_reconstruction_requires_a_project_scope(repos, org_a):
    with pytest.raises(ScopeError, match="project-scoped"):
        repos.reconstruction.project(scope=org_a["scope"])


def test_two_reconstructions_of_the_same_state_are_identical(repos, org_a):
    _prepare_invoice(repos, org_a)
    scope = _project_scope(org_a)
    first = repos.reconstruction.project(scope=scope)
    second = repos.reconstruction.project(scope=scope)
    assert first.fingerprint() == second.fingerprint()
    assert first.as_dict() == second.as_dict()


def test_reconstruction_is_stable_across_connections(repos, org_a, database):
    """A fresh repository set over a new connection sees the same state."""
    _prepare_invoice(repos, org_a)
    scope = _project_scope(org_a)
    first = repos.reconstruction.project(scope=scope)
    second = Repositories(database).reconstruction.project(scope=scope)
    assert first.fingerprint() == second.fingerprint()


def test_reconstruction_writes_nothing(repos, org_a):
    """Reading a project must not change what reading it again returns."""
    _prepare_invoice(repos, org_a)
    scope = _project_scope(org_a)
    fingerprints = {repos.reconstruction.project(scope=scope).fingerprint() for _ in range(3)}
    assert len(fingerprints) == 1

    with repos.db.scoped(scope) as cur:
        cur.execute("SELECT count(*) FROM audit_events WHERE organization_id = %s", (org_a["organization_id"],))
        before = cur.fetchone()[0]
    repos.reconstruction.project(scope=scope)
    with repos.db.scoped(scope) as cur:
        cur.execute("SELECT count(*) FROM audit_events WHERE organization_id = %s", (org_a["organization_id"],))
        assert cur.fetchone()[0] == before


def test_changing_state_changes_the_fingerprint(repos, org_a):
    _prepare_invoice(repos, org_a)
    scope = _project_scope(org_a)
    before = repos.reconstruction.project(scope=scope).fingerprint()
    _prepare_invoice(repos, org_a, invoice_number="9002")
    assert repos.reconstruction.project(scope=scope).fingerprint() != before


def test_reconstruction_does_not_reach_across_tenants(repos, org_a, org_b):
    _prepare_invoice(repos, org_a)
    foreign = org_b["scope"].for_project(UUID(org_a["project"].project_id))
    with pytest.raises(LookupError):
        repos.reconstruction.project(scope=foreign)


def test_identical_projects_in_two_tenants_reconstruct_independently(repos, org_a, org_b):
    _prepare_invoice(repos, org_a)
    a = repos.reconstruction.project(scope=_project_scope(org_a))
    b = repos.reconstruction.project(scope=_project_scope(org_b))
    assert a.identity["reference"] == b.identity["reference"] == "PRJ-0042"
    assert a.fingerprint() != b.fingerprint()
    assert b.invoices == []


def test_the_aggregate_carries_the_sections_the_contract_promises(repos, org_a):
    _prepare_invoice(repos, org_a)
    state = repos.reconstruction.project(scope=_project_scope(org_a))
    assert state.identity["reference"] == "PRJ-0042"
    assert len(state.invoices) == 1
    assert len(state.purchase_orders) == 1
    assert len(state.quotes) == 1
    assert len(state.approvals) == 1
    assert state.vendors
    assert state.evidence
    assert state.graph.authoritative()
    assert state.provenance["counts"]["invoices"] == 1
    assert state.provenance["structural_edges_missing"] == 0


# --------------------------------------------------------------------------
# Conflicts
# --------------------------------------------------------------------------

def _conflict_types(state) -> set[str]:
    return {c["conflict_type"] for c in state.unresolved_conflicts}


def test_a_clean_project_has_no_conflicts(repos, org_a):
    _prepare_invoice(repos, org_a)
    assert repos.reconstruction.project(scope=_project_scope(org_a)).unresolved_conflicts == []


def test_an_amount_mismatch_is_reported(repos, org_a):
    _prepare_invoice(repos, org_a)
    scope = _project_scope(org_a)
    repos.purchase_orders.upsert(
        scope=scope, reference="PO-1042-17", vendor_company_id=UUID(org_a["company"].company_id), amount=4320.00
    )
    state = repos.reconstruction.project(scope=scope)
    assert "AMOUNT_MISMATCH" in _conflict_types(state)
    conflict = next(c for c in state.unresolved_conflicts if c["conflict_type"] == "AMOUNT_MISMATCH")
    assert conflict["severity"] == "high"
    assert "+440" in conflict["detail"]


def test_an_unapproved_quote_is_reported(repos, org_a):
    _prepare_invoice(repos, org_a)
    scope = _project_scope(org_a)
    repos.quotes.upsert(
        scope=scope, reference="Q-8821", vendor_company_id=UUID(org_a["company"].company_id), amount=4760.00, approved=False
    )
    assert "UNAPPROVED_QUOTE" in _conflict_types(repos.reconstruction.project(scope=scope))


def test_an_unresolvable_po_reference_is_reported(repos, org_a):
    """The invoice cites a PO that is not on this project."""
    scope = _project_scope(org_a)
    repos.invoices.create(
        scope=scope, reference="INV-ORPHAN", invoice_number="7000", vendor_name="ABC Electric",
        total=100.00, po_reference="PO-DOES-NOT-EXIST",
    )
    assert "PO_REFERENCE_UNRESOLVED" in _conflict_types(repos.reconstruction.project(scope=scope))


def test_a_stale_graph_is_reported_rather_than_hidden(repos, org_a):
    scope = _project_scope(org_a)
    repos.invoices.create(
        scope=scope, reference="INV-UNPROJECTED", invoice_number="7001", vendor_name="ABC Electric", total=100.00
    )
    state = repos.reconstruction.project(scope=scope)
    assert "GRAPH_INCOMPLETE" in _conflict_types(state)
    assert state.provenance["structural_edges_missing"] > 0

    project_graph(repos, scope)
    after = repos.reconstruction.project(scope=scope)
    assert "GRAPH_INCOMPLETE" not in _conflict_types(after)
    assert after.provenance["structural_edges_missing"] == 0


def test_two_approved_matches_from_one_source_are_a_conflict(repos, org_a):
    _prepare_invoice(repos, org_a)
    scope = _project_scope(org_a)
    invoice_entity = next(e for e in repos.entities.for_project(scope=scope) if e.entity_type == "INVOICE")

    for reference in ("PO-ALT-1", "PO-ALT-2"):
        purchase_order = repos.purchase_orders.upsert(
            scope=scope, reference=reference, vendor_company_id=UUID(org_a["company"].company_id), amount=1.00
        )
        target = repos.entities.ensure(
            scope=scope, entity_type="PURCHASE_ORDER", record_table="purchase_orders",
            record_id=UUID(purchase_order.po_id), label=reference,
        )
        proposed = repos.relationships.propose(
            scope=scope, source_entity_id=invoice_entity.entity_id, target_entity_id=target.entity_id,
            relation="MATCHES", confidence=0.9,
        )
        repos.relationships.promote_by_rule(scope=scope, relationship_id=proposed.relationship_id, rule="test")

    state = repos.reconstruction.project(scope=scope)
    assert "CONTRADICTORY_MATCH" in _conflict_types(state)
    conflict = next(c for c in state.unresolved_conflicts if c["conflict_type"] == "CONTRADICTORY_MATCH")
    assert len(conflict["related_ids"]) == 2


def test_contradictory_evidence_is_reported_not_resolved(repos, org_a):
    """Two authoritative sources disagreeing is surfaced, never silently picked."""
    scope = _project_scope(org_a)
    for value in (4760.00, 5200.00):
        repos.evidence.record(
            scope=scope, field="invoice_total", value=value, confidence=0.99, authority=0.95,
            source_type="erpnext", source_id=f"src-{value}",
        )
    state = repos.reconstruction.project(scope=scope)
    assert "EVIDENCE_CONTRADICTION" in _conflict_types(state)
    conflict = next(c for c in state.unresolved_conflicts if c["conflict_type"] == "EVIDENCE_CONTRADICTION")
    assert conflict["suggested_resolution"]
    assert len(conflict["related_ids"]) == 2


def test_two_records_sharing_a_field_name_do_not_conflict(repos, org_a):
    """A field is only comparable within the thing it describes.

    Two invoices each carrying a `total` are two invoices, not a contradiction.
    Grouping by field alone would have made every second invoice look like one.
    """
    scope = _project_scope(org_a)
    for subject, value in ((uuid4(), 4760.00), (uuid4(), 8210.00)):
        repos.evidence.record(
            scope=scope, field="total", value=value, confidence=0.99, authority=0.95,
            source_type="erpnext", source_id=str(subject), subject_type="invoice", subject_id=subject,
        )
    state = repos.reconstruction.project(scope=scope)
    assert "EVIDENCE_CONTRADICTION" not in _conflict_types(state)


def test_one_subject_with_two_authoritative_values_does_conflict(repos, org_a):
    scope = _project_scope(org_a)
    subject = uuid4()
    for source, value in (("erpnext", 4760.00), ("supplier_portal", 5200.00)):
        repos.evidence.record(
            scope=scope, field="total", value=value, confidence=0.99, authority=0.95,
            source_type=source, source_id=source, subject_type="invoice", subject_id=subject,
        )
    state = repos.reconstruction.project(scope=scope)
    assert "EVIDENCE_CONTRADICTION" in _conflict_types(state)
    conflict = next(c for c in state.unresolved_conflicts if c["conflict_type"] == "EVIDENCE_CONTRADICTION")
    assert conflict["subject_id"] == str(subject)


def test_pipeline_evidence_is_attributed_to_the_invoice(repos, org_a):
    result = _prepare_invoice(repos, org_a)
    scope = _project_scope(org_a)
    attributed = repos.evidence.for_subject(scope=scope, subject_type="invoice", subject_id=UUID(result["invoice_id"]))
    assert {e.field for e in attributed} >= {"invoice_number", "total", "po_number"}
    assert all(e.subject_type == "invoice" for e in attributed)


def test_conflicts_are_ordered_deterministically(repos, org_a):
    scope = _project_scope(org_a)
    repos.invoices.create(
        scope=scope, reference=f"INV-{uuid4().hex[:6]}", invoice_number="7100", vendor_name="ABC Electric",
        total=100.00, po_reference="PO-MISSING", quote_reference="Q-MISSING",
    )
    first = [c["conflict_type"] for c in repos.reconstruction.project(scope=scope).unresolved_conflicts]
    second = [c["conflict_type"] for c in repos.reconstruction.project(scope=scope).unresolved_conflicts]
    assert first == second
    severities = [c["severity"] for c in repos.reconstruction.project(scope=scope).unresolved_conflicts]
    assert severities == sorted(severities, key=lambda s: {"high": 0, "medium": 1, "low": 2}[s])
