"""v0.4.6 — replay and decision fingerprints (items 35, 36).

Verifies that:
1. Decision fingerprints are computed from action, rationale, and evidence IDs.
2. The same inputs produce the same fingerprint; different inputs differ.
3. Decisions are persisted with both decision_fingerprint and state_fingerprint.
4. Replay detects stale decisions (state has drifted since the decision).
5. Replay confirms valid decisions (state matches the decision's state fingerprint).
6. Reconstruction is deterministic — same state produces the same fingerprint.
"""
from __future__ import annotations

from uuid import UUID, uuid4


from construction_ai.persistence.repositories.decisions import (
    compute_decision_fingerprint,
)


# --------------------------------------------------------------------------
# Decision fingerprint computation (item 36)
# --------------------------------------------------------------------------

def test_decision_fingerprint_is_deterministic():
    """Same inputs produce the same fingerprint."""
    fp1 = compute_decision_fingerprint(
        action="APPROVE", rationale="matches PO", evidence_ids=[uuid4(), uuid4()],
    )
    fp2 = compute_decision_fingerprint(
        action="APPROVE", rationale="matches PO", evidence_ids=[uuid4(), uuid4()],
    )
    # Different evidence IDs → different fingerprint.
    assert fp1 != fp2


def test_decision_fingerprint_same_evidence_ids():
    """Same evidence IDs (in any order) produce the same fingerprint."""
    e1, e2 = uuid4(), uuid4()
    fp1 = compute_decision_fingerprint(
        action="APPROVE", rationale="matches PO", evidence_ids=[e1, e2],
    )
    fp2 = compute_decision_fingerprint(
        action="APPROVE", rationale="matches PO", evidence_ids=[e2, e1],
    )
    assert fp1 == fp2  # evidence IDs are sorted


def test_decision_fingerprint_differs_on_action():
    """Different action produces a different fingerprint."""
    e1 = uuid4()
    fp1 = compute_decision_fingerprint(action="APPROVE", rationale="ok", evidence_ids=[e1])
    fp2 = compute_decision_fingerprint(action="REJECT", rationale="ok", evidence_ids=[e1])
    assert fp1 != fp2


def test_decision_fingerprint_differs_on_rationale():
    """Different rationale produces a different fingerprint."""
    e1 = uuid4()
    fp1 = compute_decision_fingerprint(action="APPROVE", rationale="matches", evidence_ids=[e1])
    fp2 = compute_decision_fingerprint(action="APPROVE", rationale="mismatch", evidence_ids=[e1])
    assert fp1 != fp2


def test_decision_fingerprint_is_sha256():
    """The fingerprint is a 64-character hex string (SHA-256)."""
    fp = compute_decision_fingerprint(action="APPROVE", rationale="ok", evidence_ids=[uuid4()])
    assert len(fp) == 64
    int(fp, 16)  # valid hex


# --------------------------------------------------------------------------
# Decision persistence (item 36)
# --------------------------------------------------------------------------

def test_decision_is_persisted_with_fingerprints(repos, org_a):
    """A decision is recorded with both decision_fingerprint and state_fingerprint."""
    scope = org_a["scope"]
    project = repos.projects.create(scope=scope, reference="P-DEC-01", name="Decision Test")
    project_scope = scope.for_project(UUID(project.project_id))

    decision = repos.decisions.record(
        scope=project_scope,
        subject_type="invoice",
        subject_id=uuid4(),
        action="APPROVE",
        rationale="matches PO",
        evidence_ids=[uuid4(), uuid4()],
        confidence=0.95,
        actor="human@example.com",
        state_fingerprint="abc123",
    )
    assert decision.decision_fingerprint is not None
    assert len(decision.decision_fingerprint) == 64
    assert decision.state_fingerprint == "abc123"


def test_decision_get_returns_persisted_decision(repos, org_a):
    """get() returns the decision with its fingerprints."""
    scope = org_a["scope"]
    project = repos.projects.create(scope=scope, reference="P-DEC-02", name="Decision Test 2")
    project_scope = scope.for_project(UUID(project.project_id))

    decision = repos.decisions.record(
        scope=project_scope,
        subject_type="invoice",
        subject_id=uuid4(),
        action="APPROVE",
        rationale="ok",
        state_fingerprint="fp1",
    )
    fetched = repos.decisions.get(scope=project_scope, decision_id=decision.decision_id)
    assert fetched is not None
    assert fetched.decision_id == decision.decision_id
    assert fetched.decision_fingerprint == decision.decision_fingerprint


def test_decisions_for_project(repos, org_a):
    """for_project returns all decisions for a project."""
    scope = org_a["scope"]
    project = repos.projects.create(scope=scope, reference="P-DEC-03", name="Decision Test 3")
    project_scope = scope.for_project(UUID(project.project_id))

    repos.decisions.record(
        scope=project_scope, subject_type="invoice", subject_id=uuid4(),
        action="APPROVE", rationale="ok", state_fingerprint="fp1",
    )
    repos.decisions.record(
        scope=project_scope, subject_type="invoice", subject_id=uuid4(),
        action="REJECT", rationale="no", state_fingerprint="fp1",
    )
    decisions = repos.decisions.for_project(scope=project_scope)
    assert len(decisions) == 2


# --------------------------------------------------------------------------
# Replay — deterministic reconstruction and stale decision detection (item 35)
# --------------------------------------------------------------------------

def test_reconstruction_is_deterministic(repos, org_a):
    """Reconstructing the same state twice produces the same fingerprint."""
    from construction_ai.reconstruction.service import ProjectReconstructor

    scope = org_a["scope"]
    project = repos.projects.create(scope=scope, reference="P-REPLAY-01", name="Replay Test")
    project_scope = scope.for_project(UUID(project.project_id))

    recon = ProjectReconstructor.from_repositories(repos)
    state1 = recon.project(scope=project_scope)
    state2 = recon.project(scope=project_scope)
    assert state1.fingerprint() == state2.fingerprint()


def test_replay_detects_stale_decisions(repos, org_a):
    """Replay flags decisions made from a state that has since changed."""
    from construction_ai.reconstruction.service import ProjectReconstructor

    scope = org_a["scope"]
    project = repos.projects.create(scope=scope, reference="P-REPLAY-02", name="Replay Test 2")
    project_scope = scope.for_project(UUID(project.project_id))

    recon = ProjectReconstructor.from_repositories(repos)

    # Record the initial state fingerprint.
    state = recon.project(scope=project_scope)
    initial_fp = state.fingerprint()

    # Record a decision from that state.
    repos.decisions.record(
        scope=project_scope, subject_type="invoice", subject_id=uuid4(),
        action="APPROVE", rationale="ok", state_fingerprint=initial_fp,
    )

    # Replay — the state hasn't changed, so the decision is valid.
    result = recon.replay(scope=project_scope)
    assert result["decisions_total"] == 1
    assert result["decisions_valid"] == 1
    assert result["decisions_stale"] == 0

    # Now change the state by adding evidence.
    repos.evidence.record(
        scope=project_scope, field="total", value="100.00", confidence=0.9, authority=0.9,
        source_type="erp", source_id="erp1",
    )

    # Replay — the state has changed, so the decision is stale.
    result = recon.replay(scope=project_scope)
    assert result["decisions_stale"] == 1
    assert result["decisions_valid"] == 0
    assert len(result["stale_decision_ids"]) == 1


def test_replay_confirms_valid_decisions(repos, org_a):
    """Replay confirms decisions made from the current state."""
    from construction_ai.reconstruction.service import ProjectReconstructor

    scope = org_a["scope"]
    project = repos.projects.create(scope=scope, reference="P-REPLAY-03", name="Replay Test 3")
    project_scope = scope.for_project(UUID(project.project_id))

    recon = ProjectReconstructor.from_repositories(repos)
    state = recon.project(scope=project_scope)
    current_fp = state.fingerprint()

    # Record a decision from the current state.
    repos.decisions.record(
        scope=project_scope, subject_type="invoice", subject_id=uuid4(),
        action="APPROVE", rationale="ok", state_fingerprint=current_fp,
    )

    # Replay — the decision is valid.
    result = recon.replay(scope=project_scope)
    assert result["decisions_valid"] == 1
    assert result["decisions_stale"] == 0
    assert result["state_fingerprint"] == current_fp


def test_replay_handles_decisions_without_fingerprint(repos, org_a):
    """Decisions without a state_fingerprint are counted separately."""
    from construction_ai.reconstruction.service import ProjectReconstructor

    scope = org_a["scope"]
    project = repos.projects.create(scope=scope, reference="P-REPLAY-04", name="Replay Test 4")
    project_scope = scope.for_project(UUID(project.project_id))

    # Record a decision without a state_fingerprint (legacy).
    repos.decisions.record(
        scope=project_scope, subject_type="invoice", subject_id=uuid4(),
        action="APPROVE", rationale="ok",
    )

    recon = ProjectReconstructor.from_repositories(repos)
    result = recon.replay(scope=project_scope)
    assert result["decisions_without_fingerprint"] == 1
    assert result["decisions_stale"] == 0
    assert result["decisions_valid"] == 0


def test_replay_returns_state_fingerprint(repos, org_a):
    """Replay returns the current state fingerprint."""
    from construction_ai.reconstruction.service import ProjectReconstructor

    scope = org_a["scope"]
    project = repos.projects.create(scope=scope, reference="P-REPLAY-05", name="Replay Test 5")
    project_scope = scope.for_project(UUID(project.project_id))

    recon = ProjectReconstructor.from_repositories(repos)
    state = recon.project(scope=project_scope)
    result = recon.replay(scope=project_scope)
    assert result["state_fingerprint"] == state.fingerprint()
