#!/usr/bin/env python
"""CLI over construction_ai.persistence.migrations.

    python scripts/migrate.py            # apply pending migrations
    python scripts/migrate.py --status   # report without changing anything
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from construction_ai.persistence.migrations import MigrationError, migrate  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="report pending migrations without applying")
    parser.add_argument("--dsn", default=None, help="database URL (defaults to $DATABASE_URL)")
    args = parser.parse_args(argv)

    dsn = (args.dsn or os.getenv("DATABASE_URL", "")).strip()
    if not dsn.startswith(("postgresql://", "postgres://")):
        print("DATABASE_URL must be a PostgreSQL DSN; SQLite manages its own schema.", file=sys.stderr)
        return 2

    try:
        result = migrate(dsn, dry_run=args.status, on_apply=lambda name: print(f"applied {name}"))
    except MigrationError as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 1

    if args.status:
        print(f"applied: {len(result['already_applied'])}  pending: {result['pending'] or 'none'}")
    elif not result["applied"]:
        print("schema up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
