"""Deterministic projection of authoritative rows into typed nodes and edges.

`structural_graph()` is a pure function: given a project's rows it returns the
nodes and edges that are true *because a column says so*. Two things consume it,
and that is the point of writing it once:

- `project_graph()` writes them, as `origin='observed'` edges.
- the consistency check reads them and compares against what is actually stored,
  so a stale graph shows up as a finding instead of as silence.

Nothing here infers anything. Inference produces `status='proposed'` candidates
through `RelationshipRepository.propose`, and stays there until promoted.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from construction_ai.persistence.db import Scope

# (entity_type, record_table, record_id)
NodeKey = tuple[str, str, UUID]


@dataclass(frozen=True)
class StructuralGraph:
    #: node key -> display label
    nodes: dict[NodeKey, str]
    #: (source key, relation, target key), in a stable order
    edges: tuple[tuple[NodeKey, str, NodeKey], ...]


@dataclass(frozen=True)
class ProjectRows:
    """Everything the structural graph is derived from, read once."""

    project: Any
    companies: list[Any]
    invoices: list[Any]
    purchase_orders: list[Any]
    quotes: list[Any]
    documents: list[dict[str, Any]]
    approvals: list[Any]

    @classmethod
    def load(cls, repos, scope: Scope) -> ProjectRows:
        project_id = scope.require_project()
        project = repos.projects.get(scope=scope, project_id=project_id)
        if project is None:
            raise LookupError(f"project {project_id} not found in this organization")
        company_ids = repos.projects.companies_for_project(scope=scope)
        companies = [c for c in (repos.companies.get(scope=scope.organization_only, company_id=UUID(cid)) for cid in company_ids) if c]
        return cls(
            project=project,
            companies=sorted(companies, key=lambda c: (c.reference or "", c.company_id)),
            invoices=sorted(repos.invoices.for_project(scope=scope), key=lambda i: (i.reference or "", i.invoice_id)),
            purchase_orders=sorted(repos.purchase_orders.for_project(scope=scope), key=lambda p: (p.reference or "", p.po_id)),
            quotes=sorted(repos.quotes.for_project(scope=scope), key=lambda q: (q.reference or "", q.quote_id)),
            documents=sorted(repos.documents.list(scope=scope), key=lambda d: (d["filename"], str(d["document_id"]))),
            approvals=sorted(repos.approvals.list(scope=scope), key=lambda a: (a.reference or "", a.approval_id)),
        )


def structural_graph(rows: ProjectRows) -> StructuralGraph:
    nodes: dict[NodeKey, str] = {}
    edges: list[tuple[NodeKey, str, NodeKey]] = []

    project_key: NodeKey = ("PROJECT", "projects", UUID(rows.project.project_id))
    nodes[project_key] = rows.project.reference or rows.project.name

    company_keys: dict[str, NodeKey] = {}
    for company in rows.companies:
        key: NodeKey = ("VENDOR", "companies", UUID(company.company_id))
        nodes[key] = company.reference or company.name
        company_keys[company.company_id] = key
        edges.append((key, "BELONGS_TO", project_key))

    # References are unique per organization, so a reference→record map is
    # unambiguous inside this scope and nowhere else.
    po_by_reference: dict[str, NodeKey] = {}
    for purchase_order in rows.purchase_orders:
        key = ("PURCHASE_ORDER", "purchase_orders", UUID(purchase_order.po_id))
        nodes[key] = purchase_order.reference or purchase_order.po_number
        po_by_reference[purchase_order.po_number] = key
        edges.append((key, "BELONGS_TO", project_key))
        vendor_key = company_keys.get(purchase_order.vendor_company_id)
        if vendor_key:
            edges.append((key, "ORDERED_FROM", vendor_key))

    quote_by_reference: dict[str, NodeKey] = {}
    for quote in rows.quotes:
        key = ("CONTRACT", "quotes", UUID(quote.quote_id))
        nodes[key] = quote.reference or quote.quote_number
        quote_by_reference[quote.quote_number] = key
        edges.append((key, "BELONGS_TO", project_key))
        vendor_key = company_keys.get(quote.vendor_company_id)
        if vendor_key:
            edges.append((key, "ORDERED_FROM", vendor_key))

    invoice_keys: dict[str, NodeKey] = {}
    for invoice in rows.invoices:
        key = ("INVOICE", "invoices", UUID(invoice.invoice_id))
        nodes[key] = invoice.reference or invoice.invoice_number
        invoice_keys[invoice.invoice_id] = key
        edges.append((key, "BELONGS_TO", project_key))
        vendor_key = company_keys.get(invoice.vendor_company_id or "")
        if vendor_key:
            edges.append((key, "BILLED_BY", vendor_key))
        # A reference that resolves to a row in this project is structural. One
        # that does not is a conflict, reported by the consistency check rather
        # than guessed at here.
        if invoice.po_number and invoice.po_number in po_by_reference:
            edges.append((key, "REFERENCES", po_by_reference[invoice.po_number]))
        if invoice.quote_number and invoice.quote_number in quote_by_reference:
            edges.append((key, "REFERENCES", quote_by_reference[invoice.quote_number]))

    for document in rows.documents:
        key = ("DOCUMENT", "documents", document["document_id"])
        nodes[key] = document["filename"]
        edges.append((key, "BELONGS_TO", project_key))

    for approval in rows.approvals:
        key = ("APPROVAL", "approvals", UUID(approval.approval_id))
        nodes[key] = approval.reference or approval.approval_id
        edges.append((key, "BELONGS_TO", project_key))
        subject_key = invoice_keys.get(approval.subject_id)
        if subject_key:
            edges.append((key, "APPROVES", subject_key))

    return StructuralGraph(nodes=nodes, edges=tuple(sorted(set(edges))))


def project_graph(repos, scope: Scope, *, rows: ProjectRows | None = None) -> dict[str, int]:
    """Write the structural graph. Idempotent — re-projecting changes nothing."""
    rows = rows or ProjectRows.load(repos, scope)
    graph = structural_graph(rows)

    entity_ids: dict[NodeKey, UUID] = {}
    for key in sorted(graph.nodes):
        entity_type, record_table, record_id = key
        entity = repos.entities.ensure(
            scope=scope, entity_type=entity_type, record_table=record_table, record_id=record_id, label=graph.nodes[key]
        )
        entity_ids[key] = entity.entity_id

    for source, relation, target in graph.edges:
        repos.relationships.observe(
            scope=scope,
            source_entity_id=entity_ids[source],
            target_entity_id=entity_ids[target],
            relation=relation,
        )
    return {"nodes": len(graph.nodes), "edges": len(graph.edges)}
