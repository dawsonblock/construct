"""Invoice → purchase-order matching.

The better first end-to-end producer: concrete evidence, consequences a person
can reason about, and a score that has to explain itself.

    s = w₁·s_PO + w₂·s_vendor + w₃·s_amount + w₄·s_line + w₅·s_date

Two rules the weights alone will not give you, so they are enforced separately:

- **A contradiction caps the score.** An exact PO reference billed by the wrong
  vendor is not a 0.87 match; it is a strong signal pointing at a problem. The
  cap sits below any plausible auto-promotion threshold.
- **Ambiguity caps the score.** If two purchase orders score within a hair of
  each other, neither is a confident match no matter how high the winner scored.

This module calls `propose()` only. It has no access to `observe()` and no
promotion path.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from uuid import UUID

from construction_ai.matching.signals import MatchSignal, combine, normalize_identifier

RULE_VERSION = "invoice-po-match-v1"
PRODUCER = f"invoice-po:{RULE_VERSION}"

WEIGHTS = {"po_number": 0.45, "vendor": 0.25, "amount": 0.20, "line_items": 0.05, "date": 0.05}

AMOUNT_TOLERANCE = Decimal("0.02")
#: Beyond this relative gap the amounts are not "close enough with a variance",
#: they are a contradiction worth a person's attention.
AMOUNT_CONTRADICTION_RATIO = Decimal("0.20")
DATE_WINDOW_DAYS = 120
#: Small negative gaps are date-entry slop. Beyond this, the invoice genuinely
#: predates the order, which is a question for a person and not a match.
DATE_BACKDATING_GRACE_DAYS = 3

CONTRADICTION_CEILING = 0.49
AMBIGUITY_CEILING = 0.60
AMBIGUITY_MARGIN = 0.05
PROPOSAL_FLOOR = 0.50


@dataclass(frozen=True)
class MatchCandidate:
    invoice_id: str
    purchase_order_id: str
    invoice_reference: str
    purchase_order_reference: str
    confidence: float
    signals: tuple[MatchSignal, ...]
    contradictions: tuple[str, ...] = ()
    unavailable_signals: tuple[str, ...] = ()
    capped_reason: str | None = None
    rule_version: str = RULE_VERSION

    def as_dict(self) -> dict:
        return {
            "invoice_id": self.invoice_id,
            "purchase_order_id": self.purchase_order_id,
            "invoice_reference": self.invoice_reference,
            "purchase_order_reference": self.purchase_order_reference,
            "confidence": self.confidence,
            "signals": {s.name: s.as_dict() for s in self.signals},
            "contradictions": list(self.contradictions),
            "unavailable_signals": list(self.unavailable_signals),
            "capped_reason": self.capped_reason,
            "rule_version": self.rule_version,
        }


def _amount_signal(invoice_total, po_amount) -> tuple[MatchSignal, str | None]:
    if invoice_total is None or po_amount is None:
        return MatchSignal("amount", 0.0, WEIGHTS["amount"], "amount missing on one side", available=False), None
    invoice_value, po_value = Decimal(str(invoice_total)), Decimal(str(po_amount))
    delta = abs(invoice_value - po_value)
    if delta <= AMOUNT_TOLERANCE:
        return MatchSignal("amount", 1.0, WEIGHTS["amount"], f"{invoice_value} == {po_value}"), None
    reference = max(po_value, Decimal("1"))
    ratio = delta / reference
    score = float(max(Decimal("0"), Decimal("1") - ratio))
    contradiction = "AMOUNT_OUT_OF_BAND" if ratio > AMOUNT_CONTRADICTION_RATIO else None
    return (
        MatchSignal("amount", score, WEIGHTS["amount"], f"{invoice_value} vs {po_value} ({ratio:.1%} apart)"),
        contradiction,
    )


def _date_signal(invoice_date: date | None, ordered_on: date | None) -> tuple[MatchSignal, str | None]:
    if invoice_date is None or ordered_on is None:
        return MatchSignal("date", 0.0, WEIGHTS["date"], "date missing on one side", available=False), None
    gap = (invoice_date - ordered_on).days
    if gap < -DATE_BACKDATING_GRACE_DAYS:
        # Work billed before it was ordered. At 5% weight this would barely dent
        # an otherwise-agreeing score, so it is a contradiction rather than a
        # low signal — the corpus case says a person should look at it.
        return (
            MatchSignal("date", 0.0, WEIGHTS["date"], f"invoice predates the order by {-gap} days"),
            "INVOICE_PREDATES_ORDER",
        )
    score = max(0.0, 1.0 - max(0, gap) / DATE_WINDOW_DAYS)
    return MatchSignal("date", round(score, 4), WEIGHTS["date"], f"{gap} days after the order"), None


def score_invoice_po(invoice, purchase_order) -> MatchCandidate:
    """Pure comparison of one invoice against one purchase order."""
    signals: list[MatchSignal] = []
    contradictions: list[str] = []

    invoice_po_reference = normalize_identifier(invoice.po_number)
    po_reference = normalize_identifier(purchase_order.po_number)
    if invoice_po_reference and po_reference:
        agree = invoice_po_reference == po_reference
        signals.append(MatchSignal("po_number", 1.0 if agree else 0.0, WEIGHTS["po_number"], f"{invoice.po_number} vs {purchase_order.po_number}"))
    else:
        signals.append(MatchSignal("po_number", 0.0, WEIGHTS["po_number"], "no PO reference on the invoice", available=False))

    invoice_vendor = invoice.vendor_company_id or None
    po_vendor = purchase_order.vendor_company_id or None
    if invoice_vendor and po_vendor:
        agree = invoice_vendor == po_vendor
        signals.append(MatchSignal("vendor", 1.0 if agree else 0.0, WEIGHTS["vendor"], "same vendor" if agree else "different vendor"))
        if not agree:
            contradictions.append("VENDOR_MISMATCH")
    else:
        signals.append(MatchSignal("vendor", 0.0, WEIGHTS["vendor"], "vendor identity unresolved on one side", available=False))

    amount_signal, amount_contradiction = _amount_signal(invoice.total, purchase_order.amount)
    signals.append(amount_signal)
    if amount_contradiction:
        contradictions.append(amount_contradiction)

    # Line-item similarity is defined and permanently unavailable until an
    # ERPNext import populates purchase-order lines. Declared rather than
    # dropped, so the explanation vector does not quietly change shape when it
    # arrives.
    signals.append(MatchSignal("line_items", 0.0, WEIGHTS["line_items"], "purchase orders carry no lines yet", available=False))

    date_signal, date_contradiction = _date_signal(
        getattr(invoice, "invoice_date", None), getattr(purchase_order, "ordered_on", None)
    )
    signals.append(date_signal)
    if date_contradiction:
        contradictions.append(date_contradiction)

    if invoice.project_id and purchase_order.project_id and invoice.project_id != purchase_order.project_id:
        contradictions.append("PROJECT_MISMATCH")

    confidence = combine(tuple(signals))
    capped_reason = None
    if contradictions:
        capped_reason = f"contradicted: {', '.join(sorted(contradictions))}"
        confidence = min(confidence, CONTRADICTION_CEILING)

    return MatchCandidate(
        invoice_id=invoice.invoice_id,
        purchase_order_id=purchase_order.po_id,
        invoice_reference=invoice.reference or invoice.invoice_number,
        purchase_order_reference=purchase_order.reference or purchase_order.po_number,
        confidence=round(confidence, 6),
        signals=tuple(signals),
        contradictions=tuple(sorted(contradictions)),
        unavailable_signals=tuple(sorted(s.name for s in signals if not s.available)),
        capped_reason=capped_reason,
    )


def rank_candidates(invoice, purchase_orders) -> list[MatchCandidate]:
    """Score an invoice against every purchase order, ambiguity accounted for.

    When the top two are within `AMBIGUITY_MARGIN`, both are capped. A confident
    match requires being clearly better than the alternative, not merely best.
    """
    scored = sorted(
        (score_invoice_po(invoice, po) for po in purchase_orders),
        key=lambda c: (-c.confidence, c.purchase_order_id),
    )
    if len(scored) >= 2 and (scored[0].confidence - scored[1].confidence) < AMBIGUITY_MARGIN:
        tied = [c for c in scored if abs(c.confidence - scored[0].confidence) < AMBIGUITY_MARGIN]
        capped = []
        for candidate in scored:
            if candidate in tied and candidate.confidence > AMBIGUITY_CEILING:
                reason = "; ".join(filter(None, [candidate.capped_reason, f"{len(tied)} purchase orders score within {AMBIGUITY_MARGIN}"]))
                capped.append(replace(candidate, confidence=AMBIGUITY_CEILING, capped_reason=reason))
            else:
                capped.append(candidate)
        scored = capped
    return scored


def propose_invoice_po_matches(repos, scope, *, floor: float = PROPOSAL_FLOOR) -> list[dict]:
    """Propose MATCHES edges for every invoice on the project. Proposals only."""
    project_scope = scope.for_project(scope.require_project())
    invoices = repos.invoices.for_project(scope=project_scope)
    purchase_orders = repos.purchase_orders.for_project(scope=project_scope)
    if not purchase_orders:
        return []

    proposals: list[dict] = []
    for invoice in sorted(invoices, key=lambda i: i.invoice_id):
        for candidate in rank_candidates(invoice, purchase_orders):
            if candidate.confidence < floor:
                continue
            invoice_entity = repos.entities.ensure(
                scope=project_scope, entity_type="INVOICE", record_table="invoices",
                record_id=UUID(candidate.invoice_id), label=candidate.invoice_reference,
            )
            po_entity = repos.entities.ensure(
                scope=project_scope, entity_type="PURCHASE_ORDER", record_table="purchase_orders",
                record_id=UUID(candidate.purchase_order_id), label=candidate.purchase_order_reference,
            )
            evidence_ids = [
                UUID(e.evidence_id)
                for e in repos.evidence.for_subject(scope=project_scope, subject_type="invoice", subject_id=UUID(candidate.invoice_id))
            ]
            relationship = repos.relationships.propose(
                scope=project_scope,
                source_entity_id=invoice_entity.entity_id,
                target_entity_id=po_entity.entity_id,
                relation="MATCHES",
                confidence=candidate.confidence,
                evidence_ids=evidence_ids,
                annotations=candidate.as_dict(),
                producer=PRODUCER,
            )
            proposals.append({"relationship_id": str(relationship.relationship_id), **candidate.as_dict()})
    return proposals
