"""Job handlers.

A handler receives the repositories and a job, and derives its scope from
`job.scope` — the row's own tenancy, not anything in the payload.
"""
from __future__ import annotations

from typing import Any

from construction_ai.domain.models import Project
from construction_ai.executive.invoice_pipeline import InvoicePipeline
from construction_ai.extraction.invoice import extract_invoice_deterministic
from construction_ai.integrations.http import create_erpnext_resolver
from construction_ai.resolution.project import _norm

INVOICE_DOCUMENT = "invoice_document"


def address_signal(text: str, projects: list[Project]) -> str | None:
    """A project address quoted in the document, but only when unambiguous.

    Two projects whose addresses both appear yields no signal at all. A wrong
    automatic file is worse than asking a person.
    """
    haystack = _norm(text)
    matches = {p.address for p in projects if p.address and _norm(p.address) in haystack}
    return next(iter(matches)) if len(matches) == 1 else None


def handle_invoice_document(repos, job) -> dict[str, Any]:
    scope = job.scope.organization_only
    payload = job.payload
    text = payload.get("text") or ""
    filename = payload.get("filename") or "invoice.txt"
    source_id = payload.get("source_id") or str(job.job_id)

    extracted, evidence, warnings = extract_invoice_deterministic(
        organization_id=str(scope.organization_id), source_id=source_id, text=text, filename=filename
    )
    if extracted is None:
        # Fail closed. Missing required fields are never inferred.
        for candidate in evidence:
            repos.evidence.record(
                scope=scope,
                field=candidate.field,
                value=candidate.value,
                confidence=candidate.confidence,
                authority=candidate.authority,
                source_type=candidate.source_type,
                source_id=candidate.source_id,
                extractor=candidate.extractor,
            )
        repos.audit.append(
            scope=scope,
            event_type="INVOICE_EXTRACTION_INCOMPLETE",
            actor="ai",
            object_type="job",
            object_id=job.job_id,
            payload={"warnings": [w for w in warnings if w], "filename": filename},
        )
        return {
            "status": "extraction_incomplete",
            "requires_human": True,
            "warnings": [w for w in warnings if w],
        }

    projects = repos.projects.list(scope=scope)
    signals: dict[str, Any] = {}
    if extracted.po_number:
        signals["po_number"] = extracted.po_number
    if payload.get("thread_id"):
        signals["thread_id"] = payload["thread_id"]
    address = address_signal(text, projects)
    if address:
        signals["address"] = address

    pipeline = InvoicePipeline(
        repositories=repos, erp_resolver=create_erpnext_resolver(str(scope.organization_id))
    )
    result = pipeline.process(
        scope=scope,
        extracted=extracted,
        signals=signals,
        evidence=evidence,
        work_confirmed=bool(payload.get("work_confirmed", False)),
    )
    result["warnings"] = [w for w in warnings if w]
    return result


HANDLERS = {INVOICE_DOCUMENT: handle_invoice_document}
