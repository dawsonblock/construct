#!/usr/bin/env python
"""Give the application role its password.

Migration 003 creates `construction_app` as a non-superuser with no password —
a migration cannot read secrets. This sets the password from the environment so
the API and worker can connect as a role that RLS actually applies to.

Runs as the database owner (`DATABASE_URL`), not as the app role.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from construction_ai.persistence.migrations import connect  # noqa: E402

ROLE = "construction_app"


def main() -> int:
    dsn = os.getenv("DATABASE_URL", "").strip()
    password = os.getenv("APP_DB_PASSWORD", "").strip()
    if not dsn.startswith(("postgresql://", "postgres://")):
        print("DATABASE_URL must be a PostgreSQL DSN", file=sys.stderr)
        return 2
    if not password:
        print("APP_DB_PASSWORD must be set", file=sys.stderr)
        return 2

    conn = connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %s", (ROLE,))
            row = cur.fetchone()
            if row is None:
                print(f"role {ROLE} does not exist; apply migrations first", file=sys.stderr)
                return 1
            if row[0] or row[1]:
                # A superuser or BYPASSRLS role silently disables every policy.
                print(f"refusing to use {ROLE}: it can bypass row-level security", file=sys.stderr)
                return 1
            # ALTER ROLE will not take a bound parameter, so compose the
            # statement with psycopg's quoting rather than string interpolation.
            from psycopg import sql

            cur.execute(sql.SQL("ALTER ROLE {} WITH PASSWORD {}").format(sql.Identifier(ROLE), sql.Literal(password)))
        conn.commit()
    finally:
        conn.close()
    print(f"application role {ROLE} configured")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
