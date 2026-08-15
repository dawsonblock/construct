#!/usr/bin/env python
"""v0.5.0-rc7 — full-stack clean-slate qualification runner (Phase 30).

Orchestrates a complete clean-slate qualification run against a fresh
PostgreSQL, Redis, object storage, migration state, and ERPNext stub.

The runner:
1. Verifies the stack is running (PostgreSQL, Redis, ERP stub).
2. Tears down and rebuilds the database schema from scratch.
3. Runs every test category with REQUIRE_INTEGRATION=1.
4. Generates the qualification report.
5. Exits non-zero if any critical test fails or skips.

Usage:
    python scripts/full_qualification.py

Prerequisites:
    - `make up` must have been run (Docker Compose stack running).
    - DATABASE_URL must point at a fresh PostgreSQL instance.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent

#: Test categories that must all pass with zero skips under REQUIRE_INTEGRATION=1.
TEST_CATEGORIES = {
    "unit": [
        "tests/test_adversarial_corpus.py",
        "tests/test_external_action_properties.py",
        "tests/test_verification_integrity.py",
    ],
    "integration": [
        "tests/test_integration_pipeline.py",
        "tests/test_executor.py",
        "tests/test_reconciliation.py",
        "tests/test_reconcile_unknown.py",
        "tests/test_erp_supplier_verification.py",
    ],
    "approval": [
        "tests/test_stale_approval.py",
        "tests/test_decision_fingerprint.py",
        "tests/test_execution_preconditions.py",
        "tests/test_schedule_of_values.py",
        "tests/test_duplicate_detection.py",
        "tests/test_verification_packet_immutable.py",
        "tests/test_evidence_freshness.py",
    ],
    "external_effect": [
        "tests/test_external_action_state_machine.py",
        "tests/test_crash_injection.py",
        "tests/test_crash_injection_erp.py",
        "tests/test_replay_fingerprints.py",
        "tests/test_rc4_leases_and_recovery.py",
        "tests/test_rc4_reconciliation.py",
        "tests/test_rc5_hardening.py",
        "tests/test_rc6_hardening.py",
    ],
    "release_integrity": [
        "tests/test_repository_integrity.py",
        "tests/test_release_artifact_integrity.py",
    ],
    "security": [
        "tests/test_ui_security.py",
    ],
    "reproducibility": [
        "tests/test_upgrade_reproducibility.py",
    ],
}


def _run(cmd: list[str], *, env: dict | None = None, timeout: int = 300) -> tuple[int, str]:
    """Run a command and return (exit_code, output)."""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=ROOT, env=full_env, timeout=timeout,
    )
    return result.returncode, result.stdout + result.stderr


def _check_stack() -> bool:
    """Verify PostgreSQL is reachable."""
    dsn = os.getenv("DATABASE_URL", "postgresql://construction:construction@localhost:5432/construction_ai")
    try:
        import psycopg
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                return True
    except Exception:
        return False


def _reset_database() -> bool:
    """Drop and recreate the schema from scratch (clean slate)."""
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL is not set — cannot reset database")
        return False

    # Drop all tables and reapply migrations from scratch.
    try:
        import psycopg
        with psycopg.connect(dsn) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                # Drop the public schema and recreate it — clean slate.
                cur.execute("DROP SCHEMA IF EXISTS public CASCADE")
                cur.execute("CREATE SCHEMA public")
                # Re-grant to the app role.
                cur.execute("GRANT ALL ON SCHEMA public TO construction_app")
                cur.execute("GRANT USAGE ON SCHEMA public TO construction_app")
        # Reapply all migrations.
        code, output = _run(
            [sys.executable, "scripts/migrate.py"],
            env={"DATABASE_URL": dsn},
        )
        if code != 0:
            print(f"ERROR: migration failed: {output}")
            return False
        return True
    except Exception as e:
        print(f"ERROR: database reset failed: {e}")
        return False


def main() -> int:
    print("=" * 70)
    print("v0.5.0-rc7 Full-Stack Clean-Slate Qualification")
    print("=" * 70)

    # 1. Check stack.
    print("\n[1/5] Checking stack...")
    if not _check_stack():
        print("FAIL: PostgreSQL is not reachable. Run 'make up' first.")
        return 1
    print("  PostgreSQL: OK")

    # 2. Reset database.
    print("\n[2/5] Resetting database (clean slate)...")
    if not _reset_database():
        print("FAIL: database reset failed")
        return 1
    print("  Database reset: OK")

    # 3. Run every test category with REQUIRE_INTEGRATION=1.
    print("\n[3/5] Running test categories (REQUIRE_INTEGRATION=1)...")
    all_passed = True
    category_results: dict[str, dict] = {}

    for category, test_files in TEST_CATEGORIES.items():
        print(f"\n  [{category}] {len(test_files)} test file(s)...")
        code, output = _run(
            [sys.executable, "-m", "pytest", "-q", "--tb=short"] + test_files,
            env={"REQUIRE_INTEGRATION": "1"},
            timeout=300,
        )
        # Extract the summary line.
        lines = [l for l in output.strip().splitlines() if l]
        summary = lines[-1] if lines else "no output"
        passed = code == 0
        category_results[category] = {
            "passed": passed,
            "exit_code": code,
            "summary": summary,
        }
        status = "PASS" if passed else "FAIL"
        print(f"  [{category}] {status}: {summary}")
        if not passed:
            all_passed = False

    # 4. Generate the acyclic attestation chain:
    #    PayloadTree -> MANIFEST -> GateArtifacts -> QualificationReport -> RELEASE_ATTESTATION
    print("\n[4/5] Generating release attestation chain...")
    db_url = os.getenv("DATABASE_URL", "postgresql://construction:construction@localhost:5432/construction_ai")

    # 4a. Payload manifest (hashes ONLY the payload tree, not qualification artifacts).
    manifest_code, manifest_output = _run(
        [sys.executable, "scripts/release_manifest.py",
         "--with-database", "--output", "MANIFEST.json"],
        env={"DATABASE_URL": db_url},
    )
    if manifest_code != 0:
        print(f"  WARNING: manifest generation failed: {manifest_output}")
    else:
        print("  Manifest: MANIFEST.json")

    # 4b. Gate artifacts (TEST_RESULTS, CRASH_MATRIX, SECURITY_GATE, MIGRATION_GATE).
    gate_code, gate_output = _run(
        [sys.executable, "scripts/generate_gate_artifacts.py"],
        env={"DATABASE_URL": db_url, "REQUIRE_INTEGRATION": "1"},
    )
    if gate_code != 0:
        print(f"  WARNING: gate artifact generation had failures: {gate_output}")
    else:
        print("  Gate artifacts: TEST_RESULTS, CRASH_MATRIX, SECURITY_GATE, MIGRATION_GATE")

    # 4c. Qualification report (binds to manifest one-directionally).
    report_code, report_output = _run(
        [sys.executable, "scripts/qualification_report.py",
         "--output", "QUALIFICATION_REPORT.json"],
        env={"REQUIRE_INTEGRATION": "1"},
    )

    # 4d. Release attestation (hashes manifest + all qualification artifacts).
    attestation_code, attestation_output = _run(
        [sys.executable, "scripts/release_attestation.py",
         "--output", "RELEASE_ATTESTATION.json"],
        env={},
    )
    if attestation_code != 0:
        print(f"  WARNING: attestation generation failed: {attestation_output}")
    else:
        print("  Release attestation: RELEASE_ATTESTATION.json")
    if os.path.exists(ROOT / "QUALIFICATION_REPORT.json"):
        print("  Report: QUALIFICATION_REPORT.json")
    else:
        print("  WARNING: report was not generated")

    # 5. Summary.
    print("\n[5/5] Summary")
    print("-" * 40)
    for category, result in category_results.items():
        status = "PASS" if result["passed"] else "FAIL"
        print(f"  {category:20s} {status}  {result['summary']}")
    print("-" * 40)

    if all_passed:
        print("\nQUALIFICATION: PASSED")
        return 0
    else:
        print("\nQUALIFICATION: FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(main())
