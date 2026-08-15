#!/usr/bin/env python
"""v0.5.0-rc7 — generate the final detached release attestation.

The rc7 attestation architecture is acyclic:

  PayloadTree -> MANIFEST.json -> Qualification -> RELEASE_ATTESTATION.json

This script is the LAST step in the release-attestation chain. It hashes:
  - MANIFEST.json (the payload manifest)
  - MANIFEST.json.sha256 (the detached manifest companion)
  - QUALIFICATION_REPORT.json (the qualification report)
  - TEST_RESULTS.json (raw test results)
  - CRASH_MATRIX.json (crash/recovery evidence)
  - SECURITY_GATE.json (security gate evidence)
  - MIGRATION_GATE.json (migration gate evidence)

And produces RELEASE_ATTESTATION.json containing all those hashes plus
metadata (version, git commit, generated_at). This file is the single
root of the release trust chain — verify it, and you can verify everything
else by following the hashes.

Usage:
    python scripts/release_attestation.py --output RELEASE_ATTESTATION.json
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent

#: The canonical list of evidence files that the attestation hashes.
#: These are the post-manifest artifacts that form the qualification bundle.
EVIDENCE_FILES = [
    "PAYLOAD_MANIFEST.json",
    "PAYLOAD_MANIFEST.sha256",
    "QUALIFICATION_REPORT.json",
    "TEST_RESULTS.json",
    "CRASH_MATRIX.json",
    "SECURITY_GATE.json",
    "MIGRATION_GATE.json",
]


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


def _hash_file(name: str) -> dict:
    """Hash a single evidence file. Returns {size, sha256} or {error}."""
    path = ROOT / name
    if not path.exists():
        return {"error": "file not found", "size": 0, "sha256": None}
    data = path.read_bytes()
    return {
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def generate_attestation() -> dict:
    """Generate the final release attestation.

    This is the root of the acyclic trust chain:
      PayloadTree -> MANIFEST.json -> Qualification -> RELEASE_ATTESTATION.json

    The attestation hashes all evidence files. To verify a release:
    1. Verify RELEASE_ATTESTATION.json against its detached companion hash.
    2. Verify each evidence file hash matches the attestation.
    3. Verify MANIFEST.json tree_hash matches the actual payload tree.
    4. Verify QUALIFICATION_REPORT.json manifest_sha matches MANIFEST.json.
    5. Verify all gates in QUALIFICATION_REPORT.json are PASS.
    """
    evidence: dict[str, dict] = {}
    for name in EVIDENCE_FILES:
        evidence[name] = _hash_file(name)

    # Extract manifest tree_hash and qualification manifest_sha for convenience.
    manifest_tree_hash = None
    qualification_manifest_sha = None
    qualification_run_id = None
    payload_tree_hash = None
    dependency_lock_hash = None
    schema_fingerprint = None
    migration_fingerprint = None
    try:
        for name in ("PAYLOAD_MANIFEST.json", "MANIFEST.json"):
            p = ROOT / name
            if p.exists():
                manifest = json.loads(p.read_text())
                manifest_tree_hash = manifest.get("tree_hash")
                payload_tree_hash = manifest.get("tree_hash")
                schema_fingerprint = manifest.get("schema_fingerprint")
                migration_fingerprint = manifest.get("migration_fingerprint")
                break
    except Exception:
        pass
    try:
        report = json.loads((ROOT / "QUALIFICATION_REPORT.json").read_text())
        qualification_manifest_sha = report.get("manifest_sha")
        qualification_run_id = report.get("qualification_run_id")
        dependency_lock_hash = report.get("dependency_lock_hash")
        if not schema_fingerprint:
            schema_fingerprint = report.get("schema_fingerprint")
        if not migration_fingerprint:
            migration_fingerprint = report.get("migration_fingerprint")
    except Exception:
        pass

    # rc8: If dependency_lock_hash is still None, compute it from the shared helper.
    if not dependency_lock_hash:
        try:
            from qualification_identity import compute_dependency_lock_hash
            dependency_lock_hash = compute_dependency_lock_hash()
        except Exception:
            dependency_lock_hash = "unknown"

    # Compute the attestation root hash over all evidence hashes.
    # This is the single value that binds the entire release bundle.
    hasher = hashlib.sha256()
    for name in EVIDENCE_FILES:
        entry = evidence[name]
        sha = entry.get("sha256")
        if sha:
            hasher.update(f"{name}:{sha}\n".encode())
    attestation_root = hasher.hexdigest()

    return {
        "release_version": _version(),
        "version": _version(),
        "git_commit": _git_commit(),
        "git_branch": _git_branch(),
        "payload_tree_hash": payload_tree_hash,
        "dependency_lock_hash": dependency_lock_hash,
        "schema_fingerprint": schema_fingerprint,
        "migration_fingerprint": migration_fingerprint,
        "qualification_run_id": qualification_run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "attestation_chain": "PayloadTree -> PAYLOAD_MANIFEST.json -> Qualification -> RELEASE_ATTESTATION.json",
        "attestation_root_hash": attestation_root,
        "manifest_tree_hash": manifest_tree_hash,
        "qualification_manifest_sha": qualification_manifest_sha,
        "evidence": evidence,
    }


def main() -> int:
    output = None
    if "--output" in sys.argv:
        output = sys.argv[sys.argv.index("--output") + 1]

    attestation = generate_attestation()

    # Check that all evidence files exist.
    missing = [
        name for name in EVIDENCE_FILES
        if attestation["evidence"][name].get("error")
    ]
    if missing:
        print(f"WARNING: missing evidence files: {missing}", file=sys.stderr)

    attestation_json = json.dumps(attestation, indent=2, sort_keys=True, default=str)

    if output:
        Path(output).write_text(attestation_json)
        # Write detached companion hash.
        companion = Path(output + ".sha256")
        final_bytes = Path(output).read_bytes()
        companion_hash = hashlib.sha256(final_bytes).hexdigest()
        companion.write_text(f"{companion_hash}  {Path(output).name}\n")
        print(f"release attestation written to {output}")
        print(f"attestation root hash: {attestation['attestation_root_hash']}")
        print(f"attestation companion written to {companion}: {companion_hash}")
    else:
        print(attestation_json)

    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
