"""v0.4.6 — observed-edge protection and provenance expansion (items 33, 34).

Verifies that:
1. AI cannot create observed edges — observe() rejects created_by='ai'.
2. Projection and named rules can create observed edges.
3. Re-projection does not overwrite a human decision on an observed edge.
4. Provenance includes expanded breakdowns: authority distribution, edge counts
   by status/origin, conflict counts by type/severity, version counts.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest



# --------------------------------------------------------------------------
# Observed-edge protection (item 33)
# --------------------------------------------------------------------------

def test_observe_rejects_ai_creator(repos, org_a):
    """observe() raises PermissionError when created_by='ai'."""
    scope = org_a["scope"]
    project = repos.projects.create(scope=scope, reference="P-EDGE-01", name="Edge Test")
    project_scope = scope.for_project(UUID(project.project_id))

    # Create two entities to connect.
    e1 = repos.entities.ensure(
        scope=project_scope, entity_type="VENDOR", record_table="companies",
        record_id=UUID("00000000-0000-0000-0000-000000000001"), label="Vendor A",
    )
    e2 = repos.entities.ensure(
        scope=project_scope, entity_type="PROJECT", record_table="projects",
        record_id=UUID(project.project_id), label="Project",
    )

    with pytest.raises(PermissionError, match="AI may not create observed edges"):
        repos.relationships.observe(
            scope=project_scope,
            source_entity_id=e1.entity_id,
            target_entity_id=e2.entity_id,
            relation="BELONGS_TO",
            created_by="ai",
        )


def test_observe_rejects_ai_producer(repos, org_a):
    """observe() raises PermissionError when producer='ai'."""
    scope = org_a["scope"]
    project = repos.projects.create(scope=scope, reference="P-EDGE-02", name="Edge Test 2")
    project_scope = scope.for_project(UUID(project.project_id))

    e1 = repos.entities.ensure(
        scope=project_scope, entity_type="VENDOR", record_table="companies",
        record_id=UUID("00000000-0000-0000-0000-000000000002"), label="Vendor B",
    )
    e2 = repos.entities.ensure(
        scope=project_scope, entity_type="PROJECT", record_table="projects",
        record_id=UUID(project.project_id), label="Project",
    )

    with pytest.raises(PermissionError, match="AI may not create observed edges"):
        repos.relationships.observe(
            scope=project_scope,
            source_entity_id=e1.entity_id,
            target_entity_id=e2.entity_id,
            relation="BELONGS_TO",
            producer="ai",
        )


def test_observe_allows_projection(repos, org_a):
    """observe() works when created_by='projection' (the default)."""
    scope = org_a["scope"]
    project = repos.projects.create(scope=scope, reference="P-EDGE-03", name="Edge Test 3")
    project_scope = scope.for_project(UUID(project.project_id))

    e1 = repos.entities.ensure(
        scope=project_scope, entity_type="VENDOR", record_table="companies",
        record_id=UUID("00000000-0000-0000-0000-000000000003"), label="Vendor C",
    )
    e2 = repos.entities.ensure(
        scope=project_scope, entity_type="PROJECT", record_table="projects",
        record_id=UUID(project.project_id), label="Project",
    )

    rel = repos.relationships.observe(
        scope=project_scope,
        source_entity_id=e1.entity_id,
        target_entity_id=e2.entity_id,
        relation="BELONGS_TO",
    )
    assert rel.status == "approved"
    assert rel.origin == "observed"


def test_reprojection_does_not_overwrite_human_decision(repos, org_a):
    """Re-projection does not overwrite a human decision on an edge."""
    scope = org_a["scope"]
    project = repos.projects.create(scope=scope, reference="P-EDGE-04", name="Edge Test 4")
    project_scope = scope.for_project(UUID(project.project_id))

    e1 = repos.entities.ensure(
        scope=project_scope, entity_type="VENDOR", record_table="companies",
        record_id=UUID("00000000-0000-0000-0000-000000000004"), label="Vendor D",
    )
    e2 = repos.entities.ensure(
        scope=project_scope, entity_type="PROJECT", record_table="projects",
        record_id=UUID(project.project_id), label="Project",
    )

    # First, AI proposes an edge.
    rel = repos.relationships.propose(
        scope=project_scope,
        source_entity_id=e1.entity_id,
        target_entity_id=e2.entity_id,
        relation="BELONGS_TO",
        confidence=0.7,
        producer="test-matcher",
    )
    assert rel.status == "proposed"

    # A human rejects it.
    rejected = repos.relationships.decide(
        scope=project_scope, relationship_id=rel.relationship_id,
        status="rejected", decided_by="human@example.com",
    )
    assert rejected is not None
    assert rejected.status == "rejected"
    assert rejected.decided_by == "human@example.com"

    # Re-projection tries to observe the same edge (upsert with the same
    # source/relation/target). The human decision must be preserved.
    re_observed = repos.relationships.observe(
        scope=project_scope,
        source_entity_id=e1.entity_id,
        target_entity_id=e2.entity_id,
        relation="BELONGS_TO",
    )
    # The human decision is preserved — re-projection does not un-reject.
    assert re_observed.status == "rejected"
    assert re_observed.decided_by == "human@example.com"


# --------------------------------------------------------------------------
# Provenance expansion (item 34)
# --------------------------------------------------------------------------

def test_provenance_includes_authority_distribution(repos, org_a):
    """Provenance includes evidence_authority_distribution."""
    from construction_ai.reconstruction.service import ProjectReconstructor

    scope = org_a["scope"]
    project = repos.projects.create(scope=scope, reference="P-PROV-01", name="Prov Test")
    project_scope = scope.for_project(UUID(project.project_id))

    # High authority evidence.
    repos.evidence.record(
        scope=project_scope, field="total", value="100.00", confidence=0.95, authority=0.95,
        source_type="erp", source_id="erp1",
    )
    # Low authority evidence.
    repos.evidence.record(
        scope=project_scope, field="guess", value="maybe", confidence=0.3, authority=0.3,
        source_type="ai", source_id="ai1",
    )

    recon = ProjectReconstructor.from_repositories(repos)
    state = recon.project(scope=project_scope)

    dist = state.provenance["evidence_authority_distribution"]
    assert dist["high"] == 1
    assert dist["low"] == 1
    assert dist["medium"] == 0


def test_provenance_includes_graph_edge_counts(repos, org_a):
    """Provenance includes graph_edges_by_status and graph_edges_by_origin."""
    from construction_ai.reconstruction.service import ProjectReconstructor

    scope = org_a["scope"]
    project = repos.projects.create(scope=scope, reference="P-PROV-02", name="Prov Test 2")
    project_scope = scope.for_project(UUID(project.project_id))

    # Create a company and invoice so projection has structural edges to create.
    company = repos.companies.create(
        scope=project_scope.organization_only, reference="V-PROV-01", name="Test Vendor", company_type="vendor",
    )
    repos.invoices.create(
        scope=project_scope, reference="INV-PROV-01", invoice_number="INV-PROV-01",
        vendor_name="Test Vendor", vendor_company_id=company.company_id,
        subtotal=Decimal("100.00"), tax=Decimal("0"), total=Decimal("100.00"), currency="CAD",
    )

    # Run projection to create observed edges.
    from construction_ai.graph.projection import project_graph
    project_graph(repos, project_scope)

    recon = ProjectReconstructor.from_repositories(repos)
    state = recon.project(scope=project_scope)

    by_status = state.provenance["graph_edges_by_status"]
    by_origin = state.provenance["graph_edges_by_origin"]
    assert "approved" in by_status
    assert by_status["approved"] > 0
    assert "observed" in by_origin
    assert by_origin["observed"] > 0


def test_provenance_includes_conflict_counts(repos, org_a):
    """Provenance includes conflicts_by_type and conflicts_by_severity."""
    from construction_ai.reconstruction.service import ProjectReconstructor

    scope = org_a["scope"]
    project = repos.projects.create(scope=scope, reference="P-PROV-03", name="Prov Test 3")
    project_scope = scope.for_project(UUID(project.project_id))

    recon = ProjectReconstructor.from_repositories(repos)
    state = recon.project(scope=project_scope)

    # Even with no conflicts, the keys should exist.
    assert "conflicts_by_type" in state.provenance
    assert "conflicts_by_severity" in state.provenance
    assert state.provenance["conflicts_by_type"] == {}
    assert state.provenance["conflicts_by_severity"] == {}


def test_provenance_includes_version_counts(repos, org_a):
    """Provenance includes document_version_counts."""
    from construction_ai.reconstruction.service import ProjectReconstructor

    scope = org_a["scope"]
    project = repos.projects.create(scope=scope, reference="P-PROV-04", name="Prov Test 4")
    project_scope = scope.for_project(UUID(project.project_id))

    recon = ProjectReconstructor.from_repositories(repos)
    state = recon.project(scope=project_scope)

    assert "document_version_counts" in state.provenance
    counts = state.provenance["document_version_counts"]
    assert "total" in counts
    assert "current" in counts
    assert "superseded" in counts
