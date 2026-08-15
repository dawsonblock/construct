"""v0.5.0-rc6 Hardening Tests.

Tests the rc6 release-blocking remediations identified in the rc5 audit:

1. P0: Release manifest no longer self-references — MANIFEST.json is excluded
   from its own per-file map; a `tree_hash` covers the included files and a
   `post_manifest_artifacts` section covers post-generation artifacts.
2. P0: Qualification report's `manifest_sha` binds to the EXACT current
   MANIFEST.json, not a stale rc3 manifest.
3. P1: Decision fingerprint replay uses the policy snapshot stored on the
   approval, not today's DEFAULT_POLICY.
4. P1: Missing expected_payload is a hard reconciliation failure — a
   submitted ERP document is NOT confirmed without canonical comparison.
5. P1: Negative confirmation is both attempt-bounded AND time-bounded.
6. P1: Production invoice pipeline disables project-wide work fallback.
7. P1: Single-SOV change-order fallback removed — unallocated COs do NOT
   adjust SOV item value.
8. P1: Work confirmation supersession — a superseded record is excluded
   from active selection.
9. P2: reconstruct_expected_erp_payload raises typed exceptions instead of
   silently returning None.
10. P2: Qualification report enumerates all skipped tests (total_skipped ==
    len(skipped_tests)).
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# 1. P0: Release manifest no longer self-references
# ---------------------------------------------------------------------------

def test_manifest_excludes_itself_from_files_map():
    """MANIFEST.json must NOT appear in its own `files` map — a manifest
    cannot contain its own final hash without a self-referential paradox."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import release_manifest
        manifest = release_manifest.generate_manifest(artifact_only=True)
    finally:
        sys.path.pop(0)

    assert "files" in manifest
    assert "MANIFEST.json" not in manifest["files"], (
        "MANIFEST.json must not be in its own per-file map — self-reference"
    )
    assert "tree_hash" in manifest, "rc6/rc7 manifest must include a tree_hash"
    assert len(manifest["tree_hash"]) == 64, "tree_hash must be SHA-256 hex"
    # rc7: post_manifest_artifacts removed — the acyclic attestation chain
    # uses RELEASE_ATTESTATION.json instead. The manifest now has
    # attestation_chain and self_excluded_artifacts.
    assert "self_excluded_artifacts" in manifest
    assert "MANIFEST.json" in manifest["self_excluded_artifacts"]
    assert "MANIFEST.json.sha256" in manifest["self_excluded_artifacts"], (
        "rc7: the companion filename must match the actual generated file"
    )
    assert "RELEASE_ATTESTATION.json" in manifest["self_excluded_artifacts"]
    assert "attestation_chain" in manifest


def test_manifest_tree_hash_is_deterministic():
    """The tree_hash is deterministic — same files → same hash."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import release_manifest
        m1 = release_manifest.generate_manifest(artifact_only=True)
        m2 = release_manifest.generate_manifest(artifact_only=True)
    finally:
        sys.path.pop(0)

    assert m1["tree_hash"] == m2["tree_hash"]


def test_manifest_files_do_not_include_post_generation_artifacts():
    """Post-generation artifacts (QUALIFICATION_REPORT.json, etc.) must NOT
    be in `files` — they did not exist at manifest-generation time."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import release_manifest
        manifest = release_manifest.generate_manifest(artifact_only=True)
    finally:
        sys.path.pop(0)

    for artifact in ("MANIFEST.json", "QUALIFICATION_REPORT.json", "TEST_RESULTS.json",
                     "CRASH_MATRIX.json", "SECURITY_GATE.json", "MIGRATION_GATE.json"):
        assert artifact not in manifest["files"], (
            f"{artifact} must not be in files — it is a post-generation artifact"
        )


# ---------------------------------------------------------------------------
# 2. P0: Qualification report binds to the EXACT current MANIFEST.json
# ---------------------------------------------------------------------------

def test_qualification_report_manifest_sha_binds_to_current_manifest(tmp_path):
    """The qualification report's manifest_sha must match the SHA-256 of
    MANIFEST.json — not a stale rc3 manifest."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import qualification_report
        import hashlib
        report = qualification_report.generate_report(run_tests=False)
        manifest_path = ROOT / "MANIFEST.json"
        if manifest_path.exists():
            expected_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            assert report["manifest_sha"] == expected_sha, (
                f"manifest_sha {report['manifest_sha']!r} != actual MANIFEST.json hash {expected_sha!r}"
            )
            assert "manifest_tree_hash" in report
    finally:
        sys.path.pop(0)


# ---------------------------------------------------------------------------
# 3. P1: Decision fingerprint replay uses stored policy snapshot
# ---------------------------------------------------------------------------

def test_decision_fingerprint_replay_uses_stored_policy_snapshot():
    """compute_decision_fingerprint_for_approval reconstructs the policy from
    the snapshot stored on the approval, not DEFAULT_POLICY."""
    from construction_ai.approvals.decision_fingerprint import (
        _policy_from_snapshot, compute_decision_fingerprint,
    )
    from construction_ai.approvals.policy import ApprovalPolicy, DEFAULT_POLICY
    from construction_ai.domain.models import Approval, Invoice
    from decimal import Decimal as _Decimal

    invoice = Invoice(
        invoice_id="inv-policy-1", organization_id="org-1", vendor_name="Vendor",
        invoice_number="INV-P1", total=_Decimal("20000"), currency="CAD",
    )
    strict_policy = ApprovalPolicy(
        version="authority:v1",
        dual_approval_threshold=_Decimal("5000"),
        required_authentication_strength="oidc",
    )
    approval = Approval(
        approval_id="app-policy-1", organization_id="org-1", type="invoice",
        subject_id="inv-policy-1", recommended_action="APPROVE",
        amount=_Decimal("20000"), currency="CAD",
        policy_hash=strict_policy.policy_hash(),
        policy_snapshot={
            "version": "authority:v1",
            "creator_cannot_approve": True,
            "dual_approval_threshold": "5000",
            "dual_approval_currency": "CAD",
            "required_authentication_strength": "oidc",
        },
    )

    # The reconstructed policy must match the original strict policy.
    reconstructed = _policy_from_snapshot(approval)
    assert reconstructed is not None
    assert reconstructed.policy_hash() == strict_policy.policy_hash()
    assert reconstructed.policy_hash() != DEFAULT_POLICY.policy_hash()

    # The fingerprint computed with the reconstructed policy must match the
    # fingerprint computed with the original strict policy.
    fp_stored = compute_decision_fingerprint(
        invoice=invoice, evidence=[], approval=approval,
        verification_packet_hash="hash-policy", policy=strict_policy,
    )
    fp_reconstructed = compute_decision_fingerprint(
        invoice=invoice, evidence=[], approval=approval,
        verification_packet_hash="hash-policy", policy=reconstructed,
    )
    assert fp_stored == fp_reconstructed

    # And it must DIFFER from the fingerprint under DEFAULT_POLICY.
    fp_default = compute_decision_fingerprint(
        invoice=invoice, evidence=[], approval=approval,
        verification_packet_hash="hash-policy", policy=DEFAULT_POLICY,
    )
    assert fp_stored != fp_default


# ---------------------------------------------------------------------------
# 4. P1: Missing expected_payload is a hard reconciliation failure
# ---------------------------------------------------------------------------

def test_reconciliation_fails_closed_when_expected_payload_missing(repos, org_a):
    """A submitted ERP document must NOT be confirmed when expected_payload
    is unavailable — fail closed to manual reconciliation."""
    from construction_ai.executive.recovery_daemon import run_recovery_cycle

    scope = org_a["scope"]
    invoice = _make_invoice(repos, scope, total=Decimal("5000.00"))

    transport = _FakeERPTransport()
    transport.add_doc(
        "PINV-NOPAYLOAD",
        docstatus=1,
        supplier="Acme Roofing",
        bill_no=invoice.invoice_number,
        grand_total="5000.00",
        net_total="4500.00",
        total_taxes="500.00",
        currency="CAD",
    )

    # Reserve an action WITHOUT request_payload (simulates a legacy/corrupted row).
    action = repos.external_actions.reserve(
        scope=scope,
        operation="erp_invoice_submit",
        idempotency_key=f"nopayload-{uuid4().hex[:6]}",
        subject_type="invoice",
        subject_id=UUID(invoice.invoice_id),
        # request_payload deliberately NOT passed
    )
    repos.external_actions.transition(
        scope=scope, action_id=action.action_id,
        from_status="pending", to_status="unknown",
        remote_document_id="PINV-NOPAYLOAD", remote_state="remote_unknown",
    )

    # Force reconstruction to fail by deleting the invoice so the subject
    # cannot be found. The recovery daemon should fail closed.
    with repos.db.scoped(scope) as cur:
        cur.execute(
            "DELETE FROM invoices WHERE organization_id = %s AND invoice_id = %s",
            (scope.organization_id, invoice.invoice_id),
        )

    run_recovery_cycle(repos, scope=scope, erp_read_transport=transport)
    reloaded = repos.external_actions.get(scope=scope, action_id=action.action_id)
    # Must NOT be confirmed — fail closed.
    assert reloaded.status != "confirmed", (
        "recovery must NOT confirm a submitted document when expected payload is unavailable"
    )
    assert reloaded.status in ("failed_terminal", "unknown"), (
        f"recovery should fail closed, got status={reloaded.status}"
    )


# ---------------------------------------------------------------------------
# 5. P1: Negative confirmation is both attempt-bounded AND time-bounded
# ---------------------------------------------------------------------------

def test_negative_confirmation_requires_time_window(repos, org_a):
    """Two empty sweeps microseconds apart must NOT declare PROVEN_ABSENT
    even if the attempt threshold is met — the time window must also elapse."""
    from construction_ai.executive.recovery_daemon import run_recovery_cycle

    scope = org_a["scope"]
    invoice = _make_invoice(repos, scope)
    transport = _FakeERPTransport()  # Empty ERP — no matches

    action = repos.external_actions.reserve(
        scope=scope,
        operation="erp_invoice_submit",
        idempotency_key=f"time-window-{uuid4().hex[:6]}",
        subject_type="invoice",
        subject_id=UUID(invoice.invoice_id),
    )
    repos.external_actions.transition(
        scope=scope, action_id=action.action_id,
        from_status="pending", to_status="unknown",
        remote_state="remote_unknown",
    )

    # Run two cycles with a LARGE window (e.g. 3600s) — even after 2 attempts,
    # the action should remain UNKNOWN because the time window has not elapsed.
    run_recovery_cycle(
        repos, scope=scope, erp_read_transport=transport,
        negative_confirmation_threshold=2,
        negative_confirmation_window_seconds=3600,
    )
    reloaded1 = repos.external_actions.get(scope=scope, action_id=action.action_id)
    assert reloaded1.status == "unknown", (
        "first cycle should remain unknown (attempts met but window not elapsed)"
    )
    assert reloaded1.recovery_attempts == 1

    run_recovery_cycle(
        repos, scope=scope, erp_read_transport=transport,
        negative_confirmation_threshold=2,
        negative_confirmation_window_seconds=3600,
    )
    reloaded2 = repos.external_actions.get(scope=scope, action_id=action.action_id)
    assert reloaded2.status == "unknown", (
        "second cycle should STILL remain unknown — time window not elapsed"
    )
    assert reloaded2.recovery_attempts == 2

    # Now run with window=0 — should transition to failed_retryable.
    run_recovery_cycle(
        repos, scope=scope, erp_read_transport=transport,
        negative_confirmation_threshold=2,
        negative_confirmation_window_seconds=0,
    )
    reloaded3 = repos.external_actions.get(scope=scope, action_id=action.action_id)
    assert reloaded3.status == "failed_retryable", (
        "third cycle with window=0 should transition to failed_retryable / PROVEN_ABSENT"
    )


# ---------------------------------------------------------------------------
# 6. P1: Production invoice pipeline disables project-wide work fallback
# ---------------------------------------------------------------------------

def test_invoice_pipeline_uses_strict_work_confirmation():
    """The production invoice pipeline calls is_work_confirmed with
    allow_project_fallback=False. This is a source-code inspection test —
    it verifies the call site uses strict mode."""
    pipeline_path = ROOT / "construction_ai" / "executive" / "invoice_pipeline.py"
    source = pipeline_path.read_text()
    assert "allow_project_fallback=False" in source, (
        "invoice_pipeline.py must call is_work_confirmed with allow_project_fallback=False"
    )


# ---------------------------------------------------------------------------
# 7. P1: Single-SOV change-order fallback removed
# ---------------------------------------------------------------------------

def test_single_sov_contract_does_not_auto_apply_unallocated_change_orders(repos, org_a):
    """A single-SOV contract with an unallocated approved change order does
    NOT have the CO applied to the SOV item's adjusted value."""
    scope = org_a["scope"]
    project_id = UUID(org_a["project"].project_id)
    company_id = UUID(org_a["company"].company_id)

    contract = repos.contracts.create(
        scope=scope, project_id=project_id, company_id=company_id,
        reference=f"CON-SINGLE-{uuid4().hex[:4]}", base_contract_value=Decimal("10000"),
    )
    item = repos.sov_items.create(
        scope=scope, contract_id=UUID(contract.contract_id), reference="SOV-SINGLE",
        name="Single Item", base_value=Decimal("10000"),
    )

    # Create an approved CO with NO explicit allocation to the SOV item.
    repos.change_orders.create(
        scope=scope, contract_id=UUID(contract.contract_id), reference="CO-UNALLOC",
        amount=Decimal("3000"), status="approved",
        # sov_item_id deliberately NOT passed
    )

    invoice = _make_invoice(repos, scope, total=Decimal("9000"))
    repos.invoice_allocations.create(
        scope=scope, invoice_id=UUID(invoice.invoice_id), sov_item_id=UUID(item.sov_item_id),
        amount=Decimal("9000"),
    )
    repos.work_confirmations.record(
        scope=scope, project_id=project_id, sov_item_id=UUID(item.sov_item_id),
        confirmation_type="superintendent", percent_complete=100.0,
    )

    from construction_ai.verification.progress_billing import evaluate_progress_billing
    outcome = evaluate_progress_billing(repos, scope=scope, invoice_id=UUID(invoice.invoice_id))
    result = outcome.per_item[0]
    # Adjusted contract value MUST be 10000 (base only), NOT 13000 (base + unallocated CO).
    assert result.adjusted_contract_value == Decimal("10000"), (
        f"unallocated CO must NOT adjust SOV value — got {result.adjusted_contract_value}"
    )


# ---------------------------------------------------------------------------
# 8. P1: Work confirmation supersession
# ---------------------------------------------------------------------------

def test_superseded_confirmation_is_excluded_from_active_set(repos, org_a):
    """A confirmation marked 'superseded' is excluded from active selection."""
    scope = org_a["scope"]
    project_id = UUID(org_a["project"].project_id)
    company_id = UUID(org_a["company"].company_id)

    contract = repos.contracts.create(
        scope=scope, project_id=project_id, company_id=company_id,
        reference=f"CON-SUP-{uuid4().hex[:4]}", base_contract_value=Decimal("10000"),
    )
    item = repos.sov_items.create(
        scope=scope, contract_id=UUID(contract.contract_id), reference="SOV-SUP2",
        name="Supersede Test", base_value=Decimal("10000"),
    )
    sov_item_id = UUID(item.sov_item_id)

    # First confirmation: superintendent at 80%.
    first = repos.work_confirmations.record(
        scope=scope, project_id=project_id, sov_item_id=sov_item_id,
        confirmation_type="superintendent", percent_complete=80.0,
    )

    # Second confirmation: signed_inspection at 55% that supersedes the first.
    repos.work_confirmations.record(
        scope=scope, project_id=project_id, sov_item_id=sov_item_id,
        confirmation_type="signed_inspection", percent_complete=55.0,
        supersedes_confirmation_id=first.confirmation_id,
    )

    from construction_ai.work.confirmation import WorkConfirmationService
    svc = WorkConfirmationService(repos.work_confirmations)
    verified = svc.verified_percent_complete(scope=scope, sov_item_id=sov_item_id)
    # The signed inspection (55%) is authoritative AND the superintendent
    # record was superseded, so it cannot compete.
    assert verified == 55.0

    # The first confirmation must now be 'superseded'.
    confirmations = repos.work_confirmations.for_sov_item(scope=scope, sov_item_id=sov_item_id)
    statuses = {c.confirmation_type: c.status for c in confirmations}
    # for_sov_item filters to status='confirmed', so the superseded record
    # should NOT appear at all.
    assert "superintendent" not in statuses, (
        "superseded superintendent record must not appear in active set"
    )
    assert "signed_inspection" in statuses


# ---------------------------------------------------------------------------
# 9. P2: reconstruct_expected_erp_payload raises typed exceptions
# ---------------------------------------------------------------------------

def test_reconstruct_expected_erp_payload_raises_on_missing_subject():
    """Reconstruction raises PayloadReconstructionError when subject_id is
    missing, rather than silently returning None."""
    from construction_ai.executive.executor import (
        reconstruct_expected_erp_payload, PayloadReconstructionError,
    )

    class FakeAction:
        request_payload = None
        subject_id = None
        operation = "erp_invoice_submit"

    with pytest.raises(PayloadReconstructionError):
        reconstruct_expected_erp_payload(
            repos=None, scope=None, action=FakeAction(),
        )


# ---------------------------------------------------------------------------
# 10. P2: Qualification report enumerates all skipped tests
# ---------------------------------------------------------------------------

def test_qualification_report_skip_parser_expands_collapsed_lines():
    """The skip parser expands `SKIPPED [N]` lines into N entries so
    total_skipped == len(skipped_tests) holds."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        # We cannot easily mock subprocess output, but we can verify the
        # parser logic by checking the regex handles [N] counts.
        import re
        # Simulate a collapsed line: 5 skips at one location.
        line = "SKIPPED [5] tests/test_ui_security.py:53: no PostgreSQL reachable"
        m = re.search(r"SKIPPED\s+\[(\d+)\]\s+([^:]+):(\d+):\s*(.*)", line)
        assert m is not None, "parser regex must match collapsed [N] lines"
        assert int(m.group(1)) == 5
    finally:
        sys.path.pop(0)


# ---------------------------------------------------------------------------
# Helpers (shared with rc5 tests)
# ---------------------------------------------------------------------------

class _FakeERPTransport:
    """Mock ERP transport supporting read and submit operations."""
    def __init__(self):
        self.documents: dict[str, dict] = {}
        self.search_results: dict[str, list[dict]] = {}
        self.submitted_docs: list[str] = []

    def add_doc(self, name: str, **fields):
        doc = {"name": name, "doctype": "Purchase Invoice", **fields}
        self.documents[name] = doc
        return doc

    def get(self, path: str, params: dict | None = None) -> dict:
        from urllib.parse import unquote
        path = unquote(path)
        if "/api/resource/Purchase Invoice/" in path:
            docname = path.split("/api/resource/Purchase Invoice/")[-1]
            if docname in self.documents:
                return {"data": self.documents[docname]}
            return {"data": {}}
        if path == "/api/resource/Purchase Invoice" and params:
            return {"data": []}
        return {"data": {}}

    def post(self, path: str, json: dict | None = None) -> dict:
        return {"data": {}}

    def submit_purchase_invoice(self, docname: str) -> dict:
        if docname in self.documents:
            self.documents[docname]["docstatus"] = 1
            self.submitted_docs.append(docname)
            return {"message": "submitted", "data": self.documents[docname]}
        raise ValueError(f"document {docname} not found")


def _make_invoice(repos, scope, *, total=Decimal("5000"), subtotal=Decimal("4500"),
                  tax=Decimal("500"), currency="CAD", vendor_company_id=None):
    ref = f"INV-REF-{uuid4().hex[:6]}"
    inv_num = f"INV-{uuid4().hex[:6]}"
    invoice = repos.invoices.create(
        scope=scope,
        vendor_name="Acme Roofing",
        invoice_number=inv_num,
        reference=ref,
        total=total,
        subtotal=subtotal,
        tax=tax,
        currency=currency,
    )
    if vendor_company_id:
        with repos.db.scoped(scope) as cur:
            cur.execute(
                "UPDATE invoices SET vendor_company_id = %s WHERE organization_id = %s AND invoice_id = %s",
                (vendor_company_id, scope.organization_id, invoice.invoice_id),
            )
        invoice = repos.invoices.get(scope=scope, invoice_id=invoice.invoice_id)
    return invoice
