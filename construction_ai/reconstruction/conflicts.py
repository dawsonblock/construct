"""Deterministic conflict and graph-consistency detection.

Conflicts are **derived at reconstruction time, not stored**. A stored conflict
table would be a second copy of a fact that can drift from the state it
describes — and the whole point of reconstruction is that the state is the only
source. Everything here is a pure function of rows already read.

Nothing in this module picks a winner. A conflict records that two things
disagree and what would settle it; resolution is Phase 33's job and a human's
decision, not a silent overwrite.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

HIGH = "high"
MEDIUM = "medium"
LOW = "low"

_SEVERITY_ORDER = {HIGH: 0, MEDIUM: 1, LOW: 2}

AMOUNT_TOLERANCE = Decimal("0.02")


def _canonical_value_repr(value: Any) -> str:
    """Canonical representation of an evidence value for comparison (item 32).

    "100.00", 100, and "100" all canonicalize to the same repr so they do not
    read as a contradiction. Strings are stripped and whitespace-normalized.
    Numeric strings are normalized via Decimal.
    """
    if value is None:
        return "None"
    if isinstance(value, bool):
        return repr(value)
    if isinstance(value, (int, float)):
        d = Decimal(str(value))
        return _format_decimal_repr(d)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return '""'
        try:
            return _format_decimal_repr(Decimal(stripped))
        except (InvalidOperation, ValueError):
            import re
            return repr(re.sub(r"\s+", " ", stripped))
    if isinstance(value, list):
        return repr([_canonical_value_repr(v) for v in value])
    if isinstance(value, dict):
        return repr({k: _canonical_value_repr(v) for k, v in sorted(value.items())})
    return repr(value)


def _format_decimal_repr(d: Decimal) -> str:
    """Format a Decimal without scientific notation and without trailing zeros."""
    normalized = d.normalize()
    return format(normalized, "f")


@dataclass(frozen=True)
class Conflict:
    conflict_type: str
    severity: str
    subject_type: str
    subject_id: str
    detail: str
    suggested_resolution: str = ""
    related_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "conflict_type": self.conflict_type,
            "severity": self.severity,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "detail": self.detail,
            "suggested_resolution": self.suggested_resolution,
            "related_ids": list(self.related_ids),
        }


def _sorted(conflicts: list[Conflict]) -> list[Conflict]:
    return sorted(conflicts, key=lambda c: (_SEVERITY_ORDER[c.severity], c.conflict_type, c.subject_id, c.detail))


def detect_conflicts(*, rows, entities, relationships, evidence, document_versions, structural) -> list[Conflict]:
    """Every check is a pure function of state already read. No I/O, no clock."""
    found: list[Conflict] = []
    found += _commercial_conflicts(rows)
    found += _graph_conflicts(rows, entities, relationships, structural)
    found += _evidence_conflicts(evidence, document_versions)
    return _sorted(found)


# --------------------------------------------------------------------------
# Commercial: references that do not resolve, numbers that do not agree
# --------------------------------------------------------------------------

def _commercial_conflicts(rows) -> list[Conflict]:
    found: list[Conflict] = []
    purchase_orders = {po.po_number: po for po in rows.purchase_orders}
    quotes = {q.quote_number: q for q in rows.quotes}

    for invoice in rows.invoices:
        subject = invoice.reference or invoice.invoice_id

        if invoice.po_number and invoice.po_number not in purchase_orders:
            found.append(Conflict(
                "PO_REFERENCE_UNRESOLVED", HIGH, "invoice", invoice.invoice_id,
                f"invoice {subject} cites purchase order {invoice.po_number}, which is not on this project",
                "confirm the PO number, or file the invoice against the project that owns that PO",
            ))
        elif invoice.po_number:
            purchase_order = purchase_orders[invoice.po_number]
            if abs(Decimal(str(invoice.total)) - Decimal(str(purchase_order.amount))) > AMOUNT_TOLERANCE:
                delta = Decimal(str(invoice.total)) - Decimal(str(purchase_order.amount))
                found.append(Conflict(
                    "AMOUNT_MISMATCH", HIGH, "invoice", invoice.invoice_id,
                    f"invoice {subject} is {delta:+} against purchase order {invoice.po_number} "
                    f"({invoice.total} vs {purchase_order.amount})",
                    "a change order, a revised PO, or a corrected invoice",
                    (purchase_order.po_id,),
                ))
            if invoice.vendor_company_id and purchase_order.vendor_company_id and invoice.vendor_company_id != purchase_order.vendor_company_id:
                found.append(Conflict(
                    "VENDOR_MISMATCH", HIGH, "invoice", invoice.invoice_id,
                    f"invoice {subject} is billed by a different company than purchase order {invoice.po_number} was ordered from",
                    "resolve vendor identity before paying",
                    (purchase_order.po_id,),
                ))

        if invoice.quote_number and invoice.quote_number not in quotes:
            found.append(Conflict(
                "QUOTE_REFERENCE_UNRESOLVED", MEDIUM, "invoice", invoice.invoice_id,
                f"invoice {subject} cites quote {invoice.quote_number}, which is not on this project",
                "attach the quote, or correct the reference",
            ))
        elif invoice.quote_number and not quotes[invoice.quote_number].approved:
            found.append(Conflict(
                "UNAPPROVED_QUOTE", HIGH, "invoice", invoice.invoice_id,
                f"invoice {subject} is billed against quote {invoice.quote_number}, which is not approved",
                "approve the quote or hold the invoice",
                (quotes[invoice.quote_number].quote_id,),
            ))

    return found


# --------------------------------------------------------------------------
# Graph consistency
# --------------------------------------------------------------------------

def _graph_conflicts(rows, entities, relationships, structural) -> list[Conflict]:
    found: list[Conflict] = []
    entity_by_id = {e.entity_id: e for e in entities}
    entity_by_record = {(e.record_table, e.record_id): e for e in entities if e.record_table}

    # 1. Structural edges that should exist and do not — the graph is stale.
    stored = {
        (
            entity_by_id[r.source_entity_id].record_id if r.source_entity_id in entity_by_id else None,
            r.relation,
            entity_by_id[r.target_entity_id].record_id if r.target_entity_id in entity_by_id else None,
        )
        for r in relationships
        if r.origin == "observed"
    }
    for source, relation, target in structural.edges:
        if (source[2], relation, target[2]) not in stored:
            found.append(Conflict(
                "GRAPH_INCOMPLETE", MEDIUM, source[0].lower(), str(source[2]),
                f"structural edge {source[0]} -{relation}-> {target[0]} is implied by the records but absent from the graph",
                "re-run projection for this project",
                (str(target[2]),),
            ))

    # 2. Nodes pointing at rows that are no longer there.
    live_records = {
        ("projects", rows.project.project_id),
        *(("companies", c.company_id) for c in rows.companies),
        *(("invoices", i.invoice_id) for i in rows.invoices),
        *(("purchase_orders", p.po_id) for p in rows.purchase_orders),
        *(("quotes", q.quote_id) for q in rows.quotes),
        *(("documents", str(d["document_id"])) for d in rows.documents),
        *(("approvals", a.approval_id) for a in rows.approvals),
    }
    for entity in entities:
        if entity.record_table and (entity.record_table, str(entity.record_id)) not in live_records:
            found.append(Conflict(
                "DANGLING_ENTITY", MEDIUM, entity.entity_type.lower(), str(entity.entity_id),
                f"graph node {entity.label!r} points at {entity.record_table} row {entity.record_id}, which is not on this project",
                "re-run projection, or investigate whether the record moved projects",
            ))

    # 3. Two approved MATCHES from the same source to different targets. Both
    #    cannot be true, and neither should be quietly preferred.
    matches: dict[Any, list[Any]] = {}
    for relationship in relationships:
        if relationship.relation == "MATCHES" and relationship.status == "approved":
            matches.setdefault(relationship.source_entity_id, []).append(relationship)
    for source_entity_id, group in matches.items():
        if len(group) > 1:
            source = entity_by_id.get(source_entity_id)
            targets = sorted(str(r.target_entity_id) for r in group)
            found.append(Conflict(
                "CONTRADICTORY_MATCH", HIGH, "relationship", str(source_entity_id),
                f"{(source.label if source else source_entity_id)!r} is approved as MATCHES against {len(group)} different targets",
                "reject all but one; a MATCHES edge is meant to be exclusive",
                tuple(targets),
            ))

    # 4. A proposal contradicting an already-approved match.
    approved_targets = {source: {r.target_entity_id for r in group} for source, group in matches.items()}
    for relationship in relationships:
        if relationship.relation != "MATCHES" or relationship.status != "proposed":
            continue
        settled = approved_targets.get(relationship.source_entity_id)
        if settled and relationship.target_entity_id not in settled:
            source = entity_by_id.get(relationship.source_entity_id)
            found.append(Conflict(
                "CONTRADICTORY_PROPOSAL", LOW, "relationship", str(relationship.relationship_id),
                f"a proposed MATCHES for {(source.label if source else relationship.source_entity_id)!r} "
                f"disagrees with the approved one",
                "reject the proposal, or reopen the approved match",
            ))

    # 5. Edges whose endpoints are not both nodes of this project.
    project_entity_ids = set(entity_by_id)
    for relationship in relationships:
        missing = [e for e in (relationship.source_entity_id, relationship.target_entity_id) if e not in project_entity_ids]
        if missing:
            found.append(Conflict(
                "EDGE_LEAVES_PROJECT", MEDIUM, "relationship", str(relationship.relationship_id),
                f"edge {relationship.relation} has {len(missing)} endpoint(s) outside this project's node set",
                "re-run projection; an endpoint may have been re-filed",
            ))

    _ = entity_by_record  # kept for readability of the lookup above
    return found


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------

def _evidence_conflicts(evidence, document_versions) -> list[Conflict]:
    found: list[Conflict] = []

    current_version_ids = set()
    latest: dict[Any, Any] = {}
    for version in document_versions:
        best = latest.get(version.document_id)
        if best is None or version.version_number > best.version_number:
            latest[version.document_id] = version
    current_version_ids = {str(v.document_version_id) for v in latest.values()}
    known_version_ids = {str(v.document_version_id) for v in document_versions}

    for item in evidence:
        source_version_id = item.source_version_id
        if source_version_id and source_version_id in known_version_ids and source_version_id not in current_version_ids:
            found.append(Conflict(
                "EVIDENCE_FROM_SUPERSEDED_VERSION", MEDIUM, "evidence", item.evidence_id,
                f"evidence for {item.field!r} was extracted from a document version that has since been superseded",
                "re-extract from the current revision, or confirm the value still holds",
            ))

    # Two pieces of evidence about the *same subject and field* disagreeing, both
    # from sources entitled to be believed.
    #
    # Grouping by (subject, field) rather than field alone matters: two invoices
    # each carrying a `total` are not in conflict, they are two invoices. A field
    # is only comparable within the thing it describes.
    #
    # v0.4.6 (item 32): values are canonicalized before comparison so that
    # "100.00" and 100 and "100" do not read as a contradiction.
    by_subject: dict[tuple[str, str, str], list[Any]] = {}
    for item in evidence:
        key = (item.subject_type or "", item.subject_id or "", item.field)
        by_subject.setdefault(key, []).append(item)
    for (subject_type, subject_id, field), group in sorted(by_subject.items()):
        authoritative = [e for e in group if e.authority >= 0.9]
        values = {_canonical_value_repr(e.value) for e in authoritative}
        if len(values) > 1:
            found.append(Conflict(
                "EVIDENCE_CONTRADICTION", HIGH, subject_type or "evidence", subject_id or field,
                f"{len(values)} different values for {field!r} from sources of equal authority: {sorted(values)}",
                "establish source authority ordering, or ask a person which is correct",
                tuple(sorted(e.evidence_id for e in authoritative)),
            ))

    return found
