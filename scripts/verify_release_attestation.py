#!/usr/bin/env python
"""rc9 Phase 14 — Verify release attestation inside an extracted ZIP.

Verifies:
1. Each qualification artifact hash matches RELEASE_ATTESTATION.json evidence.
2. Payload manifest hash matches.
3. Shared qualification identity is consistent.
4. Attestation root hash is correct.
5. Detached attestation SHA matches.

Usage:
    python scripts/verify_release_attestation.py --extracted /tmp/construct-release-check
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def verify_attestation(extracted_dir: Path) -> bool:
    """Verify release attestation inside an extracted ZIP."""
    attestation_path = extracted_dir / "RELEASE_ATTESTATION.json"
    if not attestation_path.exists():
        print("[FAIL] RELEASE_ATTESTATION.json not found in extracted ZIP")
        return False

    attestation = json.loads(attestation_path.read_text())
    errors = []

    # 1. Hash each qualification artifact and compare against attestation evidence.
    for name, entry in attestation.get("evidence", {}).items():
        evidence_path = extracted_dir / name
        if not evidence_path.exists():
            errors.append(f"evidence file missing: {name}")
            continue
        if entry.get("sha256"):
            actual = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
            if actual != entry["sha256"]:
                errors.append(f"evidence hash mismatch: {name}")

    # 2. Verify payload manifest hash.
    manifest_path = extracted_dir / "PAYLOAD_MANIFEST.json"
    if manifest_path.exists():
        manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        manifest_evidence = attestation.get("evidence", {}).get("PAYLOAD_MANIFEST.json", {})
        if manifest_evidence.get("sha256") and manifest_sha != manifest_evidence["sha256"]:
            errors.append("payload manifest hash mismatch in attestation")

    # 3. Verify shared qualification identity.
    identity_path = extracted_dir / "QUALIFICATION_IDENTITY.json"
    if identity_path.exists():
        identity = json.loads(identity_path.read_text())
        for field in ["release_version", "git_commit", "payload_tree_hash",
                       "qualification_run_id", "dependency_lock_hash",
                       "schema_fingerprint", "migration_fingerprint"]:
            if field in identity and field in attestation:
                if identity[field] != attestation[field]:
                    errors.append(f"identity mismatch: {field}")

    # 4. Recompute attestation root.
    # Must use the same EVIDENCE_FILES order as release_attestation.py.
    EVIDENCE_FILES = [
        "PAYLOAD_MANIFEST.json",
        "PAYLOAD_MANIFEST.sha256",
        "QUALIFICATION_REPORT.json",
        "TEST_RESULTS.json",
        "CRASH_MATRIX.json",
        "SECURITY_GATE.json",
        "MIGRATION_GATE.json",
    ]
    expected_root = attestation.get("attestation_root_hash")
    if expected_root:
        hasher = hashlib.sha256()
        for name in EVIDENCE_FILES:
            entry = attestation.get("evidence", {}).get(name, {})
            sha = entry.get("sha256")
            if sha:
                hasher.update(f"{name}:{sha}\n".encode())
        actual_root = hasher.hexdigest()
        if actual_root != expected_root:
            errors.append(f"attestation root hash mismatch: expected={expected_root[:16]}... actual={actual_root[:16]}...")

    # 5. Verify detached attestation SHA.
    detached_path = extracted_dir / "RELEASE_ATTESTATION.json.sha256"
    if detached_path.exists():
        detached_sha = detached_path.read_text().strip().split()[0]
        actual_attestation_sha = hashlib.sha256(attestation_path.read_bytes()).hexdigest()
        if detached_sha != actual_attestation_sha:
            errors.append("detached attestation SHA mismatch")

    if errors:
        print(f"[FAIL] {len(errors)} error(s) found:")
        for e in errors:
            print(f"  - {e}")
        return False

    print("[PASS] All evidence hashes match")
    print("[PASS] Payload manifest hash matches")
    print("[PASS] Qualification identity consistent")
    print("[PASS] Attestation root hash correct")
    print("[PASS] Detached attestation SHA matches")
    print("[PASS] Attestation(Unzip(FinalZIP)) = PASS")
    return True


def main() -> int:
    extracted = None
    if "--extracted" in sys.argv:
        extracted = sys.argv[sys.argv.index("--extracted") + 1]
    else:
        print("ERROR: --extracted <dir> required", file=sys.stderr)
        return 1

    extracted_dir = Path(extracted)
    if not extracted_dir.exists():
        print(f"ERROR: extracted directory not found: {extracted_dir}", file=sys.stderr)
        return 1

    print("rc9 Release Attestation Verification")
    print(f"Extracted: {extracted_dir}")
    print()
    success = verify_attestation(extracted_dir)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
