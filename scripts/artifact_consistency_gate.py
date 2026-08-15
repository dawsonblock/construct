#!/usr/bin/env python
"""v0.5.0-rc7 Phase 33 — exhaustive release gate artifact consistency check.

Opens every artifact and asserts:
    Version_i = Version_release
    Commit_i = Commit_release
    PayloadTreeHash_i = PayloadTreeHash_release
    QualificationRunID_i = QualificationRunID_release

for all:
    TEST_RESULTS
    SECURITY_GATE
    CRASH_MATRIX
    MIGRATION_GATE
    QUALIFICATION_REPORT
    RELEASE_ATTESTATION

No stale rc5/rc6 files can survive this.

Usage:
    python scripts/artifact_consistency_gate.py
    python scripts/artifact_consistency_gate.py --output ARTIFACT_CONSISTENCY_GATE.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent

GATE_FILES = [
    "TEST_RESULTS.json",
    "CRASH_MATRIX.json",
    "SECURITY_GATE.json",
    "MIGRATION_GATE.json",
    "QUALIFICATION_REPORT.json",
    "RELEASE_ATTESTATION.json",
]

IDENTITY_FIELDS = [
    "release_version",
    "git_commit",
    "payload_tree_hash",
    "qualification_run_id",
]


def check_consistency() -> tuple[bool, list[str], dict]:
    """Check that all artifacts share the same identity fields."""
    errors: list[str] = []
    identities: dict[str, dict] = {}

    for name in GATE_FILES:
        path = ROOT / name
        if not path.exists():
            errors.append(f"missing artifact: {name}")
            continue
        try:
            artifact = json.loads(path.read_text())
        except Exception as e:
            errors.append(f"invalid JSON in {name}: {e}")
            continue

        ident = {}
        for field in IDENTITY_FIELDS:
            val = artifact.get(field)
            if val is None:
                # Try alternate field names.
                if field == "release_version":
                    val = artifact.get("version")
                elif field == "payload_tree_hash":
                    val = artifact.get("manifest_tree_hash")
            ident[field] = val
            if val is None:
                errors.append(f"{name} missing identity field: {field}")
        identities[name] = ident

    if errors:
        return False, errors, {"identities": identities}

    # Check all identities match the release attestation.
    ref = identities.get("RELEASE_ATTESTATION.json", {})
    for name, ident in identities.items():
        for field in IDENTITY_FIELDS:
            if ident.get(field) != ref.get(field):
                errors.append(
                    f"{name}.{field} = {ident.get(field)!r} != "
                    f"RELEASE_ATTESTATION.{field} = {ref.get(field)!r}"
                )

    return len(errors) == 0, errors, {
        "passed": len(errors) == 0,
        "reference": "RELEASE_ATTESTATION.json",
        "identities": identities,
        "errors": errors,
    }


def main() -> int:
    success, errors, result = check_consistency()

    if "--output" in sys.argv:
        output = sys.argv[sys.argv.index("--output") + 1]
        Path(output).write_text(json.dumps(result, indent=2, default=str))

    if success:
        print("[PASS] All artifacts share consistent identity fields")
        for name, ident in result["identities"].items():
            print(f"  {name}: version={ident.get('release_version')} "
                  f"commit={str(ident.get('git_commit'))[:12]}... "
                  f"run_id={ident.get('qualification_run_id')}")
        return 0
    else:
        print(f"[FAIL] {len(errors)} consistency error(s):")
        for e in errors:
            print(f"  - {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
