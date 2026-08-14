#!/usr/bin/env python
"""v0.5.0-rc3 — qualification report generator (Phase 28).

Generates a qualification report that captures the full state of the system
after running the test suite. The report is the qualification artifact: a
release is qualified only if this report shows all gates green.

The report includes:
- Version
- Git SHA and branch
- Release archive SHA (if an archive was built)
- Manifest SHA (if a manifest was generated)
- Schema version (migration count + schema fingerprint)
- Dependency lock hash
- Test counts (passed, skipped, failed)
- Adversarial result
- Crash-test result
- External-effect qualification result

Usage:
    python scripts/qualification_report.py [--pytest] [--output report.json]

When --pytest is passed, the script runs pytest and captures counts.
Otherwise, it expects the counts to be provided via environment variables or
generates a report with test counts marked as "not_run".
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
    """SHA-256 over both lock files."""
    hasher = hashlib.sha256()
    for name in ("requirements.lock.txt", "requirements-dev.lock.txt"):
        path = ROOT / name
        if path.exists():
            hasher.update(path.read_bytes())
    return hasher.hexdigest()


def _manifest_sha() -> str | None:
    """SHA-256 of the release manifest if it exists."""
    for name in ("MANIFEST.rc3.json", "MANIFEST.json"):
        path = ROOT / name
        if path.exists():
            return hashlib.sha256(path.read_bytes()).hexdigest()
    return None


def _schema_version() -> dict:
    """Migration count and schema fingerprint."""
    migrations_dir = ROOT / "migrations"
    migrations = sorted(migrations_dir.glob("*.sql"))
    hasher = hashlib.sha256()
    for f in migrations:
        hasher.update(f.read_bytes())
    return {
        "migration_count": len(migrations),
        "migration_fingerprint": hasher.hexdigest(),
    }


def _parse_pytest_summary(stdout: str) -> dict:
    """Parse the last line of pytest -q output for counts.

    Example: "486 passed, 5 skipped in 17.30s"
    """
    import re

    lines = [l.strip() for l in stdout.strip().splitlines() if l.strip()]
    summary_line = lines[-1] if lines else ""
    counts = {"passed": 0, "skipped": 0, "failed": 0, "error": 0}
    for key in counts:
        match = re.search(rf"(\d+)\s+{key}", summary_line)
        if match:
            counts[key] = int(match.group(1))
    counts["summary"] = summary_line
    return counts


def _run_pytest() -> dict:
    """Run pytest and capture counts by parsing stdout."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=no"],
        capture_output=True, text=True, cwd=ROOT, timeout=300,
    )
    counts = _parse_pytest_summary(result.stdout)
    return {
        "passed": counts["passed"],
        "skipped": counts["skipped"],
        "failed": counts["failed"],
        "error": counts["error"],
        "total": counts["passed"] + counts["skipped"] + counts["failed"] + counts["error"],
        "exit_code": result.returncode,
        "summary": counts["summary"],
        "run": True,
    }


def _run_pytest_subset(test_paths: list[str]) -> dict:
    """Run pytest on a subset of tests and return pass/fail counts."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=no"] + test_paths,
        capture_output=True, text=True, cwd=ROOT, timeout=120,
    )
    # Parse the last line for counts.
    lines = [l for l in result.stdout.strip().splitlines() if l]
    return {
        "exit_code": result.returncode,
        "summary": lines[-1] if lines else "no output",
        "passed": result.returncode == 0,
    }


def generate_report(*, run_tests: bool = False) -> dict:
    """Generate the qualification report.

    When run_tests=True, runs pytest and captures counts. Otherwise, marks
    test counts as "not_run" — the caller is expected to run tests separately
    and fill in the results.
    """
    schema = _schema_version()

    report: dict = {
        "version": _version(),
        "git_commit": _git_commit(),
        "git_branch": _git_branch(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schema": schema,
        "dependency_lock_hash": _lock_hash(),
        "manifest_sha": _manifest_sha(),
    }

    if run_tests:
        # Full suite.
        report["test_suite"] = _run_pytest()

        # Adversarial subset.
        report["adversarial"] = _run_pytest_subset([
            "tests/test_adversarial_corpus.py",
            "tests/test_external_action_properties.py",
        ])

        # Crash/recovery subset.
        report["crash_recovery"] = _run_pytest_subset([
            "tests/test_crash_injection.py",
            "tests/test_crash_injection_erp.py",
        ])

        # External-effect subset.
        report["external_effect"] = _run_pytest_subset([
            "tests/test_executor.py",
            "tests/test_reconciliation.py",
            "tests/test_reconcile_unknown.py",
            "tests/test_stale_approval.py",
            "tests/test_decision_fingerprint.py",
            "tests/test_execution_preconditions.py",
            "tests/test_external_action_state_machine.py",
        ])

        # Release integrity subset.
        report["release_integrity"] = _run_pytest_subset([
            "tests/test_repository_integrity.py",
            "tests/test_release_artifact_integrity.py",
        ])

        # Overall qualification: all subsets must pass.
        all_passed = (
            report["test_suite"].get("exit_code", 1) == 0
            and report["adversarial"].get("passed", False)
            and report["crash_recovery"].get("passed", False)
            and report["external_effect"].get("passed", False)
            and report["release_integrity"].get("passed", False)
        )
        report["qualified"] = all_passed
    else:
        report["test_suite"] = {"run": False}
        report["adversarial"] = {"run": False}
        report["crash_recovery"] = {"run": False}
        report["external_effect"] = {"run": False}
        report["release_integrity"] = {"run": False}
        report["qualified"] = False

    return report


def main() -> int:
    run_tests = "--pytest" in sys.argv
    output = None
    if "--output" in sys.argv:
        output = sys.argv[sys.argv.index("--output") + 1]

    report = generate_report(run_tests=run_tests)
    report_json = json.dumps(report, indent=2, sort_keys=True, default=str)

    if output:
        Path(output).write_text(report_json)
        print(f"qualification report written to {output}")
    else:
        print(report_json)

    return 0 if report.get("qualified") else 1


if __name__ == "__main__":
    sys.exit(main())
