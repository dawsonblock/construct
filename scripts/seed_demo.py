#!/usr/bin/env python
"""Seed the demo organizations used by the acceptance and isolation gates.

Two organizations, deliberately: the isolation gate needs a second tenant whose
data the first must never reach. Both contain a project called `PRJ-0042` —
references are unique per organization, never globally, and the gate proves it.

Projects here line up with `apps/erpnext_stub/fixtures.json`. This is fixture
data; the real path to a populated organization is the Phase 6 importer.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from construction_ai.persistence.db import Scope  # noqa: E402
from construction_ai.persistence.repositories import Repositories  # noqa: E402

CREDENTIALS_PATH = ROOT / ".demo-credentials.json"

FIXTURES = {
    "demo": {
        "name": "Blackrock Construction (demo)",
        "companies": [
            {"reference": "ABC-ELECTRIC", "name": "ABC Electric", "type": "subcontractor",
             "aliases": ["ABC Electric Ltd.", "A.B.C. Electric"], "erp_supplier_id": "ABC Electric"},
            {"reference": "NORTHLINE", "name": "Northline Drywall", "type": "subcontractor",
             "aliases": ["Northline Drywall Inc."], "erp_supplier_id": "Northline Drywall"},
        ],
        "projects": [
            {"reference": "PRJ-0042", "name": "Wilson Residence", "address": "421 8th St E",
             "identifiers": {"po": ["PO-1042-17"], "thread": ["THR-0042"]}, "companies": ["ABC-ELECTRIC"]},
            {"reference": "PRJ-0063", "name": "Parker Residence", "address": "900 Main St",
             "identifiers": {"po": ["PO-1077-02"], "thread": ["THR-0063"]}, "companies": ["NORTHLINE"]},
        ],
    },
    # Same project reference, same vendor name, different tenant. If anything
    # leaks between these two, the gate will say so.
    "rival": {
        "name": "Rival Builders (demo)",
        "companies": [
            {"reference": "ABC-ELECTRIC", "name": "ABC Electric", "type": "subcontractor",
             "aliases": [], "erp_supplier_id": "ABC Electric"},
        ],
        "projects": [
            {"reference": "PRJ-0042", "name": "Rival Tower", "address": "77 Rival Way",
             "identifiers": {"po": ["PO-9999-01"]}, "companies": ["ABC-ELECTRIC"]},
        ],
    },
}


def seed(repos: Repositories) -> dict[str, dict]:
    credentials: dict[str, dict] = {}
    for slug, fixture in FIXTURES.items():
        organization = repos.organizations.create(slug=slug, name=fixture["name"])
        scope = Scope(organization.organization_id)

        company_ids: dict[str, UUID] = {}
        for spec in fixture["companies"]:
            company = repos.companies.create(
                scope=scope,
                reference=spec["reference"],
                name=spec["name"],
                company_type=spec["type"],
                aliases=spec["aliases"],
                erp_supplier_id=spec["erp_supplier_id"],
            )
            company_ids[spec["reference"]] = UUID(company.company_id)

        for spec in fixture["projects"]:
            project = repos.projects.create(
                scope=scope,
                reference=spec["reference"],
                name=spec["name"],
                address=spec["address"],
                identifiers=spec["identifiers"],
            )
            for company_reference in spec["companies"]:
                repos.projects.add_company(
                    scope=scope, project_id=UUID(project.project_id), company_id=company_ids[company_reference]
                )

        token = repos.organizations.issue_api_key(organization_id=organization.organization_id, label="demo-seed")
        credentials[slug] = {"organization_id": str(organization.organization_id), "api_key": token}
    return credentials


def main() -> int:
    credentials = seed(Repositories.from_env())
    # Written for the acceptance and isolation gates to consume. Demo keys for a
    # local stack; gitignored, and never a path for real credentials.
    CREDENTIALS_PATH.write_text(json.dumps(credentials, indent=2))
    for slug, entry in credentials.items():
        print(f"seeded organization {slug} ({entry['organization_id']})")
    print(f"demo API keys written to {CREDENTIALS_PATH.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
