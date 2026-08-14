from __future__ import annotations
import csv, hashlib, io, json, mimetypes, re, zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

# -- v0.4.5 parser limits (items 28, 29) ----------------------------------
# These protect against resource exhaustion from malicious or accidentally
# large documents. A document that exceeds a limit is still extracted, but
# with a warning and truncated/limited output.

MAX_FILE_SIZE = 100 * 1024 * 1024          # 100 MB — reject larger files
MAX_EXTRACTED_TEXT = 10 * 1024 * 1024      # 10 MB of extracted text
MAX_TABLE_ROWS = 100_000                    # rows per table
MAX_TABLE_COLS = 1_000                      # columns per row
MAX_PDF_PAGES = 1_000                       # pages to extract
MAX_XML_ENTITIES = 10_000                   # XML elements to iterate
MAX_ZIP_ENTRIES = 10_000                    # entries in a zip archive
MAX_ZIP_TOTAL_SIZE = 500 * 1024 * 1024     # 500 MB total uncompressed
MAX_ZIP_COMPRESSION_RATIO = 100             # zip bomb threshold


@dataclass(frozen=True)
class ExtractedDocument:
    sha256: str
    filename: str
    mime_type: str
    text: str
    document_type: str
    tables: list[list[list[str]]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

_PATTERNS = {
  "invoice": ("invoice", "amount due", "bill to", "invoice number"),
  "quote": ("quote", "quotation", "estimate"),
  "purchase_order": ("purchase order", "po number", "p.o.", "purchase order no"),
  "change_order": ("change order", "change directive"),
  "timesheet": ("timesheet", "hours worked"),
  "receipt": ("receipt", "paid"),
  "contract": ("contract", "agreement"),
  "drawing": ("drawing", "revision", "issued for construction", "ifc"),
}

def classify(filename: str, text: str) -> str:
    hay=(filename+"\n"+text[:20000]).lower()
    scored=[(sum(1 for x in pats if x in hay),typ) for typ,pats in _PATTERNS.items()]
    score,typ=max(scored,default=(0,"unknown"))
    return typ if score else "unknown"


def _check_zip_bomb(zf: zipfile.ZipFile) -> list[str]:
    """Check for zip bomb patterns (item 29). Returns warnings if suspicious."""
    warnings = []
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        warnings.append(f"zip_too_many_entries:{len(infos)}")
    total_uncompressed = sum(i.file_size for i in infos)
    if total_uncompressed > MAX_ZIP_TOTAL_SIZE:
        warnings.append(f"zip_total_too_large:{total_uncompressed}")
    for info in infos:
        if info.compress_size > 0:
            ratio = info.file_size / info.compress_size
            if ratio > MAX_ZIP_COMPRESSION_RATIO:
                warnings.append(f"zip_high_compression_ratio:{ratio:.0f}x:{info.filename}")
                break  # one warning is enough
    return warnings


def _safe_zip_read(zf: zipfile.ZipFile, name: str, max_size: int = 50 * 1024 * 1024) -> bytes:
    """Read a zip entry with a size limit (item 29)."""
    info = zf.getinfo(name)
    if info.file_size > max_size:
        raise ValueError(f"zip entry {name} too large: {info.file_size}")
    return zf.read(name)


def _xml_text(data: bytes) -> str:
    """Extract text from XML with entity limit (item 28)."""
    try:
        root = ET.fromstring(data)
        parts = []
        count = 0
        for t in root.itertext():
            if t.strip():
                parts.append(t.strip())
                count += 1
                if count >= MAX_XML_ENTITIES:
                    break
        return " ".join(parts)
    except ET.ParseError:
        return ""


def _xlsx(path: Path):
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        tables = []; chunks = []
        for ws in wb.worksheets:
            rows = []
            row_count = 0
            for row in ws.iter_rows(values_only=True):
                if row_count >= MAX_TABLE_ROWS:
                    break
                # Truncate each row to MAX_TABLE_COLS
                vals = [("" if v is None else str(v)) for v in row][:MAX_TABLE_COLS]
                if any(vals):
                    rows.append(vals)
                    chunks.append(" | ".join(vals))
                    row_count += 1
            if rows:
                tables.append(rows)
        return "\n".join(chunks), tables, []
    except Exception as e:
        return "", [], [f"xlsx_extract_failed:{type(e).__name__}"]


def _pdf(path: Path):
    warnings = []; pages = []; tables = []
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        page_count = min(len(reader.pages), MAX_PDF_PAGES)
        if len(reader.pages) > MAX_PDF_PAGES:
            warnings.append(f"pdf_truncated_pages:{len(reader.pages)}")
        pages = [reader.pages[i].extract_text() or "" for i in range(page_count)]
    except Exception as e:
        warnings.append(f"pypdf_extract_failed:{type(e).__name__}")
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            page_count = min(len(pdf.pages), MAX_PDF_PAGES)
            for i in range(page_count):
                page = pdf.pages[i]
                for table in (page.extract_tables() or []):
                    cleaned = []
                    for row in table[:MAX_TABLE_ROWS]:
                        cleaned_row = ["" if c is None else str(c).strip() for c in row[:MAX_TABLE_COLS]]
                        if cleaned_row:
                            cleaned.append(cleaned_row)
                    if cleaned:
                        tables.append(cleaned)
    except Exception as e:
        warnings.append(f"table_extract_unavailable:{type(e).__name__}")
    text = "\n".join(pages)
    if not text.strip():
        warnings.append("no_embedded_text_route_to_document_vision_or_ocr")
    return text, tables, warnings


def _canonicalize_text(text: str) -> str:
    """Canonicalize extracted text (item 30).

    - Normalize line endings to \\n
    - Strip carriage returns
    - Collapse runs of spaces/tabs to a single space
    - Collapse runs of 3+ newlines to 2
    - Strip leading/trailing whitespace
    - Normalize Unicode to NFC
    - Truncate to MAX_EXTRACTED_TEXT
    """
    import unicodedata

    if not text:
        return ""
    # NFC normalization for consistent Unicode representation.
    text = unicodedata.normalize("NFC", text)
    # Normalize line endings: \r\n and \r → \n.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Remove null bytes and other control characters except tab and newline.
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    # Collapse runs of spaces/tabs to a single space.
    text = re.sub(r"[ \t]+", " ", text)
    # Collapse runs of 3+ newlines to 2.
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Strip leading/trailing whitespace.
    text = text.strip()
    # Truncate to the maximum extracted text size.
    if len(text) > MAX_EXTRACTED_TEXT:
        text = text[:MAX_EXTRACTED_TEXT]
    return text


def extract_document(path: str|Path) -> ExtractedDocument:
    p = Path(path)
    data = p.read_bytes()
    suffix = p.suffix.lower()
    text = ""
    tables = []
    warnings = []

    # File size limit (item 28).
    if len(data) > MAX_FILE_SIZE:
        return ExtractedDocument(
            hashlib.sha256(data).hexdigest(), p.name,
            mimetypes.guess_type(p.name)[0] or "application/octet-stream",
            "", "unknown", [], [f"file_too_large:{len(data)}"],
        )

    if suffix in {".txt", ".md"}:
        text = data.decode("utf-8", errors="replace")
    elif suffix == '.csv':
        text = data.decode('utf-8', errors='replace')
        # Limit CSV rows.
        reader = csv.reader(io.StringIO(text))
        rows = []
        for i, row in enumerate(reader):
            if i >= MAX_TABLE_ROWS:
                warnings.append(f"csv_truncated_rows:{i}")
                break
            rows.append(row[:MAX_TABLE_COLS])
        tables = [rows] if rows else []
    elif suffix == '.json':
        try:
            text = json.dumps(json.loads(data.decode('utf-8', errors='replace')), indent=2, sort_keys=True)
        except json.JSONDecodeError as e:
            warnings.append(f"json_parse_failed:{type(e).__name__}")
            text = data.decode('utf-8', errors='replace')
    elif suffix == ".xlsx":
        text, tables, warnings = _xlsx(p)
    elif suffix == ".docx":
        try:
            from docx import Document as Docx
            d = Docx(p)
            text = '\n'.join(x.text for x in d.paragraphs)
            for t in d.tables:
                table_rows = []
                for row in t.rows[:MAX_TABLE_ROWS]:
                    table_rows.append([c.text for c in row.cells[:MAX_TABLE_COLS]])
                if table_rows:
                    tables.append(table_rows)
        except Exception:
            # Fallback: direct zip/XML extraction with bomb protection.
            try:
                with zipfile.ZipFile(p) as z:
                    zip_warnings = _check_zip_bomb(z)
                    warnings.extend(zip_warnings)
                    if not any("zip_high_compression" in w or "zip_total_too_large" in w for w in zip_warnings):
                        names = [n for n in z.namelist() if n.startswith("word/") and n.endswith(".xml")]
                        text = " ".join(_xml_text(_safe_zip_read(z, n)) for n in names)
                    else:
                        warnings.append("docx_zip_bomb_suspected_extraction_skipped")
            except (zipfile.BadZipFile, ValueError) as e:
                warnings.append(f"docx_extract_failed:{type(e).__name__}")
    elif suffix == ".pdf":
        text, tables, warnings = _pdf(p)
    else:
        warnings.append('unsupported_format')

    # Canonicalize text (item 30).
    text = _canonicalize_text(text)
    mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    return ExtractedDocument(
        hashlib.sha256(data).hexdigest(), p.name, mime, text,
        classify(p.name, text), tables, warnings,
    )
