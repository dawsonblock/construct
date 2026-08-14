"""ERP supplier -> local company resolution, independent of the invoice's vendor.

Item 6: the v0.3 pipeline copied the invoice's resolved vendor onto the purchase
order before comparing them, so `vendor_match` was tautological. Resolution of
the ERP PO's supplier must instead follow its own chain and never borrow state
from the invoice.

Resolution priority (highest first):

  1. ERP supplier ID mapping   — companies.erp_supplier_id == ERP supplier id
  2. Registered business/tax ID — companies.tax_id == supplier tax id
  3. Verified organization mapping — external_entity_mappings row
  4. Exact normalized legal identity — normalized names match exactly
  5. Strong address/contact evidence — (future; not auto-merged)
  6. Fuzzy similarity — SUGGESTION ONLY, never an authoritative resolution

A resolution is either a single confident local company or `None` (abstain). This
module never merges vendors and never creates a company. Fuzzy matching stays
advisory and is returned as a *suggestion*, not a resolution.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from uuid import UUID

from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories import Repositories


@dataclass(frozen=True)
class SupplierResolution:
    """The outcome of resolving an ERP supplier to a local company.

    `company_id` is None when no authoritative resolution was reached; `reason`
    says which priority was tried and why it abstained. `suggestion` carries a
    fuzzy candidate that a human may promote, never an automatic match.
    """

    company_id: UUID | None
    reason: str
    suggestion: UUID | None = None
    priority_used: int | None = None


def normalize_name(name: str | None) -> str:
    """Casefold, strip accents and legal suffixes for identity comparison."""
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", name)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = n.lower().strip()
    for suffix in (" ltd.", " ltd", " inc.", " inc", " corp.", " corp", " llc", " co.", " co", " limited", " incorporated"):
        if n.endswith(suffix):
            n = n[: -len(suffix)].rstrip()
    return n


def resolve_supplier_to_company(
    repos: Repositories,
    scope: Scope,
    *,
    erp_supplier_id: str | None = None,
    supplier_name: str | None = None,
    tax_id: str | None = None,
    source_system: str = "ERPNext",
) -> SupplierResolution:
    """Resolve an ERP supplier to one local company, or abstain.

    The ERP PO record carries the supplier id ERP actually returned; that is the
    authoritative input (`erp_supplier_id`). A supplier name/tax id may also be
    available from a supplier snapshot and feeds the lower-priority chains.
    """
    # Priority 1: ERP supplier ID mapping.
    if erp_supplier_id:
        company = repos.companies.find_by_erp_supplier(scope=scope, erp_supplier_id=erp_supplier_id)
        if company:
            return SupplierResolution(UUID(company.company_id), "erp_supplier_id", priority_used=1)

    # Priority 2: registered business/tax id.
    if tax_id:
        match = _find_by_tax_id(repos, scope, tax_id)
        if match:
            return SupplierResolution(match, "tax_id", priority_used=2)

    # Priority 3: verified organization mapping.
    if erp_supplier_id:
        mapped = _find_verified_mapping(repos, scope, source_system=source_system, external_entity_id=erp_supplier_id)
        if mapped:
            return SupplierResolution(mapped, "verified_mapping", priority_used=3)

    # Priority 4: exact normalized legal identity.
    if supplier_name:
        normalized = normalize_name(supplier_name)
        if normalized:
            match = _find_by_normalized_name(repos, scope, normalized)
            if match:
                return SupplierResolution(match, "exact_normalized_name", priority_used=4)

    # Priority 5/6: address/contact and fuzzy similarity are suggestion-only.
    suggestion = None
    if supplier_name:
        suggestion = _fuzzy_suggestion(repos, scope, supplier_name)
    return SupplierResolution(None, "no_authoritative_resolution", suggestion=suggestion)


def _find_by_tax_id(repos: Repositories, scope: Scope, tax_id: str) -> UUID | None:
    with repos.db.scoped(scope) as cur:
        cur.execute(
            "SELECT company_id FROM companies WHERE organization_id = %s AND tax_id = %s LIMIT 1",
            (scope.organization_id, tax_id),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _find_verified_mapping(repos: Repositories, scope: Scope, *, source_system: str, external_entity_id: str) -> UUID | None:
    with repos.db.scoped(scope) as cur:
        cur.execute(
            """SELECT local_entity_id FROM external_entity_mappings
               WHERE organization_id = %s AND source_system = %s
                 AND external_entity_type = 'Supplier' AND external_entity_id = %s
                 AND local_entity_type = 'company'
               LIMIT 1""",
            (scope.organization_id, source_system, external_entity_id),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _find_by_normalized_name(repos: Repositories, scope: Scope, normalized: str) -> UUID | None:
    """Exact normalized legal identity. Abstains on ambiguity: two companies with
    the same normalized name are not auto-merged — a human must disambiguate."""
    matches: list = []
    with repos.db.scoped(scope) as cur:
        cur.execute(
            "SELECT company_id, name, aliases FROM companies WHERE organization_id = %s",
            (scope.organization_id,),
        )
        for company_id, name, aliases in cur.fetchall():
            if normalize_name(name) == normalized or any(normalize_name(a) == normalized for a in (aliases or [])):
                matches.append(company_id)
    if len(matches) == 1:
        return matches[0]
    return None  # zero or ambiguous -> abstain rather than guess


def _fuzzy_suggestion(repos: Repositories, scope: Scope, supplier_name: str) -> UUID | None:
    """A weak, advisory candidate only. Never an authoritative resolution.

    Uses a simple token-overlap ratio; the real scorer lives in
    construction_ai/matching and is calibrated separately. This exists so a
    reviewer can be pointed at a plausible company without the system asserting
    they are the same.
    """
    target = normalize_name(supplier_name)
    if not target:
        return None
    target_tokens = set(target.split())
    if not target_tokens:
        return None
    best: tuple[float, UUID | None] = (0.0, None)
    with repos.db.scoped(scope) as cur:
        cur.execute("SELECT company_id, name FROM companies WHERE organization_id = %s", (scope.organization_id,))
        for company_id, name in cur.fetchall():
            candidate = normalize_name(name)
            if not candidate:
                continue
            overlap = len(target_tokens & set(candidate.split())) / max(len(target_tokens), 1)
            if overlap > best[0]:
                best = (overlap, company_id)
    # A suggestion only above a conservative threshold; never resolve on it.
    return best[1] if best[0] >= 0.8 else None
