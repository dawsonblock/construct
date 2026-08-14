#!/usr/bin/env python
"""Create an organization and issue its first API key.

    python scripts/create_organization.py --slug acme --name "Acme Construction"

The key is printed once and stored only as a SHA-256 digest. There is no way to
recover it afterwards; issue a new one instead.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from construction_ai.persistence.repositories import Repositories  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", required=True, help="stable machine identifier, e.g. 'acme'")
    parser.add_argument("--name", required=True, help="display name")
    parser.add_argument("--label", default="initial", help="label for the issued key")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args(argv)

    repos = Repositories.from_env()
    organization = repos.organizations.create(slug=args.slug, name=args.name)
    token = repos.organizations.issue_api_key(organization_id=organization.organization_id, label=args.label)

    if args.json:
        print(json.dumps({"organization_id": str(organization.organization_id), "slug": organization.slug, "api_key": token}))
    else:
        print(f"organization {organization.slug} ({organization.organization_id})")
        print(f"API key (shown once): {token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
