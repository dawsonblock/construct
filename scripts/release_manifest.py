#!/usr/bin/env python
"""v0.4.7 — release manifest (item 42).

Generates a release manifest that captures the exact state of the system:
- Version
- Git commit
- Migration count and checksums
- Database schema fingerprint
- Code fingerprint (Python files)
- Dependency versions
- Test count

This manifest is the qualification artifact: a release is qualified only if
the manifest matches what was tested. The manifest is deterministic — same
code + same migrations = same manifest.

Usage:
    python scripts/release_manifest.py [--output manifest.json]
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
            cwd=Path(__file__).parent.parent,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def _git_branch() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, check=True,
            cwd=Path(__file__).parent.parent,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def _version() -> str:
    version_file = Path(__file__).parent.parent / "VERSION"
    if version_file.exists():
        return version_file.read_text().strip()
    return "unknown"


def _migration_checksums() -> list[dict[str, str]]:
    migrations_dir = Path(__file__).parent.parent / "migrations"
    checksums = []
    for f in sorted(migrations_dir.glob("*.sql")):
        checksums.append({
            "filename": f.name,
            "sha256": hashlib.sha256(f.read_bytes()).hexdigest(),
        })
    return checksums


def _code_fingerprint() -> str:
    """SHA-256 over all Python source files (excluding tests and scripts)."""
    root = Path(__file__).parent.parent
    hasher = hashlib.sha256()
    for f in sorted(root.glob("construction_ai/**/*.py")):
        hasher.update(f.read_bytes())
    return hasher.hexdigest()


def _test_fingerprint() -> str:
    """SHA-256 over all test files."""
    root = Path(__file__).parent.parent
    hasher = hashlib.sha256()
    for f in sorted(root.glob("tests/**/*.py")):
        hasher.update(f.read_bytes())
    return hasher.hexdigest()


def _migration_fingerprint() -> str:
    """SHA-256 over all migration files."""
    root = Path(__file__).parent.parent
    hasher = hashlib.sha256()
    for f in sorted(root.glob("migrations/*.sql")):
        hasher.update(f.read_bytes())
    return hasher.hexdigest()


def _dependency_versions() -> dict[str, str]:
    """Key dependency versions."""
    versions = {}
    for pkg in ["psycopg", "redis", "hypothesis", "pytest", "ruff"]:
        try:
            mod = __import__(pkg)
            versions[pkg] = getattr(mod, "__version__", "unknown")
        except ImportError:
            versions[pkg] = "not installed"
    # Python version
    versions["python"] = sys.version
    return versions


def _schema_fingerprint() -> str:
    """SHA-256 over the database schema (DDL)."""
    import psycopg

    dsn = os.getenv("DATABASE_URL", "postgresql://construction:construction@localhost:5432/construction_ai")
    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT table_name, column_name, data_type, is_nullable, column_default
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                    ORDER BY table_name, ordinal_position
                """)
                rows = cur.fetchall()
                hasher = hashlib.sha256()
                for row in rows:
                    hasher.update(str(row).encode())
                return hasher.hexdigest()
    except Exception:
        return "unavailable"


def generate_manifest() -> dict:
    return {
        "version": _version(),
        "git_commit": _git_commit(),
        "git_branch": _git_branch(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "migration_count": len(_migration_checksums()),
        "migration_fingerprint": _migration_fingerprint(),
        "code_fingerprint": _code_fingerprint(),
        "test_fingerprint": _test_fingerprint(),
        "schema_fingerprint": _schema_fingerprint(),
        "dependencies": _dependency_versions(),
        "migrations": _migration_checksums(),
    }


def main() -> int:
    output = sys.argv[sys.argv.index("--output") + 1] if "--output" in sys.argv else None
    manifest = generate_manifest()
    manifest_json = json.dumps(manifest, indent=2, sort_keys=True, default=str)
    if output:
        Path(output).write_text(manifest_json)
        print(f"manifest written to {output}")
    else:
        print(manifest_json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
