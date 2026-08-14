"""Ordered, idempotent, checksummed schema migration.

Applies `migrations/*.sql` in filename order, one transaction per file, and
records each application in `schema_migrations`. A file that changes after it has
been applied is a hard error: silently re-running mutated DDL is how a schema and
its audit history drift apart.

`scripts/migrate.py` is the CLI over this module.
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename text PRIMARY KEY,
  checksum text NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now()
);
"""


class MigrationError(RuntimeError):
    pass


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def discover(directory: Path | None = None) -> list[Path]:
    directory = directory or MIGRATIONS_DIR
    if not directory.is_dir():
        raise MigrationError(f"migrations directory not found: {directory}")
    return sorted(directory.glob("*.sql"))


def plan(files: list[Path], applied: dict[str, str]) -> tuple[list[Path], list[str]]:
    """Return (pending, drifted). Drifted files changed after being applied."""
    pending: list[Path] = []
    drifted: list[str] = []
    for path in files:
        previous = applied.get(path.name)
        if previous is None:
            pending.append(path)
        elif previous != checksum(path):
            drifted.append(path.name)
    return pending, drifted


def connect(dsn: str, *, attempts: int = 30, delay: float = 1.0):
    """Postgres in compose can accept-then-drop connections on first boot."""
    import psycopg

    last: Exception | None = None
    for _ in range(attempts):
        try:
            return psycopg.connect(dsn)
        except psycopg.OperationalError as exc:  # pragma: no cover - timing dependent
            last = exc
            time.sleep(delay)
    raise MigrationError(f"could not connect to database after {attempts} attempts: {last}")


def applied_state(conn) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute(BOOTSTRAP)
        conn.commit()
        cur.execute("SELECT filename, checksum FROM schema_migrations")
        return dict(cur.fetchall())


def migrate(dsn: str, *, dry_run: bool = False, directory: Path | None = None, on_apply=None) -> dict:
    files = discover(directory)
    conn = connect(dsn)
    try:
        applied = applied_state(conn)
        pending, drifted = plan(files, applied)
        if drifted:
            raise MigrationError(
                "migration files changed after being applied: "
                + ", ".join(drifted)
                + ". Add a new migration instead of editing an applied one."
            )
        if dry_run:
            return {"applied": [], "pending": [p.name for p in pending], "already_applied": sorted(applied)}
        for path in pending:
            with conn.cursor() as cur:
                cur.execute(path.read_text())
                cur.execute(
                    "INSERT INTO schema_migrations(filename, checksum) VALUES(%s, %s)",
                    (path.name, checksum(path)),
                )
            conn.commit()
            if on_apply:
                on_apply(path.name)
        return {"applied": [p.name for p in pending], "pending": [], "already_applied": sorted(applied)}
    finally:
        conn.close()
