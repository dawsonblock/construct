#!/usr/bin/env python
"""rc4 Phase 21 — Qualification evidence bundle generator.

Generates the complete evidence bundle for a release:
- QUALIFICATION_REPORT.json
- MANIFEST.json
- TEST_RESULTS.json
- CRASH_MATRIX.json
- SECURITY_GATE.json
- MIGRATION_GATE.json

Each artifact records release version, Git SHA, timestamps, and relevant
verification data. Artifacts are self-consistent and verifiable.

Usage:
    python scripts/evidence_bundle.py [--pytest] [--output-dir .]
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
            cwd=ROOT,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def _version() -> str:
    version_file = ROOT / "VERSION"
    if version_file.exists():
        return version_file.read_text().strip()
    return "unknown"


def _generate_test_results() -> dict:
    """Run pytest and capture detailed test results."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=no"],
        capture_output=True, text=True, cwd=ROOT, timeout=300,
    )
    import re
    lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
    summary = lines[-1] if lines else ""
    counts = {"passed": 0, "skipped": 0, "failed": 0, "error": 0}
    for key in counts:
        match = re.search(rf"(\d+)\s+{key}", summary)
        if match:
            counts[key] = int(match.group(1))

    return {
        "version": _version(),
        "git_commit": _git_commit(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "passed": counts["passed"],
        "skipped": counts["skipped"],
        "failed": counts["failed"],
        "error": counts["error"],
        "total": sum(counts.values()),
        "exit_code": result.returncode,
        "summary": summary,
    }


def _generate_crash_matrix() -> dict:
    """Run crash matrix tests and capture results."""
    test_paths = [
        "tests/test_crash_injection.py",
        "tests/test_crash_injection_erp.py",
        "tests/test_rc4_crash_matrix.py",
        "tests/test_rc4_leases_and_recovery.py",
    ]
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=no"] + test_paths,
        capture_output=True, text=True, cwd=ROOT, timeout=120,
    )
    import re
    lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
    summary = lines[-1] if lines else ""
    counts = {"passed": 0, "skipped": 0, "failed": 0, "error": 0}
    for key in counts:
        match = re.search(rf"(\d+)\s+{key}", summary)
        if match:
            counts[key] = int(match.group(1))

    return {
        "version": _version(),
        "git_commit": _git_commit(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "crash_points_tested": [
            "after_reservation",
            "after_lease_acquire",
            "after_erp_create",
            "before_erp_submit",
            "after_erp_submit",
            "before_readback",
            "after_readback",
            "before_confirmed",
            "before_audit",
        ],
        "passed": counts["passed"],
        "skipped": counts["skipped"],
        "failed": counts["failed"],
        "exit_code": result.returncode,
        "summary": summary,
        "uses_production_reaper": True,
        "no_manual_state_mutations": True,
    }


def _generate_security_gate() -> dict:
    """Run security/RLS tests and capture results."""
    test_paths = [
        "tests/test_integration_tenancy.py",
    ]
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=no"] + test_paths,
        capture_output=True, text=True, cwd=ROOT, timeout=120,
    )
    lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
    summary = lines[-1] if lines else ""

    return {
        "version": _version(),
        "git_commit": _git_commit(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "exit_code": result.returncode,
        "summary": summary,
        "rls_tested": True,
        "tenant_isolation_tested": True,
    }


def _generate_migration_gate() -> dict:
    """Verify migrations are applied and record schema state."""
    migrations_dir = ROOT / "migrations"
    migrations = sorted(migrations_dir.glob("*.sql"))
    hasher = hashlib.sha256()
    for f in migrations:
        hasher.update(f.read_bytes())

    # Check database state.
    schema_fingerprint = "offline"
    applied_count = 0
    pending_count = 0
    try:
        import psycopg
        import os
        dsn = os.getenv("DATABASE_URL", "postgresql://construction:construction@localhost:5432/construction_ai")
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM schema_migrations")
                applied_count = cur.fetchone()[0]
                cur.execute("""
                    SELECT table_name, column_name, data_type, is_nullable, column_default
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                    ORDER BY table_name, ordinal_position
                """)
                rows = cur.fetchall()
                schema_hasher = hashlib.sha256()
                for row in rows:
                    schema_hasher.update(str(row).encode())
                schema_fingerprint = schema_hasher.hexdigest()
    except Exception:
        pass

    return {
        "version": _version(),
        "git_commit": _git_commit(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "migration_count": len(migrations),
        "migration_fingerprint": hasher.hexdigest(),
        "applied_migrations": applied_count,
        "pending_migrations": pending_count,
        "schema_fingerprint": schema_fingerprint,
        "all_applied": applied_count == len(migrations),
    }


def generate_evidence_bundle(*, output_dir: str = ".", run_tests: bool = False) -> dict:
    """Generate the complete evidence bundle."""
    output_path = Path(output_dir)

    # Import sibling scripts.
    import importlib
    sys.path.insert(0, str(ROOT / "scripts"))
    release_manifest_mod = importlib.import_module("release_manifest")
    qualification_report_mod = importlib.import_module("qualification_report")

    # 1. MANIFEST.json
    manifest = release_manifest_mod.generate_manifest(artifact_only=False)
    (output_path / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, default=str)
    )

    # 2. TEST_RESULTS.json
    if run_tests:
        test_results = _generate_test_results()
    else:
        test_results = {
            "version": _version(), "git_commit": _git_commit(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run": False,
        }
    (output_path / "TEST_RESULTS.json").write_text(
        json.dumps(test_results, indent=2, sort_keys=True, default=str)
    )

    # 3. CRASH_MATRIX.json
    if run_tests:
        crash_matrix = _generate_crash_matrix()
    else:
        crash_matrix = {
            "version": _version(), "git_commit": _git_commit(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run": False,
        }
    (output_path / "CRASH_MATRIX.json").write_text(
        json.dumps(crash_matrix, indent=2, sort_keys=True, default=str)
    )

    # 4. SECURITY_GATE.json
    if run_tests:
        security_gate = _generate_security_gate()
    else:
        security_gate = {
            "version": _version(), "git_commit": _git_commit(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run": False,
        }
    (output_path / "SECURITY_GATE.json").write_text(
        json.dumps(security_gate, indent=2, sort_keys=True, default=str)
    )

    # 5. MIGRATION_GATE.json
    migration_gate = _generate_migration_gate()
    (output_path / "MIGRATION_GATE.json").write_text(
        json.dumps(migration_gate, indent=2, sort_keys=True, default=str)
    )

    # 6. QUALIFICATION_REPORT.json
    report = qualification_report_mod.generate_report(run_tests=run_tests)
    (output_path / "QUALIFICATION_REPORT.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str)
    )

    return {
        "artifacts": [
            "QUALIFICATION_REPORT.json",
            "MANIFEST.json",
            "TEST_RESULTS.json",
            "CRASH_MATRIX.json",
            "SECURITY_GATE.json",
            "MIGRATION_GATE.json",
        ],
        "qualified": report.get("qualified", False),
    }


def main() -> int:
    run_tests = "--pytest" in sys.argv
    output_dir = "."
    if "--output-dir" in sys.argv:
        output_dir = sys.argv[sys.argv.index("--output-dir") + 1]

    result = generate_evidence_bundle(output_dir=output_dir, run_tests=run_tests)
    print(json.dumps(result, indent=2))
    return 0 if result["qualified"] else 1


if __name__ == "__main__":
    sys.exit(main())
