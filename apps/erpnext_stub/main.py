"""In-repo ERPNext stub.

Serves the exact REST shapes `ERPNextEvidenceResolver` reads, so the acceptance
gate runs offline and deterministically. It is a fixture server, not a
simulation of ERPNext: it has no doctype model, no permissions, no workflow.

v0.5.0-rc1: added write support (POST resource, submit) so the
ApprovedInvoiceExecutor can be tested end-to-end against the stub. Writes are
held in an in-memory store that mirrors the fixture shape — the stub is a test
server, not a production ERP.

Point `ERP_NEXT_URL` at a live Frappe instance to use the real thing — the
connector code does not change.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query

FIXTURES = json.loads((Path(__file__).parent / "fixtures.json").read_text())

app = FastAPI(title="ERPNext stub", version="0.5.0-rc9.dev0")

# In-memory store for created documents. Mirrors the fixture shape so reads
# work identically. Initialized at module load time so TestClient works without
# lifespan management.
_STORE: dict[str, list[dict[str, Any]]] = {doctype: [dict(r) for r in rows] for doctype, rows in FIXTURES.items()}


def reset_store():
    """Reset the store to the fixture state. Call between tests if needed."""
    _STORE.clear()
    for doctype, rows in FIXTURES.items():
        _STORE[doctype] = [dict(r) for r in rows]


def _matches(row: dict[str, Any], filters: list[list[Any]]) -> bool:
    for entry in filters:
        if len(entry) == 3:
            fieldname, operator, value = entry
        elif len(entry) == 4:  # ERPNext also accepts [doctype, field, op, value]
            _, fieldname, operator, value = entry
        else:
            raise HTTPException(400, f"unsupported filter shape: {entry!r}")
        actual = row.get(fieldname)
        if operator == "=":
            if actual != value:
                return False
        elif operator == "!=":
            if actual == value:
                return False
        elif operator == "like":
            pattern = str(value).replace("%", "")
            if pattern.lower() not in str(actual or "").lower():
                return False
        elif operator == "in":
            if actual not in value:
                return False
        else:
            raise HTTPException(400, f"unsupported operator: {operator!r}")
    return True


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "erpnext-stub"}


@app.get("/api/method/ping")
def ping() -> dict[str, str]:
    return {"message": "pong"}


@app.get("/api/resource/{doctype}")
def resource_list(
    doctype: str,
    filters: str | None = Query(default=None),
    fields: str | None = Query(default=None),
    limit_page_length: int = Query(default=20),
) -> dict[str, list[dict[str, Any]]]:
    rows = _STORE.get(doctype)
    if rows is None:
        raise HTTPException(404, f"unknown doctype: {doctype}")
    parsed_filters = json.loads(filters) if filters else []
    parsed_fields = json.loads(fields) if fields else None
    selected = [r for r in rows if _matches(r, parsed_filters)][:limit_page_length]
    if parsed_fields:
        selected = [{k: r.get(k) for k in parsed_fields} for r in selected]
    return {"data": selected}


@app.get("/api/resource/{doctype}/{name}")
def resource_get(doctype: str, name: str) -> dict[str, Any]:
    for row in _STORE.get(doctype, []):
        if row.get("name") == name:
            return {"data": row}
    raise HTTPException(404, f"{doctype} {name} not found")


@app.post("/api/resource/{doctype}")
def resource_create(doctype: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create a document. Returns the created document with a generated name.

    The stub assigns a sequential name like `PINV-0001` and stores the document
    with docstatus from the body (default 0 = draft).
    """
    body = body or {}
    rows = _STORE.setdefault(doctype, [])
    # Generate a sequential name.
    n = len(rows) + 1
    name = body.pop("name", None) or f"{doctype[:4].upper()}-{n:04d}"
    doc = {**body, "name": name, "docstatus": body.get("docstatus", 0)}
    rows.append(doc)
    return {"data": doc}


@app.post("/api/method/frappe.client.submit")
def resource_submit(body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Submit a document (set docstatus from 0 to 1).

    The body should contain `doctype` and `name`. Returns the submitted document.
    """
    body = body or {}
    doctype = body.get("doctype")
    name = body.get("name")
    if not doctype or not name:
        raise HTTPException(400, "doctype and name are required")
    rows = _STORE.get(doctype)
    if rows is None:
        raise HTTPException(404, f"unknown doctype: {doctype}")
    for row in rows:
        if row.get("name") == name:
            if row.get("docstatus") != 0:
                raise HTTPException(409, f"{doctype} {name} is already submitted (docstatus={row['docstatus']})")
            row["docstatus"] = 1
            return {"data": row}
    raise HTTPException(404, f"{doctype} {name} not found")
