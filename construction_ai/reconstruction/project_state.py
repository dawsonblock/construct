"""Deterministic project reconstruction.

    same database state  ⇒  same reconstructed project state

That invariant is the reason this module exists and the reason it is boring.
Reconstruction:

- reads persistent state and nothing else — no model call, no network, no clock;
- writes nothing, so calling it can never change what a later call produces;
- orders every collection totally, so equality is byte-level and not incidental;
- derives conflicts rather than reading them from a table that could drift.

`fingerprint()` makes the invariant testable instead of aspirational. An AI may
read a `ProjectState`; nothing an AI does may change what rebuilding one returns.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from construction_ai.graph.projection import ProjectRows, structural_graph
from construction_ai.persistence.db import Scope
from construction_ai.persistence.serialization import dumps
from construction_ai.reconstruction.conflicts import Conflict, detect_conflicts
from construction_ai.reconstruction.graph_view import GraphNode, ProjectGraph, edge_from_relationship


@dataclass(frozen=True)
class ProjectState:
    identity: dict[str, Any]
    vendors: list[dict[str, Any]] = field(default_factory=list)
    documents: list[dict[str, Any]] = field(default_factory=list)
    purchase_orders: list[dict[str, Any]] = field(default_factory=list)
    quotes: list[dict[str, Any]] = field(default_factory=list)
    invoices: list[dict[str, Any]] = field(default_factory=list)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    #: Authority-aware. There is no flat edge list; see reconstruction/graph_view.py.
    graph: ProjectGraph = field(default_factory=ProjectGraph)
    unresolved_conflicts: list[dict[str, Any]] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)

    def fingerprint(self) -> str:
        """SHA-256 over the canonical serialization of the whole aggregate.

        Uses the same encoder the persistence layer writes with, so a value that
        round-trips through the database fingerprints identically before and
        after.
        """
        return hashlib.sha256(dumps(asdict(self)).encode()).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def authoritative_edges(self):
        """Shorthand for the default view. Proposals are not project state."""
        return self.graph.authoritative()

    @property
    def conflict_count(self) -> int:
        return len(self.unresolved_conflicts)

    def conflicts_of_severity(self, severity: str) -> list[dict[str, Any]]:
        return [c for c in self.unresolved_conflicts if c["severity"] == severity]


def _vendor(company) -> dict[str, Any]:
    return {
        "company_id": company.company_id,
        "reference": company.reference,
        "name": company.name,
        "type": company.type,
        "erp_supplier_id": company.erp_supplier_id,
    }


def _document(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "document_id": str(row["document_id"]),
        "filename": row["filename"],
        "document_type": row["document_type"],
        "document_family": row.get("document_family"),
        "current_version": row["current_version"],
        "source_id": str(row["source_id"]) if row.get("source_id") else None,
    }


def _purchase_order(purchase_order) -> dict[str, Any]:
    return {
        "purchase_order_id": purchase_order.po_id,
        "reference": purchase_order.reference,
        "vendor_company_id": purchase_order.vendor_company_id or None,
        "amount": purchase_order.amount,
        "quote_reference": purchase_order.quote_number,
    }


def _quote(quote) -> dict[str, Any]:
    return {
        "quote_id": quote.quote_id,
        "reference": quote.reference,
        "vendor_company_id": quote.vendor_company_id or None,
        "amount": quote.amount,
        "approved": quote.approved,
    }


def _invoice(invoice) -> dict[str, Any]:
    return {
        "invoice_id": invoice.invoice_id,
        "reference": invoice.reference,
        "invoice_number": invoice.invoice_number,
        "vendor_name": invoice.vendor_name,
        "vendor_company_id": invoice.vendor_company_id,
        "subtotal": invoice.subtotal,
        "tax": invoice.tax,
        "total": invoice.total,
        "currency": invoice.currency,
        "po_reference": invoice.po_number,
        "quote_reference": invoice.quote_number,
        "source_version_id": invoice.source_version_id,
    }


def _approval(approval) -> dict[str, Any]:
    return {
        "approval_id": approval.approval_id,
        "reference": approval.reference,
        "type": approval.type,
        "subject_id": approval.subject_id,
        "status": approval.status.value,
        "recommended_action": approval.recommended_action,
        "amount": approval.amount,
        "exceptions": list(approval.exceptions),
        "decided_by": approval.approved_by,
        "evidence_ids": sorted(approval.evidence_ids),
    }


def _evidence(item) -> dict[str, Any]:
    return {
        "evidence_id": item.evidence_id,
        "field": item.field,
        "value": _canonical_value(item.value),
        "confidence": item.confidence,
        "authority": item.authority,
        "source_type": item.source_type,
        "source_id": item.source_id,
        "source_version_id": item.source_version_id,
        "extractor": item.extractor,
        "observed_at": item.observed_at,
        # v0.4.6 (item 31): subject is part of the reconstructed state, not
        # just a conflict-detection grouping key. A consumer needs to know what
        # each piece of evidence is *about* to use it correctly.
        "subject_type": item.subject_type,
        "subject_id": item.subject_id,
    }


def _canonical_value(value: Any) -> Any:
    """Canonicalize an evidence value for deterministic comparison (item 32).

    Two pieces of evidence about the same subject and field should compare
    equal when they carry the same fact, even if the JSON representation
    differs (e.g., "100.00" vs 100.00 vs "100"). Canonicalization:
    - Numeric strings that are valid decimals become Decimal strings.
    - Strings are stripped and whitespace-normalized.
    - Nested dicts and lists are canonicalized recursively.
    - None stays None.
    """
    from decimal import Decimal, InvalidOperation

    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        # Normalize floats to Decimal strings to avoid float comparison issues.
        # Use a format that avoids scientific notation (Decimal.normalize() can
        # produce "1E+2" for 100).
        d = Decimal(str(value))
        return _format_decimal(d)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return ""
        # Try to parse as a number — "100.00" and "100" should canonicalize
        # to the same value.
        try:
            d = Decimal(stripped)
            return _format_decimal(d)
        except InvalidOperation:
            pass
        # Normalize whitespace within strings.
        import re
        return re.sub(r"\s+", " ", stripped)
    if isinstance(value, list):
        return [_canonical_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _canonical_value(v) for k, v in sorted(value.items())}
    return value


def _format_decimal(d: Decimal) -> str:
    """Format a Decimal without scientific notation and without trailing zeros."""
    # Normalize to remove trailing zeros, then format without exponent.
    normalized = d.normalize()
    sign, digits, exponent = normalized.as_tuple()
    if exponent < 0:
        # Has fractional digits — format as plain string.
        return format(normalized, "f")
    else:
        # Integer value — format without exponent.
        return format(normalized, "f")


def reconstruct(repos, scope: Scope) -> ProjectState:
    """Rebuild a project from persistent state. Reads only; never writes."""
    scope.require_project()

    rows = ProjectRows.load(repos, scope)
    entities = repos.entities.for_project(scope=scope)
    relationships = repos.relationships.for_project(scope=scope)
    evidence = repos.evidence.for_project(scope=scope)
    document_versions = repos.documents.versions_for_project(scope=scope)
    structural = structural_graph(rows)

    conflicts: list[Conflict] = detect_conflicts(
        rows=rows,
        entities=entities,
        relationships=relationships,
        evidence=evidence,
        document_versions=document_versions,
        structural=structural,
    )

    labels = {e.entity_id: e.label for e in entities}
    grouped: dict[str, list[Any]] = {"approved": [], "proposed": [], "rejected": [], "superseded": []}
    for relationship in relationships:
        grouped.setdefault(relationship.status, []).append(edge_from_relationship(relationship, labels))
    graph = ProjectGraph(
        nodes=tuple(
            GraphNode(
                entity_id=str(e.entity_id), entity_type=e.entity_type, label=e.label,
                record_table=e.record_table, record_id=str(e.record_id) if e.record_id else None,
            )
            for e in entities
        ),
        approved=tuple(grouped["approved"]),
        proposed=tuple(grouped["proposed"]),
        rejected=tuple(grouped["rejected"]),
        superseded=tuple(grouped["superseded"]),
    )

    identity = {
        "project_id": rows.project.project_id,
        "reference": rows.project.reference,
        "name": rows.project.name,
        "address": rows.project.address,
        "status": rows.project.status,
        "organization_id": rows.project.organization_id,
        "identifiers": rows.project.identifiers,
    }

    provenance = {
        "counts": {
            "vendors": len(rows.companies),
            "documents": len(rows.documents),
            "document_versions": len(document_versions),
            "purchase_orders": len(rows.purchase_orders),
            "quotes": len(rows.quotes),
            "invoices": len(rows.invoices),
            "approvals": len(rows.approvals),
            "evidence": len(evidence),
            "entities": len(entities),
            "relationships": len(relationships),
        },
        "evidence_source_types": sorted({e.source_type for e in evidence}),
        "extractors": sorted({e.extractor for e in evidence}),
        # v0.4.6 (item 31): subject coverage — how many evidence items carry
        # a subject vs how many are unscoped. Unscoped evidence is weaker
        # because it cannot be compared safely across records.
        "evidence_with_subject": sum(1 for e in evidence if e.subject_type and e.subject_id),
        "evidence_without_subject": sum(1 for e in evidence if not (e.subject_type and e.subject_id)),
        "evidence_subject_types": sorted({e.subject_type for e in evidence if e.subject_type}),
        # Structural completeness: how much of the graph the records imply is
        # actually stored. Anything below 1.0 means projection is stale, and the
        # matching GRAPH_INCOMPLETE conflicts say exactly which edges.
        "structural_edges_expected": len(structural.edges),
        "structural_edges_missing": sum(1 for c in conflicts if c.conflict_type == "GRAPH_INCOMPLETE"),
        # Deliberately no reconstruction timestamp: it would make two rebuilds of
        # identical state produce different fingerprints.
    }

    return ProjectState(
        identity=identity,
        vendors=[_vendor(c) for c in rows.companies],
        documents=[_document(d) for d in rows.documents],
        purchase_orders=[_purchase_order(p) for p in rows.purchase_orders],
        quotes=[_quote(q) for q in rows.quotes],
        invoices=[_invoice(i) for i in rows.invoices],
        approvals=[_approval(a) for a in rows.approvals],
        evidence=[_evidence(e) for e in evidence],
        # `decisions` has a table and no writer yet; the executive controller
        # still returns actions without persisting them. Empty is honest.
        decisions=[],
        graph=graph,
        unresolved_conflicts=[c.as_dict() for c in conflicts],
        provenance=provenance,
    )
