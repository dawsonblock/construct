"""Labelled invoice → purchase-order corpus.

Hand-written adversarial cases first, then a seeded generator for volume. Plain
records, no database: the matcher under test is a pure function, and a corpus
that needs a stack to run is a corpus nobody runs.

Every case states what the right answer is and *why*, so a future change that
improves the headline number while breaking a case has to argue with the label.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta

VENDOR_A = "11111111-1111-4111-8111-111111111111"
VENDOR_B = "22222222-2222-4222-8222-222222222222"
PROJECT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PROJECT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


@dataclass
class FakeInvoice:
    invoice_id: str
    invoice_number: str
    total: float
    po_number: str | None = None
    vendor_company_id: str | None = VENDOR_A
    project_id: str | None = PROJECT_A
    reference: str | None = None
    invoice_date: date | None = None
    quote_number: str | None = None


@dataclass
class FakePurchaseOrder:
    po_id: str
    po_number: str
    amount: float
    vendor_company_id: str | None = VENDOR_A
    project_id: str | None = PROJECT_A
    reference: str | None = None
    ordered_on: date | None = None
    quote_number: str | None = None


@dataclass
class Case:
    name: str
    invoice: FakeInvoice
    purchase_order: FakePurchaseOrder
    is_match: bool
    why: str


ORDERED = date(2026, 6, 1)
BILLED = date(2026, 6, 20)


def adversarial_cases() -> list[Case]:
    """The cases that matter. Volume is generated; these are chosen."""
    return [
        Case(
            "exact reference, same vendor, same amount",
            FakeInvoice("i1", "8831", 4760.00, "PO-1042-17", invoice_date=BILLED),
            FakePurchaseOrder("p1", "PO-1042-17", 4760.00, ordered_on=ORDERED),
            True,
            "every available signal agrees",
        ),
        Case(
            "exact reference, wrong vendor",
            FakeInvoice("i2", "8832", 4760.00, "PO-1042-17", vendor_company_id=VENDOR_B, invoice_date=BILLED),
            FakePurchaseOrder("p1", "PO-1042-17", 4760.00, ordered_on=ORDERED),
            False,
            "a reference someone typed does not outrank who is billing",
        ),
        Case(
            "same vendor and amount, different project",
            FakeInvoice("i3", "8833", 4760.00, None, project_id=PROJECT_B, invoice_date=BILLED),
            FakePurchaseOrder("p1", "PO-1042-17", 4760.00, ordered_on=ORDERED),
            False,
            "shared vendors across projects are the normal case, not evidence",
        ),
        Case(
            "reference matches but amount is wildly off",
            FakeInvoice("i4", "8834", 19000.00, "PO-1042-17", invoice_date=BILLED),
            FakePurchaseOrder("p1", "PO-1042-17", 4760.00, ordered_on=ORDERED),
            False,
            "a 4x overrun on an exact reference is a problem, not a match",
        ),
        Case(
            "reference matches, amount slightly over within band",
            FakeInvoice("i5", "8835", 4900.00, "PO-1042-17", invoice_date=BILLED),
            FakePurchaseOrder("p1", "PO-1042-17", 4760.00, ordered_on=ORDERED),
            True,
            "small variances are ordinary; the amount signal degrades rather than vetoes",
        ),
        Case(
            "no reference, same vendor and exact amount",
            FakeInvoice("i6", "8836", 4760.00, None, invoice_date=BILLED),
            FakePurchaseOrder("p1", "PO-1042-17", 4760.00, ordered_on=ORDERED),
            True,
            "corroboration without a reference is a real but weaker match",
        ),
        Case(
            "no reference, same vendor, unrelated amount",
            FakeInvoice("i7", "8837", 812.55, None, invoice_date=BILLED),
            FakePurchaseOrder("p1", "PO-1042-17", 4760.00, ordered_on=ORDERED),
            False,
            "vendor alone is not identification",
        ),
        Case(
            "reference differs only by punctuation",
            FakeInvoice("i8", "8838", 4760.00, "po 1042 17", invoice_date=BILLED),
            FakePurchaseOrder("p1", "PO-1042-17", 4760.00, ordered_on=ORDERED),
            True,
            "identifier normalization must survive formatting",
        ),
        Case(
            "invoice predates the purchase order",
            FakeInvoice("i9", "8839", 4760.00, None, invoice_date=date(2026, 5, 1)),
            FakePurchaseOrder("p1", "PO-1042-17", 4760.00, ordered_on=ORDERED),
            False,
            "work billed before it was ordered needs a person, not a match",
        ),
        Case(
            "vendor unresolved on the invoice",
            FakeInvoice("i10", "8840", 4760.00, "PO-1042-17", vendor_company_id=None, invoice_date=BILLED),
            FakePurchaseOrder("p1", "PO-1042-17", 4760.00, ordered_on=ORDERED),
            True,
            "an unavailable signal must not be scored as disagreement",
        ),
    ]


def generated_cases(count: int = 400, seed: int = 20260813) -> list[Case]:
    """Volume, deterministically. The seed is fixed so the corpus is a constant."""
    rng = random.Random(seed)
    cases: list[Case] = []
    for index in range(count):
        amount = round(rng.uniform(500, 50_000), 2)
        reference = f"PO-{1000 + index}-{rng.randrange(10, 99)}"
        ordered = ORDERED + timedelta(days=rng.randrange(0, 60))

        kind = index % 5
        if kind == 0:  # true: everything agrees
            cases.append(Case(
                f"gen-{index}-exact",
                FakeInvoice(f"gi{index}", str(index), amount, reference, invoice_date=ordered + timedelta(days=rng.randrange(1, 45))),
                FakePurchaseOrder(f"gp{index}", reference, amount, ordered_on=ordered),
                True, "generated exact match",
            ))
        elif kind == 1:  # true: small variance
            cases.append(Case(
                f"gen-{index}-variance",
                FakeInvoice(f"gi{index}", str(index), round(amount * 1.03, 2), reference, invoice_date=ordered + timedelta(days=20)),
                FakePurchaseOrder(f"gp{index}", reference, amount, ordered_on=ordered),
                True, "generated 3% variance",
            ))
        elif kind == 2:  # false: different vendor
            cases.append(Case(
                f"gen-{index}-vendor",
                FakeInvoice(f"gi{index}", str(index), amount, reference, vendor_company_id=VENDOR_B, invoice_date=ordered + timedelta(days=10)),
                FakePurchaseOrder(f"gp{index}", reference, amount, ordered_on=ordered),
                False, "generated vendor mismatch",
            ))
        elif kind == 3:  # false: unrelated PO
            cases.append(Case(
                f"gen-{index}-unrelated",
                FakeInvoice(f"gi{index}", str(index), round(rng.uniform(500, 50_000), 2), f"PO-{9000 + index}-01", invoice_date=ordered + timedelta(days=5)),
                FakePurchaseOrder(f"gp{index}", reference, amount, ordered_on=ordered),
                False, "generated unrelated purchase order",
            ))
        else:  # false: right vendor, wrong project
            cases.append(Case(
                f"gen-{index}-project",
                FakeInvoice(f"gi{index}", str(index), amount, None, project_id=PROJECT_B, invoice_date=ordered + timedelta(days=15)),
                FakePurchaseOrder(f"gp{index}", reference, amount, ordered_on=ordered),
                False, "generated cross-project",
            ))
    return cases


def corpus() -> list[Case]:
    return adversarial_cases() + generated_cases()
