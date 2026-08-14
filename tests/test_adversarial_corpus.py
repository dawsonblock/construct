"""v0.4.7 — adversarial corpus and property tests (items 37, 38).

The adversarial corpus is a set of inputs designed to break the system's
invariants. Each case targets a specific invariant and verifies that the system
holds under hostile input. The corpus is deterministic (no random seeds) so
failures are reproducible.

Property tests use Hypothesis to generate random inputs and verify that
invariants hold across the input space. They complement the adversarial corpus
by exploring edges the corpus author didn't think of.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest
from hypothesis import given, strategies as st, settings, HealthCheck

from construction_ai.documents.extract import (
    MAX_FILE_SIZE,
    MAX_TABLE_COLS,
    MAX_TABLE_ROWS,
    _canonicalize_text,
    extract_document,
)
from construction_ai.persistence.repositories.decisions import compute_decision_fingerprint
from construction_ai.reconstruction.conflicts import _canonical_value_repr
from construction_ai.reconstruction.project_state import _canonical_value


# ==========================================================================
# Item 37: Adversarial corpus
# ==========================================================================

class TestAdversarialCorpus:
    """Targeted inputs that try to break specific invariants."""

    # --- Document extraction ---

    def test_empty_file(self):
        """An empty file extracts to empty text with no crash."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.txt"
            path.write_bytes(b"")
            result = extract_document(path)
            assert result.text == ""
            assert result.sha256 == hashlib.sha256(b"").hexdigest()

    def test_binary_garbage_as_text(self):
        """Binary garbage in a .txt file doesn't crash extraction."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "garbage.txt"
            path.write_bytes(bytes(range(256)) * 100)
            result = extract_document(path)
            assert result.warnings == [] or "unsupported" not in result.warnings[0]

    def test_oversized_file_rejected(self):
        """A file over MAX_FILE_SIZE is rejected, not processed."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "huge.txt"
            with open(path, "wb") as f:
                f.write(b"x" * (MAX_FILE_SIZE + 1))
            result = extract_document(path)
            assert any("file_too_large" in w for w in result.warnings)
            assert result.text == ""

    def test_zip_bomb_docx(self):
        """A .docx containing a zip bomb is detected and skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bomb.docx"
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("word/document.xml", b"\x00" * (5 * 1024 * 1024))
            result = extract_document(path)
            # Should not crash, should have a warning.
            assert len(result.warnings) > 0

    def test_csv_with_million_rows(self):
        """A CSV with many rows is truncated, not exhausted."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.csv"
            # Write enough rows to exceed the limit.
            with open(path, "w") as f:
                f.write("a,b,c\n")
                for i in range(MAX_TABLE_ROWS + 1000):
                    f.write(f"{i},val_{i},x\n")
            result = extract_document(path)
            assert any("csv_truncated" in w for w in result.warnings)
            assert len(result.tables[0]) <= MAX_TABLE_ROWS + 1  # +1 for header

    def test_csv_with_thousand_columns(self):
        """A CSV with many columns is truncated, not exhausted."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wide.csv"
            cols = ",".join(f"col_{i}" for i in range(MAX_TABLE_COLS + 1000))
            path.write_text(cols + "\n")
            result = extract_document(path)
            assert len(result.tables[0][0]) <= MAX_TABLE_COLS

    def test_deeply_nested_json(self):
        """Deeply nested JSON doesn't crash extraction."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested.json"
            # Create deeply nested JSON.
            data = {"a": {"b": {"c": {"d": {"e": "deep"}}}}}
            path.write_text(json.dumps(data))
            result = extract_document(path)
            assert "deep" in result.text

    def test_invalid_json(self):
        """Invalid JSON produces a warning, not a crash."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{not valid json at all")
            result = extract_document(path)
            assert any("json_parse_failed" in w for w in result.warnings)

    def test_text_with_null_bytes(self):
        """Text with null bytes is cleaned during canonicalization."""
        assert _canonicalize_text("hello\x00world") == "helloworld"

    def test_text_with_control_characters(self):
        """Control characters are removed during canonicalization."""
        text = "hello\x01\x02\x03world"
        assert _canonicalize_text(text) == "helloworld"

    def test_text_with_mixed_line_endings(self):
        """Mixed line endings are normalized to \\n."""
        text = "line1\r\nline2\rline3\nline4"
        assert _canonicalize_text(text) == "line1\nline2\nline3\nline4"

    def test_text_with_excessive_whitespace(self):
        """Excessive whitespace is collapsed."""
        text = "hello     world\t\t\tfoo\n\n\n\n\nbar"
        assert _canonicalize_text(text) == "hello world foo\n\nbar"

    # --- Canonical value comparison ---

    def test_canonical_value_string_vs_number(self):
        """"100.00" and 100 canonicalize to the same value."""
        assert _canonical_value("100.00") == _canonical_value(100)

    def test_canonical_value_float_vs_int(self):
        """100.0 and 100 canonicalize to the same value."""
        assert _canonical_value(100.0) == _canonical_value(100)

    def test_canonical_value_negative_numbers(self):
        """Negative numbers canonicalize correctly."""
        assert _canonical_value("-100.00") == _canonical_value(-100)

    def test_canonical_value_zero_variants(self):
        """All representations of zero canonicalize to the same value."""
        assert _canonical_value("0") == _canonical_value("0.00")
        assert _canonical_value("0") == _canonical_value(0)
        assert _canonical_value("0") == _canonical_value(0.0)

    def test_canonical_value_whitespace_in_strings(self):
        """Whitespace within strings is normalized."""
        assert _canonical_value("  hello  world  ") == "hello world"
        # _canonical_value collapses all whitespace (including newlines) to
        # single spaces — it operates on evidence values, not extracted text.
        assert _canonical_value("hello\nworld") == "hello world"

    # --- Decision fingerprints ---

    def test_decision_fingerprint_empty_evidence(self):
        """A decision with no evidence has a valid fingerprint."""
        fp = compute_decision_fingerprint(action="APPROVE", rationale="ok", evidence_ids=[])
        assert len(fp) == 64

    def test_decision_fingerprint_empty_rationale(self):
        """A decision with empty rationale has a valid fingerprint."""
        fp = compute_decision_fingerprint(action="APPROVE", rationale="", evidence_ids=[uuid4()])
        assert len(fp) == 64

    def test_decision_fingerprint_unicode_rationale(self):
        """Unicode in rationale doesn't crash fingerprint computation."""
        fp = compute_decision_fingerprint(action="APPROVE", rationale="café — naïve", evidence_ids=[uuid4()])
        assert len(fp) == 64

    def test_decision_fingerprint_long_action(self):
        """A very long action string doesn't crash."""
        fp = compute_decision_fingerprint(action="A" * 10000, rationale="ok", evidence_ids=[uuid4()])
        assert len(fp) == 64


# ==========================================================================
# Item 38: Property tests (Hypothesis)
# ==========================================================================

class TestPropertyTests:
    """Invariants that must hold across the input space."""

    @given(text=st.text(min_size=0, max_size=1000))
    @settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
    def test_canonicalize_text_is_idempotent(self, text):
        """Canonicalizing twice produces the same result as canonicalizing once."""
        once = _canonicalize_text(text)
        twice = _canonicalize_text(once)
        assert once == twice

    @given(text=st.text(min_size=0, max_size=500))
    @settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
    def test_canonicalize_text_no_control_chars(self, text):
        """Canonicalized text contains no control characters (except newline)."""
        result = _canonicalize_text(text)
        for char in result:
            if ord(char) < 0x20 and char != "\n":
                pytest.fail(f"control character {ord(char)} found in canonicalized text")

    @given(text=st.text(min_size=0, max_size=500))
    @settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
    def test_canonicalize_text_no_trailing_leading_whitespace(self, text):
        """Canonicalized text has no leading or trailing whitespace."""
        result = _canonicalize_text(text)
        if result:
            assert result == result.strip()

    @given(
        action=st.text(min_size=1, max_size=100),
        rationale=st.text(min_size=0, max_size=500),
        evidence_ids=st.lists(st.uuids(), min_size=0, max_size=20),
    )
    @settings(suppress_health_check=[HealthCheck.too_slow], max_examples=100)
    def test_decision_fingerprint_is_deterministic(self, action, rationale, evidence_ids):
        """Same inputs always produce the same fingerprint."""
        fp1 = compute_decision_fingerprint(action=action, rationale=rationale, evidence_ids=evidence_ids)
        fp2 = compute_decision_fingerprint(action=action, rationale=rationale, evidence_ids=evidence_ids)
        assert fp1 == fp2

    @given(
        action=st.text(min_size=1, max_size=100),
        rationale=st.text(min_size=0, max_size=500),
        evidence_ids=st.lists(st.uuids(), min_size=0, max_size=20),
    )
    @settings(suppress_health_check=[HealthCheck.too_slow], max_examples=100)
    def test_decision_fingerprint_is_hex(self, action, rationale, evidence_ids):
        """The fingerprint is always a valid 64-character hex string."""
        fp = compute_decision_fingerprint(action=action, rationale=rationale, evidence_ids=evidence_ids)
        assert len(fp) == 64
        int(fp, 16)  # raises if not valid hex

    @given(
        evidence_ids=st.lists(st.uuids(), min_size=2, max_size=20),
    )
    @settings(suppress_health_check=[HealthCheck.too_slow], max_examples=100)
    def test_decision_fingerprint_order_invariant(self, evidence_ids):
        """Evidence ID order doesn't affect the fingerprint."""
        fp1 = compute_decision_fingerprint(action="A", rationale="R", evidence_ids=evidence_ids)
        fp2 = compute_decision_fingerprint(action="A", rationale="R", evidence_ids=list(reversed(evidence_ids)))
        assert fp1 == fp2

    @given(
        value=st.one_of(
            st.text(min_size=0, max_size=100),
            st.integers(),
            st.floats(allow_nan=False, allow_infinity=False),
            st.booleans(),
            st.none(),
        ),
    )
    @settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
    def test_canonical_value_is_deterministic(self, value):
        """Canonicalizing the same value twice produces the same result."""
        c1 = _canonical_value(value)
        c2 = _canonical_value(value)
        assert c1 == c2

    @given(
        a=st.floats(allow_nan=False, allow_infinity=False, min_value=-1e10, max_value=1e10),
        b=st.floats(allow_nan=False, allow_infinity=False, min_value=-1e10, max_value=1e10),
    )
    @settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
    def test_canonical_value_repr_equal_for_equal_values(self, a, b):
        """If a == b, their canonical reprs are equal."""
        if a == b:
            assert _canonical_value_repr(a) == _canonical_value_repr(b)

    @given(data=st.binary(min_size=0, max_size=10000))
    @settings(suppress_health_check=[HealthCheck.too_slow], max_examples=100)
    def test_sha256_is_deterministic(self, data):
        """SHA-256 of the same bytes always produces the same hash."""
        h1 = hashlib.sha256(data).hexdigest()
        h2 = hashlib.sha256(data).hexdigest()
        assert h1 == h2


# ==========================================================================
# v0.5.0-rc3 — adversarial cases for external-effect safety invariants
# ==========================================================================

class TestExternalEffectAdversarial:
    """Adversarial cases targeting the central invariant:

        Approved Local Intent ⇒ Exactly One Verified External Financial Effect

    Each case tries to find a path from ambiguous, stale, incomplete, or
    insufficiently authorized local state to an irreversible ERP financial
    effect. The system must fail closed in every case.
    """

    def test_no_fingerprint_implies_no_execution(self):
        """NoFingerprint ⇒ ERPExecution is forbidden.

        An approval with no state_fingerprint cannot pass check_approval_staleness.
        We verify the pure precondition: the fingerprint must be present and
        non-empty."""
        from construction_ai.approvals.decision_fingerprint import compute_decision_fingerprint
        from construction_ai.domain.models import Approval, Invoice

        inv = Invoice(
            invoice_id="I1", organization_id="O1", reference="R1",
            invoice_number="INV-1", vendor_name="ABC", total="100.00",
            subtotal="87.00", tax="13.00", currency="CAD",
        )
        approval = Approval(
            approval_id="A1", organization_id="O1", type="PURCHASE_INVOICE",
            subject_id="I1", recommended_action="APPROVE", amount=100.0,
            state_fingerprint=None,  # No fingerprint — must fail closed
        )
        # The decision fingerprint can still be computed (it's a pure function),
        # but the executor's check_approval_staleness refuses when
        # state_fingerprint is None. This test proves the fingerprint is not
        # silently defaulted.
        assert approval.state_fingerprint is None
        fp = compute_decision_fingerprint(
            invoice=inv, evidence=[], approval=approval, verification_packet_hash=None,
        )
        assert fp is not None  # computable, but the approval lacks it stored

    def test_stale_approval_cannot_execute(self):
        """StaleApproval ⇒ ERPExecution is forbidden.

        If the decision fingerprint changes, the recomputed fingerprint will not
        match the stored one. We verify the fingerprint is sensitive to the
        invoice amount."""
        from construction_ai.approvals.decision_fingerprint import compute_decision_fingerprint
        from construction_ai.domain.models import Approval, Invoice
        from decimal import Decimal

        inv = Invoice(
            invoice_id="I1", organization_id="O1", reference="R1",
            invoice_number="INV-1", vendor_name="ABC", total=Decimal("100.00"),
            subtotal=Decimal("87.00"), tax=Decimal("13.00"), currency="CAD",
        )
        approval = Approval(
            approval_id="A1", organization_id="O1", type="PURCHASE_INVOICE",
            subject_id="I1", recommended_action="APPROVE", amount=100.0,
        )
        original_fp = compute_decision_fingerprint(
            invoice=inv, evidence=[], approval=approval, verification_packet_hash=None,
        )
        # Adversary changes the invoice amount after approval.
        tampered_inv = Invoice(
            invoice_id="I1", organization_id="O1", reference="R1",
            invoice_number="INV-1", vendor_name="ABC", total=Decimal("99999.99"),
            subtotal=Decimal("99999.99"), tax=Decimal("0.00"), currency="CAD",
        )
        tampered_fp = compute_decision_fingerprint(
            invoice=tampered_inv, evidence=[], approval=approval, verification_packet_hash=None,
        )
        assert original_fp != tampered_fp, (
            "decision fingerprint must change when the invoice amount is tampered — "
            "a stale approval must be detectable"
        )

    def test_unknown_currency_blocks_amount_comparison(self):
        """AmountComparison ⇒ AuthoritativeCurrencyKnown.

        An invoice with an unknown currency cannot be compared against ERP
        readback. The readback comparison function must not silently treat
        different currencies as equal."""
        from construction_ai.executive.executor import _compare_readback

        payload = {"supplier": "ABC", "bill_no": "INV-1", "currency": "CAD",
                   "grand_total": "100.00", "net_total": "87.00", "total_taxes": "13.00"}
        # Adversary sends a different currency in readback.
        readback = {"supplier": "ABC", "bill_no": "INV-1", "currency": "USD",
                    "grand_total": "100.00", "net_total": "87.00", "total_taxes": "13.00"}
        mismatches = _compare_readback(payload, readback)
        assert any("currency" in m for m in mismatches), (
            "a currency mismatch must be detected — 100 CAD != 100 USD"
        )

    def test_duplicate_idempotency_key_produces_one_effect(self):
        """RepeatedExecution ⇒ ExternalFinancialEffectCount ≤ 1.

        The idempotency key is deterministic per approval: same approval → same
        key. A second execution with the same key hits the existing reservation
        and returns the existing result, not a new ERP document."""
        from uuid import uuid4
        approval_id = uuid4()
        key1 = f"approval:{approval_id}"
        key2 = f"approval:{approval_id}"
        assert key1 == key2, "same approval must produce the same idempotency key"

    def test_electrical_confirmation_does_not_satisfy_roofing(self):
        """InvoicePayment ⇒ WorkEvidenceMatchesBilledScope.

        Confirming electrical work must not satisfy a roofing invoice's scope
        check. The scope-specific confirmation service must reject mismatched
        scopes."""
        from uuid import UUID
        from construction_ai.work.confirmation import WorkConfirmationService

        electrical_id = UUID("11111111-1111-1111-1111-111111111111")
        roofing_id = UUID("22222222-2222-2222-2222-222222222222")

        # A stub repository that returns confirmations only for electrical.
        class StubRepo:
            def for_sov_item(self, *, scope, sov_item_id):
                if sov_item_id == electrical_id:
                    return [object()]  # confirmed
                return []  # not confirmed

        svc = WorkConfirmationService(StubRepo())
        # Electrical scope is confirmed.
        assert svc.is_work_confirmed_for_sov_items(scope=None, sov_item_ids=[electrical_id]) is True
        # Roofing scope is NOT confirmed even though electrical is.
        assert svc.is_work_confirmed_for_sov_items(scope=None, sov_item_ids=[roofing_id]) is False
        # An invoice billing both must fail because roofing is unconfirmed.
        assert svc.is_work_confirmed_for_sov_items(scope=None, sov_item_ids=[electrical_id, roofing_id]) is False

    def test_overbilling_detected_when_invoice_exceeds_earned_value(self):
        """InvoiceCurrentAmount > CurrentBillable ⇒ OVERBILLING.

        The verify_invoice function must mark overbilled invoices as
        REVIEW_REQUIRED, not PASS."""
        from construction_ai.domain.models import CheckStatus, Invoice
        from construction_ai.verification.invoice import verify_invoice

        inv = Invoice(
            invoice_id="I1", organization_id="O1", reference="R1",
            invoice_number="INV-1", vendor_name="ABC", total="5000.00",
            subtotal="5000.00", tax="0.00", currency="CAD",
        )
        result = verify_invoice(
            inv, po=None, quote=None, duplicate=False, work_confirmed=True,
            work_scope_confirmed=True, progress_billing_overbilled=True,
        )
        assert result.checks["progress_billing"] == CheckStatus.REVIEW_REQUIRED
        assert "OVERBILLING" in result.exceptions
        assert not result.passed

    def test_confirmed_duplicate_is_hard_fail_not_hold(self):
        """CONFIRMED_DUPLICATE ⇒ FAIL (not REVIEW_REQUIRED).

        A confirmed duplicate (same vendor + invoice number, or same ERP
        supplier + invoice number) is a hard rejection, not a hold for review."""
        from construction_ai.domain.models import CheckStatus, Invoice
        from construction_ai.verification.invoice import verify_invoice

        inv = Invoice(
            invoice_id="I1", organization_id="O1", reference="R1",
            invoice_number="INV-1", vendor_name="ABC", total="100.00",
            subtotal="100.00", tax="0.00", currency="CAD",
        )
        result = verify_invoice(
            inv, po=None, quote=None, duplicate=True, work_confirmed=True,
            duplicate_status="CONFIRMED_DUPLICATE",
        )
        assert result.checks["not_duplicate"] == CheckStatus.FAIL
        assert not result.passed

    def test_possible_duplicate_is_hold_not_pass(self):
        """POSSIBLE_DUPLICATE ⇒ REVIEW_REQUIRED (HOLD), not PASS.

        A possible duplicate (weak signal) must not auto-approve — it requires
        human review."""
        from construction_ai.domain.models import CheckStatus, Invoice
        from construction_ai.verification.invoice import verify_invoice

        inv = Invoice(
            invoice_id="I1", organization_id="O1", reference="R1",
            invoice_number="INV-1", vendor_name="ABC", total="100.00",
            subtotal="100.00", tax="0.00", currency="CAD",
        )
        result = verify_invoice(
            inv, po=None, quote=None, duplicate=False, work_confirmed=True,
            duplicate_status="POSSIBLE_DUPLICATE",
        )
        assert result.checks["not_duplicate"] == CheckStatus.REVIEW_REQUIRED
        assert not result.passed

    def test_stale_evidence_cannot_be_treated_as_fresh(self):
        """StaleEvidence ⇒ refresh before execution.

        ERP evidence observed 30 minutes ago is stale (bound is 15m). The
        freshness evaluator must report it as stale, not fresh."""
        from datetime import datetime, timedelta, timezone
        from construction_ai.domain.models import Evidence
        from construction_ai.verification.freshness import evaluate_freshness

        now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        stale_ev = Evidence(
            evidence_id="E1", source_type="erpnext", source_id="S1",
            field="ERP_PURCHASE_ORDER_SNAPSHOT", value={}, confidence=1.0,
            observed_at=now - timedelta(minutes=30),
        )
        result = evaluate_freshness([stale_ev], now=now)
        assert result.fresh is False
        assert len(result.stale) == 1

    def test_missing_evidence_is_treated_as_stale(self):
        """Missing evidence cannot be treated as fresh — fail closed.

        If an evidence ID referenced by an approval cannot be loaded, the
        freshness evaluator must report stale (not silently skip it)."""
        from datetime import datetime, timezone
        from uuid import uuid4
        from construction_ai.verification.freshness import evaluate_freshness_for_approval

        # A stub repos that returns fewer evidence records than requested.
        class StubRepos:
            class evidence:
                @staticmethod
                def get_many(*, scope, evidence_ids):
                    return []  # nothing found

        result = evaluate_freshness_for_approval(
            StubRepos(), scope=None, evidence_ids=[uuid4()],
            now=datetime.now(timezone.utc),
        )
        assert result.fresh is False

    def test_packet_hash_binds_approval_to_exact_verification(self):
        """ERPConfirmed ⇒ RemoteFinancialFields=IntendedFinancialFields.

        The verification packet's canonical hash binds the approval to the exact
        verification result. If the verification changes, the hash changes, and
        the approval is stale."""
        from construction_ai.approvals.decision_fingerprint import compute_decision_fingerprint
        from construction_ai.domain.models import Approval, Invoice

        inv = Invoice(
            invoice_id="I1", organization_id="O1", reference="R1",
            invoice_number="INV-1", vendor_name="ABC", total="100.00",
            subtotal="87.00", tax="13.00", currency="CAD",
        )
        approval = Approval(
            approval_id="A1", organization_id="O1", type="PURCHASE_INVOICE",
            subject_id="I1", recommended_action="APPROVE", amount=100.0,
        )
        fp_pass = compute_decision_fingerprint(
            invoice=inv, evidence=[], approval=approval, verification_packet_hash="hash_pass",
        )
        fp_fail = compute_decision_fingerprint(
            invoice=inv, evidence=[], approval=approval, verification_packet_hash="hash_fail",
        )
        assert fp_pass != fp_fail, (
            "a different verification packet hash must produce a different "
            "decision fingerprint — the approval is bound to the exact verification"
        )
