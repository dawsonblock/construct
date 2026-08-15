#!/usr/bin/env python
"""rc9 Phase 19 — Generate the final release receipt.

Creates dist/RELEASE_RECEIPT.json containing:
    release_version
    git_commit
    qualification_run_id
    payload_tree_hash
    release_attestation_sha256
    final_zip_filename
    final_zip_sha256
    created_at

Then optionally writes RELEASE_RECEIPT.sha256.

This is the final human-readable handoff record.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
DIST = ROOT / "dist"


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
            cwd=ROOT,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def generate_receipt() -> dict:
    """Generate the release receipt from current artifacts."""
    version = (ROOT / "VERSION").read_text().strip()
    zip_name = f"construct-{version}.zip"
    zip_path = DIST / zip_name
    hash_path = DIST / f"{zip_name}.sha256"

    if not zip_path.exists():
        raise FileNotFoundError(f"final ZIP not found: {zip_path}")

    # Read ZIP hash.
    zip_sha256 = hash_path.read_text().strip().split()[0] if hash_path.exists() else None
    if not zip_sha256:
        zip_sha256 = hashlib.sha256(zip_path.read_bytes()).hexdigest()

    # Read attestation.
    attestation_path = ROOT / "RELEASE_ATTESTATION.json"
    attestation_sha = None
    qualification_run_id = None
    payload_tree_hash = None
    if attestation_path.exists():
        attestation_sha = hashlib.sha256(attestation_path.read_bytes()).hexdigest()
        att = json.loads(attestation_path.read_text())
        qualification_run_id = att.get("qualification_run_id")
        payload_tree_hash = att.get("payload_tree_hash")

    receipt = {
        "release_version": version,
        "git_commit": _git_commit(),
        "qualification_run_id": qualification_run_id,
        "payload_tree_hash": payload_tree_hash,
        "release_attestation_sha256": attestation_sha,
        "final_zip_filename": zip_name,
        "final_zip_sha256": zip_sha256,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return receipt


def main() -> int:
    DIST.mkdir(exist_ok=True)
    receipt = generate_receipt()
    receipt_path = DIST / "RELEASE_RECEIPT.json"
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True, default=str))

    # Write detached hash.
    receipt_sha = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    hash_path = DIST / "RELEASE_RECEIPT.sha256"
    hash_path.write_text(f"{receipt_sha}  RELEASE_RECEIPT.json\n")

    print(f"Release receipt written to {receipt_path}")
    print(f"  release_version: {receipt['release_version']}")
    print(f"  git_commit: {receipt['git_commit'][:12]}...")
    print(f"  qualification_run_id: {receipt['qualification_run_id']}")
    print(f"  final_zip_sha256: {receipt['final_zip_sha256'][:16]}...")
    print(f"  receipt_sha256: {receipt_sha[:16]}...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
