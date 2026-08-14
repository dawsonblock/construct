from __future__ import annotations
import csv, hashlib, io, json, mimetypes, re, zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

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

def _xml_text(data: bytes) -> str:
    try:
        root=ET.fromstring(data)
        return " ".join(t.strip() for t in root.itertext() if t.strip())
    except ET.ParseError: return ""

def _xlsx(path: Path):
    try:
        import openpyxl
        wb=openpyxl.load_workbook(path,read_only=True,data_only=True)
        tables=[]; chunks=[]
        for ws in wb.worksheets:
            rows=[]
            for row in ws.iter_rows(values_only=True):
                vals=["" if v is None else str(v) for v in row]
                if any(vals): rows.append(vals); chunks.append(" | ".join(vals))
            if rows: tables.append(rows)
        return "\n".join(chunks),tables,[]
    except Exception as e:
        return "",[],[f"xlsx_extract_failed:{type(e).__name__}"]

def _pdf(path: Path):
    warnings=[]; pages=[]; tables=[]
    try:
        from pypdf import PdfReader
        pages=[page.extract_text() or "" for page in PdfReader(str(path)).pages]
    except Exception as e:
        warnings.append(f"pypdf_extract_failed:{type(e).__name__}")
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for table in (page.extract_tables() or []):
                    cleaned=[["" if c is None else str(c).strip() for c in row] for row in table]
                    if cleaned: tables.append(cleaned)
    except Exception as e:
        warnings.append(f"table_extract_unavailable:{type(e).__name__}")
    text="\n".join(pages)
    if not text.strip(): warnings.append("no_embedded_text_route_to_document_vision_or_ocr")
    return text,tables,warnings

def extract_document(path: str|Path) -> ExtractedDocument:
    p=Path(path); data=p.read_bytes(); suffix=p.suffix.lower(); text=""; tables=[]; warnings=[]
    if suffix in {".txt",".md"}:
        text=data.decode("utf-8",errors="replace")
    elif suffix=='.csv':
        text=data.decode('utf-8',errors='replace')
        tables=[[row for row in csv.reader(io.StringIO(text))]]
    elif suffix=='.json':
        text=json.dumps(json.loads(data.decode('utf-8',errors='replace')),indent=2,sort_keys=True)
    elif suffix==".xlsx":
        text,tables,warnings=_xlsx(p)
    elif suffix==".docx":
        try:
            from docx import Document as Docx
            d=Docx(p); text='\n'.join(x.text for x in d.paragraphs)
            for t in d.tables:
                tables.append([[c.text for c in row.cells] for row in t.rows])
        except Exception:
            with zipfile.ZipFile(p) as z:
                names=[n for n in z.namelist() if n.startswith("word/") and n.endswith(".xml")]
                text=" ".join(_xml_text(z.read(n)) for n in names)
    elif suffix==".pdf":
        text,tables,warnings=_pdf(p)
    else:
        warnings.append('unsupported_format')
    text=re.sub(r"[ \t]+"," ",text); text=re.sub(r"\n{3,}","\n\n",text).strip()
    mime=mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    return ExtractedDocument(hashlib.sha256(data).hexdigest(),p.name,mime,text,classify(p.name,text),tables,warnings)
