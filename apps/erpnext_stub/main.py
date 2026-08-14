"""In-repo ERPNext stub.

Serves the exact REST shapes `ERPNextEvidenceResolver` reads, so the acceptance
gate runs offline and deterministically. It is a fixture server, not a
simulation of ERPNext: it has no doctype model, no permissions, no workflow.

Point `ERP_NEXT_URL` at a live Frappe instance to use the real thing — the
connector code does not change.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query

FIXTURES = json.loads((Path(__file__).parent / "fixtures.json").read_text())

app = FastAPI(title="ERPNext stub", version="0.4.1.dev0")


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
    rows = FIXTURES.get(doctype)
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
    for row in FIXTURES.get(doctype, []):
        if row.get("name") == name:
            return {"data": row}
    raise HTTPException(404, f"{doctype} {name} not found")
