"""Labelled company-matching corpus.

The hard part of entity resolution is not "ABC Electric" vs "ABC Electric Ltd."
It is knowing when two similar names are two different companies. Roughly half
of these cases are near-misses on purpose.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakeCompany:
    company_id: str
    name: str
    reference: str | None = None
    aliases: list[str] = field(default_factory=list)
    erp_supplier_id: str | None = None
    tax_id: str | None = None
    type: str = "subcontractor"


@dataclass
class Case:
    name: str
    left: FakeCompany
    right: FakeCompany
    is_match: bool
    why: str


def adversarial_cases() -> list[Case]:
    return [
        Case(
            "same tax id, different spellings",
            FakeCompany("c1", "ABC Electric Ltd.", tax_id="GST-100 200 300"),
            FakeCompany("c2", "A.B.C. Electric", tax_id="gst100200300"),
            True,
            "a business identifier is identification; formatting is not",
        ),
        Case(
            "identical name, different tax id",
            FakeCompany("c3", "Northern Electric", tax_id="GST-111"),
            FakeCompany("c4", "Northern Electric", tax_id="GST-222"),
            False,
            "two real companies can share a name; identifiers say they are not one",
        ),
        Case(
            "same normalized legal name, no identifiers",
            FakeCompany("c5", "Northline Drywall Inc."),
            FakeCompany("c6", "Northline Drywall"),
            True,
            "suffix normalization is safe; this is the ordinary duplicate",
        ),
        Case(
            "alias overlap only",
            FakeCompany("c7", "ABC Electric", aliases=["ABC Electrical Services"]),
            FakeCompany("c8", "ABC Electrical Services"),
            True,
            "an alias is a name the company uses for itself",
        ),
        Case(
            "same ERP supplier record",
            FakeCompany("c9", "ABC Electric", erp_supplier_id="SUPP-0001"),
            FakeCompany("c10", "ABC Elec.", erp_supplier_id="SUPP-0001"),
            True,
            "the ERP already treats them as one supplier",
        ),
        Case(
            "similar names, unrelated companies",
            FakeCompany("c11", "Northline Drywall"),
            FakeCompany("c12", "Northline Drilling"),
            False,
            "one token apart, entirely different trades",
        ),
        Case(
            "same first word only",
            FakeCompany("c13", "Prairie Electric"),
            FakeCompany("c14", "Prairie Mechanical"),
            False,
            "regional prefixes are shared across every trade in a city",
        ),
        Case(
            "abbreviation vs full name, no identifiers",
            FakeCompany("c15", "ABC Elec."),
            FakeCompany("c16", "ABC Electric"),
            False,
            "plausible, but fuzzy-only: must stay below auto-promotion",
        ),
        Case(
            "completely different",
            FakeCompany("c17", "ABC Electric"),
            FakeCompany("c18", "Wilson Plumbing"),
            False,
            "the easy negative",
        ),
        Case(
            "same name, conflicting tax id, shared alias",
            FakeCompany("c19", "Summit Roofing", aliases=["Summit Roofing Group"], tax_id="GST-900"),
            FakeCompany("c20", "Summit Roofing", aliases=["Summit Roofing Group"], tax_id="GST-901"),
            False,
            "a contradicted identifier outranks every corroborating soft signal",
        ),
    ]


def corpus() -> list[Case]:
    return adversarial_cases()
