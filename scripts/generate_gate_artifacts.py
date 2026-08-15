#!/usr/bin/env python
"""v0.5.0-rc9 — generate all gate evidence artifacts from the current build.

This script regenerates the supporting qualification artifacts so they all
refer to the same exact commit, version, and migration count. The rc6 archive
had rc5-era gate files mixed with rc6 qualification claims, which made the
evidence bundle internally inconsistent.

Generates:
  - TEST_RESULTS.json    — raw pytest results with version/commit provenance
  - CRASH_MATRIX.json    — crash/recovery test results
  - SECURITY_GATE.json   — security gate results
  - MIGRATION_GATE.json  — migration/upgrade gate results

All artifacts include:
  - version (from VERSION file)
  - git_commit (current HEAD)
  - git_branch (current branch)
  - generated_at (UTC timestamp)
  - exit_code / passed / summary

Usage:
    python scripts/generate_gate_artifacts.py

    With --pytest, runs the actual tests. Without it, uses the qualification
    report's data if available.
"""
from __future__ import annotations

import hashlib
import json
import os
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


def _git_branch() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, check=True,
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


def _lock_hash() -> str:
    """rc8: Use the shared canonical dependency lock hash.

    Previously this hashed only requirements.lock.txt, while
    qualification_report.py hashed both lock files. This mismatch
    caused the artifact consistency gate to miss a dependency identity
    disagreement. Now both use the same shared helper.
    """
    from qualification_identity import compute_dependency_lock_hash
    return compute_dependency_lock_hash()


def _payload_tree_hash() -> str | None:
    """Read the payload_tree_hash from the current payload manifest."""
    for name in ("PAYLOAD_MANIFEST.json", "MANIFEST.json"):
        p = ROOT / name
        if p.exists():
            try:
                manifest = json.loads(p.read_text())
                return manifest.get("tree_hash")
            except Exception:
                pass
    return None


def _qualification_run_id() -> str:
    """rc7 Phase 6: Generate a deterministic qualification run ID.

    Format: qual-<date>-<commit-prefix>
    This prevents accidental mixing of old crash matrix with new test results
    even if they happen to have the same version string.
    """
    commit = _git_commit()[:12]
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"qual-{date}-{commit}"


def _schema_fingerprint() -> str:
    """rc8: Schema fingerprint for identity consistency."""
    try:
        import psycopg
        import os
        dsn = os.getenv("DATABASE_URL", "postgresql://construction:construction@localhost:5432/construction_ai")
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
        return "offline"


def _base_artifact() -> dict:
    """rc7 Phase 6/7 / rc8: Common identity header for all gate artifacts.

    Every artifact must contain the same identity fields. If any field differs
    between artifacts, qualification fails (Phase 33).

    rc8: If QUALIFICATION_IDENTITY.json exists, read identity from it
    instead of independently computing each field. This ensures all gates
    share one immutable identity object.
    """
    # rc8: Try to read from QUALIFICATION_IDENTITY.json first.
    identity_path = ROOT / "QUALIFICATION_IDENTITY.json"
    if identity_path.exists():
        try:
            identity = json.loads(identity_path.read_text())
            return {
                "release_version": identity.get("release_version", _version()),
                "version": identity.get("release_version", _version()),
                "git_commit": identity.get("git_commit", _git_commit()),
                "git_branch": identity.get("git_branch", _git_branch()),
                "payload_tree_hash": identity.get("payload_tree_hash", _payload_tree_hash()),
                "dependency_lock_hash": identity.get("dependency_lock_hash", _lock_hash()),
                "schema_fingerprint": identity.get("schema_fingerprint", _schema_fingerprint()),
                "migration_fingerprint": identity.get("migration_fingerprint", _migration_fingerprint()),
                "qualification_run_id": identity.get("qualification_run_id", _qualification_run_id()),
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        except Exception:
            pass

    # Fallback: compute identity fields independently.
    return {
        "release_version": _version(),
        "version": _version(),
        "git_commit": _git_commit(),
        "git_branch": _git_branch(),
        "payload_tree_hash": _payload_tree_hash(),
        "dependency_lock_hash": _lock_hash(),
        "schema_fingerprint": _schema_fingerprint(),
        "migration_fingerprint": _migration_fingerprint(),
        "qualification_run_id": _qualification_run_id(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _run_tests(test_files: list[str], timeout: int = 300) -> dict:
    """Run pytest on a subset and return structured results."""
    env = dict(os.environ)
    env["REQUIRE_INTEGRATION"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=no"] + test_files,
        capture_output=True, text=True, cwd=ROOT, timeout=timeout, env=env,
    )
    lines = [l for l in result.stdout.strip().splitlines() if l]
    summary = lines[-1] if lines else "no output"
    return {
        "exit_code": result.returncode,
        "passed": result.returncode == 0,
        "summary": summary,
    }


def _migration_checksums() -> list[dict]:
    """List all migrations with their SHA-256 checksums."""
    migrations_dir = ROOT / "migrations"
    migrations = sorted(migrations_dir.glob("*.sql"))
    result = []
    for f in migrations:
        data = f.read_bytes()
        result.append({
            "filename": f.name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        })
    return result


def _migration_fingerprint() -> str:
    """SHA-256 over all migration file contents concatenated."""
    hasher = hashlib.sha256()
    for m in sorted((ROOT / "migrations").glob("*.sql")):
        hasher.update(m.read_bytes())
    return hasher.hexdigest()


def generate_test_results() -> dict:
    """Generate TEST_RESULTS.json — full test suite results."""
    artifact = _base_artifact()
    env = dict(os.environ)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=no"],
        capture_output=True, text=True, cwd=ROOT, timeout=300, env=env,
    )
    import re
    lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
    summary_line = lines[-1] if lines else ""
    counts = {"passed": 0, "skipped": 0, "failed": 0, "error": 0}
    for key in counts:
        match = re.search(rf"(\d+)\s+{key}", summary_line)
        if match:
            counts[key] = int(match.group(1))

    artifact.update({
        "exit_code": result.returncode,
        "passed": counts["passed"],
        "skipped": counts["skipped"],
        "failed": counts["failed"],
        "error": counts["error"],
        "total": sum(counts.values()),
        "summary": summary_line,
    })
    return artifact


def generate_crash_matrix() -> dict:
    """Generate CRASH_MATRIX.json — crash/recovery test results."""
    artifact = _base_artifact()
    result = _run_tests([
        "tests/test_crash_injection.py",
        "tests/test_crash_injection_erp.py",
        "tests/test_rc4_crash_matrix.py",
        "tests/test_rc4_leases_and_recovery.py",
        "tests/test_rc5_hardening.py",
        "tests/test_rc6_hardening.py",
    ])
    artifact.update(result)
    artifact["description"] = "Crash injection and recovery tests using production mechanisms"
    return artifact


def generate_security_gate() -> dict:
    """Generate SECURITY_GATE.json — security gate results."""
    artifact = _base_artifact()
    # Run the security gate script itself.
    code, output = 0, ""
    try:
        result = subprocess.run(
            [sys.executable, "scripts/security_gate.py"],
            capture_output=True, text=True, cwd=ROOT, timeout=120,
            env={**os.environ, "DATABASE_URL": os.getenv("DATABASE_URL", "postgresql://construction:construction@localhost:5432/construction_ai")},
        )
        code = result.returncode
        output = result.stdout + result.stderr
    except Exception as e:
        code = 1
        output = str(e)

    artifact.update({
        "exit_code": code,
        "passed": code == 0,
        "summary": "security gate passed" if code == 0 else "security gate failed",
        "output": output.strip(),
    })
    return artifact


def generate_migration_gate() -> dict:
    """Generate MIGRATION_GATE.json — migration/upgrade gate results."""
    artifact = _base_artifact()
    # Run the upgrade gate script.
    code, output = 0, ""
    try:
        result = subprocess.run(
            [sys.executable, "scripts/upgrade_gate.py"],
            capture_output=True, text=True, cwd=ROOT, timeout=120,
            env={**os.environ, "DATABASE_URL": os.getenv("DATABASE_URL", "postgresql://construction:construction@localhost:5432/construction_ai")},
        )
        code = result.returncode
        output = result.stdout + result.stderr
    except Exception as e:
        code = 1
        output = str(e)

    migrations = _migration_checksums()
    artifact.update({
        "exit_code": code,
        "passed": code == 0,
        "summary": "upgrade gate passed" if code == 0 else "upgrade gate failed",
        "migration_count": len(migrations),
        "migration_fingerprint": _migration_fingerprint(),
        "migrations": migrations,
        "output": output.strip(),
    })
    return artifact


def main() -> int:
    print("Generating all gate artifacts from current build...")
    print(f"  version: {_version()}")
    print(f"  git_commit: {_git_commit()[:12]}")
    print()

    # rc8: Generate QUALIFICATION_IDENTITY.json first.
    # All gate artifacts read from this file to ensure one immutable identity.
    print("[0/5] Generating QUALIFICATION_IDENTITY.json...")
    from qualification_identity import generate_qualification_identity, write_qualification_identity
    tree_hash = _payload_tree_hash() or ""
    schema_fp = _schema_fingerprint()
    mig_fp = _migration_fingerprint()
    identity = generate_qualification_identity(
        payload_tree_hash=tree_hash,
        schema_fingerprint=schema_fp,
        migration_fingerprint=mig_fp,
    )
    write_qualification_identity(identity)
    print(f"  qualification_run_id: {identity['qualification_run_id']}")
    print(f"  dependency_lock_hash: {identity['dependency_lock_hash'][:16]}...")
    print(f"  schema_fingerprint: {identity['schema_fingerprint'][:16]}...")

    # 1. TEST_RESULTS.json
    print("[1/5] Generating TEST_RESULTS.json (full test suite)...")
    test_results = generate_test_results()
    (ROOT / "TEST_RESULTS.json").write_text(
        json.dumps(test_results, indent=2, sort_keys=True, default=str)
    )
    print(f"  {test_results['summary']}")

    # 2. CRASH_MATRIX.json
    print("[2/5] Generating CRASH_MATRIX.json (crash/recovery)...")
    crash_matrix = generate_crash_matrix()
    (ROOT / "CRASH_MATRIX.json").write_text(
        json.dumps(crash_matrix, indent=2, sort_keys=True, default=str)
    )
    print(f"  {crash_matrix['summary']}")

    # 3. SECURITY_GATE.json
    print("[3/5] Generating SECURITY_GATE.json...")
    security_gate = generate_security_gate()
    (ROOT / "SECURITY_GATE.json").write_text(
        json.dumps(security_gate, indent=2, sort_keys=True, default=str)
    )
    print(f"  passed: {security_gate['passed']}")

    # 4. MIGRATION_GATE.json
    print("[4/5] Generating MIGRATION_GATE.json...")
    migration_gate = generate_migration_gate()
    (ROOT / "MIGRATION_GATE.json").write_text(
        json.dumps(migration_gate, indent=2, sort_keys=True, default=str)
    )
    print(f"  passed: {migration_gate['passed']}, migrations: {migration_gate['migration_count']}")

    print()
    print("All gate artifacts generated.")
    all_passed = (
        test_results["exit_code"] == 0
        and crash_matrix["passed"]
        and security_gate["passed"]
        and migration_gate["passed"]
    )
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
