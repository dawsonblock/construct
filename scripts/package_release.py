#!/usr/bin/env python
"""v0.5.0-rc7 Phase 9 — package the release ZIP and hash it externally.

The trust chain is:
    FinalZIP → ExternalZIPHash

Inside the ZIP:
    ReleaseAttestation → QualificationReport → PayloadManifest → PayloadFiles

No cycles: the ZIP hash is computed AFTER packaging and stored in a separate
file that is NOT included in the ZIP.

Usage:
    python scripts/package_release.py --version 0.5.0-rc7
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
import zipfile
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


def package_release(version: str) -> tuple[Path, str]:
    """Package the release ZIP and return (zip_path, sha256)."""
    zip_name = f"construct-{version}.zip"
    zip_path = ROOT / zip_name

    # Files to include in the ZIP.
    # Get tracked files from git.
    try:
        res = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True, cwd=ROOT)
        tracked = [l.strip() for l in res.stdout.splitlines() if l.strip()]
    except Exception:
        tracked = []

    # Also include generated artifacts.
    artifacts = [
        "PAYLOAD_MANIFEST.json",
        "PAYLOAD_MANIFEST.sha256",
        "QUALIFICATION_REPORT.json",
        "TEST_RESULTS.json",
        "CRASH_MATRIX.json",
        "SECURITY_GATE.json",
        "MIGRATION_GATE.json",
        "RELEASE_ATTESTATION.json",
        "RELEASE_ATTESTATION.json.sha256",
    ]

    all_files = set(tracked)
    for a in artifacts:
        if (ROOT / a).exists():
            all_files.add(a)

    # Exclude the ZIP itself and any old ZIPs.
    all_files = {f for f in all_files if not f.endswith(".zip")}

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(all_files):
            p = ROOT / f
            if p.exists() and p.is_file():
                zf.write(p, f)

    # Compute the external hash (NOT included in the ZIP).
    sha256 = hashlib.sha256(zip_path.read_bytes()).hexdigest()

    # Write the external hash file.
    hash_file = ROOT / f"{zip_name}.sha256"
    hash_file.write_text(f"{sha256}  {zip_name}\n")

    return zip_path, sha256


def main() -> int:
    version = None
    if "--version" in sys.argv:
        version = sys.argv[sys.argv.index("--version") + 1]
    else:
        vfile = ROOT / "VERSION"
        if vfile.exists():
            version = vfile.read_text().strip()
        else:
            print("ERROR: no version specified", file=sys.stderr)
            return 1

    print(f"Packaging release: construct-{version}.zip")
    zip_path, sha256 = package_release(version)
    print(f"  ZIP: {zip_path}")
    print(f"  Size: {zip_path.stat().st_size} bytes")
    print(f"  SHA-256: {sha256}")
    print(f"  Hash file: {zip_path}.sha256")
    print()
    print("Trust chain (acyclic):")
    print(f"  construct-{version}.zip → construct-{version}.zip.sha256")
    print("  Inside ZIP: RELEASE_ATTESTATION → QUALIFICATION_REPORT → PAYLOAD_MANIFEST → PayloadFiles")
    return 0


if __name__ == "__main__":
    sys.exit(main())
