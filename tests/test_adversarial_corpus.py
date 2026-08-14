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
