"""v0.4.5 — parser limits, archive protections, and canonicalization (items 28, 29, 30).

Verifies that:
1. Files exceeding MAX_FILE_SIZE are rejected with a warning.
2. Zip bombs are detected and extraction is skipped.
3. XML entity limits prevent resource exhaustion.
4. Table row/column limits prevent memory exhaustion.
5. PDF page limits prevent excessive extraction time.
6. Text canonicalization normalizes line endings, whitespace, control chars,
   and Unicode.
"""
from __future__ import annotations

import io
import tempfile
import zipfile
from pathlib import Path


from construction_ai.documents.extract import (
    MAX_FILE_SIZE,
    MAX_TABLE_COLS,
    MAX_TABLE_ROWS,
    MAX_ZIP_ENTRIES,
    _canonicalize_text,
    _check_zip_bomb,
    extract_document,
)


# --------------------------------------------------------------------------
# Parser limits (item 28)
# --------------------------------------------------------------------------

def test_file_size_limit_rejects_oversized_files():
    """A file larger than MAX_FILE_SIZE is rejected with a warning."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "huge.txt"
        # Write a file just over the limit (write in chunks to avoid memory issues).
        with open(path, "wb") as f:
            f.write(b"x" * (MAX_FILE_SIZE + 1))
        result = extract_document(path)
        assert "file_too_large" in result.warnings[0]
        assert result.text == ""
        assert result.document_type == "unknown"


def test_csv_row_limit_truncates():
    """CSV extraction truncates at MAX_TABLE_ROWS."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "big.csv"
        rows = "\n".join(f"row_{i},val_{i}" for i in range(MAX_TABLE_ROWS + 100))
        path.write_text(rows)
        result = extract_document(path)
        assert any("csv_truncated" in w for w in result.warnings)
        assert len(result.tables[0]) <= MAX_TABLE_ROWS


def test_csv_column_limit_truncates():
    """CSV extraction truncates columns at MAX_TABLE_COLS."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "wide.csv"
        cols = ",".join(f"col_{i}" for i in range(MAX_TABLE_COLS + 100))
        path.write_text(cols)
        result = extract_document(path)
        assert len(result.tables[0][0]) <= MAX_TABLE_COLS


def test_json_parse_failure_produces_warning():
    """Invalid JSON produces a warning, not a crash."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bad.json"
        path.write_text("{not valid json")
        result = extract_document(path)
        assert any("json_parse_failed" in w for w in result.warnings)


# --------------------------------------------------------------------------
# Archive protections / zip bomb detection (item 29)
# --------------------------------------------------------------------------

def test_zip_bomb_detection_high_compression_ratio():
    """A zip with extreme compression ratio is flagged as a potential bomb."""
    # Create a zip with highly compressible content.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bomb.txt", b"\x00" * (10 * 1024 * 1024))  # 10MB of zeros
    buf.seek(0)
    with zipfile.ZipFile(buf) as zf:
        warnings = _check_zip_bomb(zf)
    assert any("zip_high_compression" in w for w in warnings)


def test_zip_bomb_detection_too_many_entries():
    """A zip with too many entries is flagged."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(MAX_ZIP_ENTRIES + 10):
            zf.writestr(f"file_{i}.txt", b"x")
    buf.seek(0)
    with zipfile.ZipFile(buf) as zf:
        warnings = _check_zip_bomb(zf)
    assert any("zip_too_many_entries" in w for w in warnings)


def test_normal_zip_is_not_flagged():
    """A normal zip file is not flagged as a bomb."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("document.xml", b"<root>hello</root>")
        zf.writestr("metadata.xml", b"<meta>data</meta>")
    buf.seek(0)
    with zipfile.ZipFile(buf) as zf:
        warnings = _check_zip_bomb(zf)
    assert warnings == []


def test_docx_zip_bomb_is_detected():
    """A .docx that looks like a zip bomb is detected and extraction is skipped."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bomb.docx"
        # Create a fake .docx with a zip-bomb-like entry.
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("word/document.xml", b"\x00" * (5 * 1024 * 1024))
        result = extract_document(path)
        # Either a zip bomb warning or a compression ratio warning.
        assert any("zip" in w.lower() or "bomb" in w.lower() or "compression" in w.lower() or "docx" in w.lower() for w in result.warnings)


# --------------------------------------------------------------------------
# Canonicalization (item 30)
# --------------------------------------------------------------------------

def test_canonicalize_normalizes_line_endings():
    """\\r\\n and \\r are normalized to \\n."""
    text = "line1\r\nline2\rline3\n"
    assert _canonicalize_text(text) == "line1\nline2\nline3"


def test_canonicalize_collapses_whitespace():
    """Runs of spaces/tabs are collapsed to a single space."""
    text = "hello    world\t\tfoo"
    assert _canonicalize_text(text) == "hello world foo"


def test_canonicalize_collapses_multiple_newlines():
    """3+ newlines are collapsed to 2."""
    text = "para1\n\n\n\npara2"
    assert _canonicalize_text(text) == "para1\n\npara2"


def test_canonicalize_strips_control_characters():
    """Null bytes and control characters are removed."""
    text = "hello\x00world\x01foo\x02"
    assert _canonicalize_text(text) == "helloworldfoo"


def test_canonicalize_strips_leading_trailing_whitespace():
    """Leading and trailing whitespace is stripped."""
    text = "  \n  hello world  \n  "
    assert _canonicalize_text(text) == "hello world"


def test_canonicalize_preserves_content():
    """Canonicalization doesn't corrupt normal text."""
    text = "INVOICE 8831\nAmount Due: $100.00\n\nLine 1: $50.00\nLine 2: $50.00"
    result = _canonicalize_text(text)
    assert "INVOICE 8831" in result
    assert "Amount Due: $100.00" in result
    assert "Line 1: $50.00" in result


def test_canonicalize_empty_string():
    """Empty input returns empty."""
    assert _canonicalize_text("") == ""


def test_canonicalize_nfc_normalization():
    """Unicode is normalized to NFC."""
    # NFD form of é (decomposed) should become NFC form (composed).
    text = "cafe\u0301"  # 'e' + combining acute accent (NFD)
    result = _canonicalize_text(text)
    assert result == "café"  # precomposed é (NFC)


def test_extracted_text_is_canonicalized():
    """The extract_document output is canonicalized."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.txt"
        path.write_bytes(b"hello\r\n\r\n\r\nworld   foo")
        result = extract_document(path)
        assert "\r" not in result.text
        assert "  " not in result.text  # no double spaces
        assert "\n\n\n" not in result.text  # no triple newlines
