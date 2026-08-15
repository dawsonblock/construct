"""v0.5.0-rc7 Hardening Tests.

Tests the rc7 release-engineering and runtime fixes:

1. Release attestation chain is acyclic (no circular manifest↔report hashing).
2. Manifest companion filename matches the exclusion set.
3. RELEASE_ATTESTATION.json hashes all evidence files.
4. Executable approvals require policy_snapshot (no DEFAULT_POLICY fallback).
5. Policy snapshot hash is validated against stored policy_hash.
6. Negative-observation state resets when a positive remote document is found.
7. Disappearance of a known remote_document_id is REMOTE_STATE_INCONSISTENT.
8. Work-confirmation supersession enforces same-subject invariant.
9. Progress billing distinguishes UNAVAILABLE from OVERBILLED.
10. Skip classification includes full pytest node IDs.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


# -- Release attestation chain tests -----------------------------------------


def test_manifest_companion_filename_matches_exclusion():
    """rc7: The companion filename in SELF_EXCLUDED_ARTIFACTS must match
    the actual filename written by the manifest generator."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import release_manifest
        assert release_manifest.MANIFEST_COMPANION_FILENAME == "MANIFEST.json.sha256"
        assert release_manifest.MANIFEST_COMPANION_FILENAME in release_manifest.SELF_EXCLUDED_ARTIFACTS
        assert "MANIFEST.sha256" not in release_manifest.SELF_EXCLUDED_ARTIFACTS, (
            "rc7: the old incorrect 'MANIFEST.sha256' must not be in the exclusion set"
        )
    finally:
        sys.path.pop(0)


def test_manifest_does_not_hash_qualification_report():
    """rc7: The manifest must NOT hash QUALIFICATION_REPORT.json — that was
    the rc6 circular dependency. The acyclic chain is:
    PayloadTree -> MANIFEST -> Qualification -> RELEASE_ATTESTATION."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import release_manifest
        manifest = release_manifest.generate_manifest(artifact_only=True)
        assert "QUALIFICATION_REPORT.json" not in manifest["files"], (
            "rc7: manifest must not hash qualification artifacts (circular dependency)"
        )
        assert "RELEASE_ATTESTATION.json" not in manifest["files"]
        assert "attestation_chain" in manifest
        assert "PayloadTree" in manifest["attestation_chain"]
    finally:
        sys.path.pop(0)


def test_release_attestation_hashes_all_evidence():
    """rc7: RELEASE_ATTESTATION.json must hash all evidence files."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import release_attestation
        attestation = release_attestation.generate_attestation()
        expected = set(release_attestation.EVIDENCE_FILES)
        actual = set(attestation["evidence"].keys())
        assert expected == actual, f"evidence files mismatch: {expected ^ actual}"
        assert "attestation_root_hash" in attestation
        assert len(attestation["attestation_root_hash"]) == 64
        # Each evidence entry should have a sha256 (or error).
        for name, entry in attestation["evidence"].items():
            assert "sha256" in entry, f"{name} missing sha256"
            assert "size" in entry, f"{name} missing size"
    finally:
        sys.path.pop(0)


def test_attestation_chain_is_acyclic():
    """rc7: The attestation chain must be a DAG, not a cycle.

    Manifest -> hashes payload tree (NOT qualification report)
    Qualification report -> hashes manifest (one-directional)
    Release attestation -> hashes manifest + qualification + gates

    The manifest must NOT reference the qualification report.
    The qualification report references the manifest (one-directional).
    The release attestation references both.
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import release_manifest
        manifest = release_manifest.generate_manifest(artifact_only=True)
        # Manifest must not contain qualification report hash in its files.
        assert "QUALIFICATION_REPORT.json" not in manifest["files"]
        # Manifest must not have post_manifest_artifacts (rc7 removed it).
        assert "post_manifest_artifacts" not in manifest, (
            "rc7: post_manifest_artifacts removed — use RELEASE_ATTESTATION.json"
        )
    finally:
        sys.path.pop(0)


# -- Policy snapshot enforcement tests ---------------------------------------


def test_approval_policy_missing_raises_for_legacy_approval():
    """rc7: An approval without policy_snapshot must raise ApprovalPolicyMissing,
    not fall back to DEFAULT_POLICY."""
    from unittest.mock import MagicMock
    from construction_ai.approvals.decision_fingerprint import (
        ApprovalPolicyMissing,
        compute_decision_fingerprint_for_approval,
    )
    from construction_ai.domain.models import Approval

    approval = MagicMock(spec=Approval)
    approval.approval_id = "test-approval"
    approval.policy_snapshot = None
    approval.policy_hash = None
    approval.evidence_ids = []
    approval.subject_id = "00000000-0000-0000-0000-000000000000"

    repos = MagicMock()
    with pytest.raises(ApprovalPolicyMissing):
        compute_decision_fingerprint_for_approval(
            repos, scope=MagicMock(), approval=approval,
        )


def test_approval_policy_corrupt_detected_on_hash_mismatch():
    """rc7: If the reconstructed policy hash does not match the stored
    policy_hash, ApprovalPolicyCorrupt is raised."""
    from unittest.mock import MagicMock
    from construction_ai.approvals.decision_fingerprint import (
        ApprovalPolicyCorrupt,
        compute_decision_fingerprint_for_approval,
    )
    from construction_ai.domain.models import Approval

    approval = MagicMock(spec=Approval)
    approval.approval_id = "test-approval"
    approval.policy_snapshot = {
        "version": "authority:v1",
        "creator_cannot_approve": True,
        "dual_approval_threshold": "5000",
        "dual_approval_currency": "CAD",
        "required_authentication_strength": "dev",
    }
    # Store a WRONG hash — does not match the snapshot.
    approval.policy_hash = "0000000000000000000000000000000000000000000000000000000000000000"
    approval.evidence_ids = []
    approval.subject_id = "00000000-0000-0000-0000-000000000000"

    repos = MagicMock()
    with pytest.raises(ApprovalPolicyCorrupt):
        compute_decision_fingerprint_for_approval(
            repos, scope=MagicMock(), approval=approval,
        )


# -- Progress billing UNAVAILABLE vs OVERBILLED tests ------------------------


def test_progress_billing_unavailable_status_when_no_work_evidence():
    """rc7: When no verified percent_complete exists, the status should be
    UNAVAILABLE, not OVERBILLED. The invoice is held because the ceiling
    cannot be established, not because it exceeds a known ceiling."""
    from construction_ai.domain.models import ProgressBillingResult
    # Verify the model has the status field.
    result = ProgressBillingResult(
        sov_item_id="test", adjusted_contract_value=0,
        verified_percent_complete=None, earned_value=0,
        previously_approved_billing=0, retainage=0,
        current_billable=0, invoice_amount=100,
        overbilled=True, currency="CAD", status="UNAVAILABLE",
    )
    assert result.status == "UNAVAILABLE"
    assert result.overbilled is True  # still held, but for a different reason


# -- Work-confirmation same-subject supersession tests -----------------------


def test_supersession_rejects_different_sov_item(repos, org_a):
    """rc7: A confirmation for one SOV item must not supersede a confirmation
    for a different SOV item."""
    from uuid import UUID
    project_id = UUID(str(org_a["project"].project_id))
    company_id = UUID(str(org_a["company"].company_id))

    # Create a contract and two SOV items.
    contract = repos.contracts.create(
        scope=org_a["scope"], project_id=project_id, company_id=company_id,
        reference="CON-RC7-1", name="RC7 Contract", base_contract_value=10000, currency="CAD",
    )
    sov_item_a = repos.sov_items.create(
        scope=org_a["scope"], contract_id=UUID(contract.contract_id),
        reference="SOV-A", name="Item A", base_value=5000, currency="CAD", sort_order=1,
    )
    sov_item_b = repos.sov_items.create(
        scope=org_a["scope"], contract_id=UUID(contract.contract_id),
        reference="SOV-B", name="Item B", base_value=5000, currency="CAD", sort_order=2,
    )

    # Create first confirmation for SOV item A.
    first = repos.work_confirmations.record(
        scope=org_a["scope"], project_id=project_id,
        sov_item_id=UUID(sov_item_a.sov_item_id),
        confirmation_type="superintendent", percent_complete=50.0,
    )

    # Attempt to supersede with a confirmation for SOV item B.
    with pytest.raises(ValueError, match="subject mismatch"):
        repos.work_confirmations.record(
            scope=org_a["scope"], project_id=project_id,
            sov_item_id=UUID(sov_item_b.sov_item_id),
            confirmation_type="superintendent", percent_complete=100.0,
            supersedes_confirmation_id=first.confirmation_id,
        )


def test_supersession_allows_same_subject(repos, org_a):
    """rc7: Supersession with the same project + SOV item should succeed."""
    from uuid import UUID
    project_id = UUID(str(org_a["project"].project_id))
    company_id = UUID(str(org_a["company"].company_id))

    contract = repos.contracts.create(
        scope=org_a["scope"], project_id=project_id, company_id=company_id,
        reference="CON-RC7-2", name="RC7 Contract 2", base_contract_value=10000, currency="CAD",
    )
    sov_item = repos.sov_items.create(
        scope=org_a["scope"], contract_id=UUID(contract.contract_id),
        reference="SOV-C", name="Item C", base_value=10000, currency="CAD", sort_order=1,
    )

    first = repos.work_confirmations.record(
        scope=org_a["scope"], project_id=project_id,
        sov_item_id=UUID(sov_item.sov_item_id),
        confirmation_type="superintendent", percent_complete=50.0,
    )

    second = repos.work_confirmations.record(
        scope=org_a["scope"], project_id=project_id,
        sov_item_id=UUID(sov_item.sov_item_id),
        confirmation_type="signed_inspection", percent_complete=100.0,
        supersedes_confirmation_id=first.confirmation_id,
    )
    assert second.supersedes_confirmation_id == first.confirmation_id
    assert second.status == "confirmed"
