"""v0.4.6 — subject-aware evidence and canonical values (items 31, 32).

Verifies that:
1. Reconstructed evidence includes subject_type and subject_id.
2. Provenance tracks subject coverage (how many evidence items have subjects).
3. Canonical value comparison: "100.00" and 100 and "100" do not conflict.
4. Canonical value normalization: whitespace, numeric strings, nested structures.
5. Evidence without subjects is flagged in provenance.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4


from construction_ai.reconstruction.conflicts import _canonical_value_repr
from construction_ai.reconstruction.project_state import _canonical_value


# --------------------------------------------------------------------------
# Canonical value comparison (item 32)
# --------------------------------------------------------------------------

def test_canonical_value_normalizes_numeric_strings():
    """"100.00", 100, and "100" all canonicalize to the same value."""
    assert _canonical_value("100.00") == _canonical_value(100)
    assert _canonical_value("100") == _canonical_value(100.0)
    assert _canonical_value("100.00") == _canonical_value("100")


def test_canonical_value_strips_strings():
    """Strings are stripped and whitespace-normalized."""
    assert _canonical_value("  hello  world  ") == "hello world"
    assert _canonical_value("hello") == "hello"


def test_canonical_value_preserves_none():
    """None stays None."""
    assert _canonical_value(None) is None


def test_canonical_value_preserves_bools():
    """Booleans are preserved (not converted to 0/1)."""
    assert _canonical_value(True) is True
    assert _canonical_value(False) is False


def test_canonical_value_handles_lists():
    """Lists are canonicalized recursively."""
    assert _canonical_value(["100.00", "hello", None]) == ["100", "hello", None]


def test_canonical_value_handles_dicts():
    """Dicts are canonicalized recursively with sorted keys."""
    result = _canonical_value({"b": "200", "a": "100.00"})
    assert result == {"a": "100", "b": "200"}


def test_canonical_value_empty_string():
    """Empty strings stay empty (not None)."""
    assert _canonical_value("") == ""


def test_canonical_value_repr_matches_for_equivalent_values():
    """The repr function used in conflict detection matches equivalent values."""
    assert _canonical_value_repr("100.00") == _canonical_value_repr(100)
    assert _canonical_value_repr("100") == _canonical_value_repr(100.0)
    assert _canonical_value_repr("  hello  ") == _canonical_value_repr("hello")


def test_canonical_value_repr_differs_for_different_values():
    """Different values produce different reprs."""
    assert _canonical_value_repr("100") != _canonical_value_repr("200")
    assert _canonical_value_repr("hello") != _canonical_value_repr("world")


# --------------------------------------------------------------------------
# Subject-aware evidence in reconstruction (item 31)
# --------------------------------------------------------------------------

def test_reconstructed_evidence_includes_subject(repos, org_a):
    """Reconstructed evidence items include subject_type and subject_id."""
    from construction_ai.reconstruction.service import ProjectReconstructor

    scope = org_a["scope"]
    project = repos.projects.create(scope=scope, reference="P-SUBJ-01", name="Subject Test")
    project_scope = scope.for_project(UUID(project.project_id))

    # Create an invoice to be the subject.
    invoice = repos.invoices.create(
        scope=project_scope,
        reference="INV-SUBJ-001",
        invoice_number="INV-SUBJ-001",
        vendor_name="Test Vendor",
        subtotal=Decimal("100.00"),
        tax=Decimal("13.00"),
        total=Decimal("113.00"),
        currency="CAD",
    )

    # Record evidence with a subject.
    repos.evidence.record(
        scope=project_scope,
        field="total",
        value="113.00",
        confidence=0.95,
        authority=0.95,
        source_type="document",
        source_id="doc-1",
        subject_type="invoice",
        subject_id=invoice.invoice_id,
    )

    recon = ProjectReconstructor.from_repositories(repos)
    state = recon.project(scope=project_scope)

    assert len(state.evidence) == 1
    ev = state.evidence[0]
    assert ev["subject_type"] == "invoice"
    assert ev["subject_id"] == str(invoice.invoice_id)


def test_reconstructed_evidence_without_subject(repos, org_a):
    """Evidence without a subject is still reconstructed, with None subject."""
    from construction_ai.reconstruction.service import ProjectReconstructor

    scope = org_a["scope"]
    project = repos.projects.create(scope=scope, reference="P-SUBJ-02", name="No Subject Test")
    project_scope = scope.for_project(UUID(project.project_id))

    repos.evidence.record(
        scope=project_scope,
        field="project_status",
        value="active",
        confidence=0.8,
        authority=0.8,
        source_type="system",
        source_id="sys-1",
        subject_type=None,
        subject_id=None,
    )

    recon = ProjectReconstructor.from_repositories(repos)
    state = recon.project(scope=project_scope)

    ev = state.evidence[0]
    assert ev["subject_type"] is None
    assert ev["subject_id"] is None


def test_provenance_tracks_subject_coverage(repos, org_a):
    """Provenance counts evidence with and without subjects."""
    from construction_ai.reconstruction.service import ProjectReconstructor

    scope = org_a["scope"]
    project = repos.projects.create(scope=scope, reference="P-SUBJ-03", name="Coverage Test")
    project_scope = scope.for_project(UUID(project.project_id))

    invoice = repos.invoices.create(
        scope=project_scope,
        reference="INV-COV-001",
        invoice_number="INV-COV-001",
        vendor_name="Vendor",
        subtotal=Decimal("100.00"),
        tax=Decimal("0"),
        total=Decimal("100.00"),
        currency="CAD",
    )

    # Evidence with subject.
    repos.evidence.record(
        scope=project_scope, field="total", value="100.00", confidence=0.9, authority=0.9,
        source_type="document", source_id="d1", subject_type="invoice", subject_id=invoice.invoice_id,
    )
    # Evidence without subject.
    repos.evidence.record(
        scope=project_scope, field="project_name", value="Coverage Test", confidence=0.8, authority=0.8,
        source_type="system", source_id="s1",
    )

    recon = ProjectReconstructor.from_repositories(repos)
    state = recon.project(scope=project_scope)

    assert state.provenance["evidence_with_subject"] == 1
    assert state.provenance["evidence_without_subject"] == 1
    assert "invoice" in state.provenance["evidence_subject_types"]


# --------------------------------------------------------------------------
# Canonical values prevent false contradictions (item 32)
# --------------------------------------------------------------------------

def test_canonical_values_prevent_false_contradiction(repos, org_a):
    """Two evidence items with the same canonical value do not conflict."""
    from construction_ai.reconstruction.conflicts import detect_conflicts
    from construction_ai.graph.projection import ProjectRows, StructuralGraph
    from construction_ai.domain.models import Evidence
    from datetime import datetime, timezone

    scope = org_a["scope"]
    project = repos.projects.create(scope=scope, reference="P-CANON-01", name="Canon Test")

    invoice_id = uuid4()
    subject = str(invoice_id)

    # Two evidence items with the same value but different representations.
    evidence = [
        Evidence(
            evidence_id="e1", source_type="document", source_id="d1",
            field="total", value="100.00", confidence=0.95, authority=0.95,
            observed_at=datetime.now(timezone.utc), extractor="deterministic",
            organization_id=str(scope.organization_id), project_id=str(project.project_id),
            subject_type="invoice", subject_id=subject,
        ),
        Evidence(
            evidence_id="e2", source_type="erp", source_id="erp1",
            field="total", value=100, confidence=0.95, authority=0.95,
            observed_at=datetime.now(timezone.utc), extractor="deterministic",
            organization_id=str(scope.organization_id), project_id=str(project.project_id),
            subject_type="invoice", subject_id=subject,
        ),
    ]

    # Minimal rows/entities/relationships for detect_conflicts.
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class FakeProject:
        project_id: str
        reference: str
        name: str

    rows = ProjectRows(
        project=FakeProject(str(project.project_id), "P-CANON-01", "Canon Test"),
        companies=[], invoices=[], purchase_orders=[], quotes=[], documents=[], approvals=[],
    )

    conflicts = detect_conflicts(
        rows=rows, entities=[], relationships=[],
        evidence=evidence, document_versions=[],
        structural=StructuralGraph(nodes={}, edges=()),
    )

    # No EVIDENCE_CONTRADICTION — the values are canonically equal.
    contradiction_conflicts = [c for c in conflicts if c.conflict_type == "EVIDENCE_CONTRADICTION"]
    assert contradiction_conflicts == []


def test_real_contradiction_still_detected(repos, org_a):
    """Genuinely different values still produce a contradiction."""
    from construction_ai.reconstruction.conflicts import detect_conflicts
    from construction_ai.graph.projection import ProjectRows, StructuralGraph
    from construction_ai.domain.models import Evidence
    from datetime import datetime, timezone
    from dataclasses import dataclass

    scope = org_a["scope"]
    project = repos.projects.create(scope=scope, reference="P-CANON-02", name="Canon Test 2")

    invoice_id = uuid4()
    subject = str(invoice_id)

    evidence = [
        Evidence(
            evidence_id="e1", source_type="document", source_id="d1",
            field="total", value="100.00", confidence=0.95, authority=0.95,
            observed_at=datetime.now(timezone.utc), extractor="deterministic",
            organization_id=str(scope.organization_id), project_id=str(project.project_id),
            subject_type="invoice", subject_id=subject,
        ),
        Evidence(
            evidence_id="e2", source_type="erp", source_id="erp1",
            field="total", value="200.00", confidence=0.95, authority=0.95,
            observed_at=datetime.now(timezone.utc), extractor="deterministic",
            organization_id=str(scope.organization_id), project_id=str(project.project_id),
            subject_type="invoice", subject_id=subject,
        ),
    ]

    @dataclass(frozen=True)
    class FakeProject:
        project_id: str
        reference: str
        name: str

    rows = ProjectRows(
        project=FakeProject(str(project.project_id), "P-CANON-02", "Canon Test 2"),
        companies=[], invoices=[], purchase_orders=[], quotes=[], documents=[], approvals=[],
    )

    conflicts = detect_conflicts(
        rows=rows, entities=[], relationships=[],
        evidence=evidence, document_versions=[],
        structural=StructuralGraph(nodes={}, edges=()),
    )

    contradiction_conflicts = [c for c in conflicts if c.conflict_type == "EVIDENCE_CONTRADICTION"]
    assert len(contradiction_conflicts) == 1
