#!/usr/bin/env python
"""v0.5.0-rc9 Phase 9 — package the release ZIP and hash it externally.

The trust chain is:
    FinalZIP → ExternalZIPHash

Inside the ZIP:
    ReleaseAttestation → QualificationReport → PayloadManifest → PayloadFiles

No cycles: the ZIP hash is computed AFTER packaging and stored in a separate
file that is NOT included in the ZIP.

rc9: The ZIP and its hash are written to dist/ — not the source root.
This keeps SourcePayload separate from ReleaseOutputs.

Usage:
    python scripts/package_release.py --version 0.5.0-rc9.dev0
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
import zipfile
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


def package_release(version: str) -> tuple[Path, str]:
    """Package the release ZIP and return (zip_path, sha256).

    rc9: Writes to dist/ directory. Reads the payload file list from
    PAYLOAD_MANIFEST.json. The ZIP contains exactly:
      PayloadFiles (from manifest) ∪ AttestationFiles (generated artifacts)

    No build outputs, no ZIPs, no dist/ paths appear in the payload.
    """
    import json

    zip_name = f"construct-{version}.zip"

    # rc9: Write to dist/ directory, not source root.
    DIST.mkdir(exist_ok=True)
    zip_path = DIST / zip_name

    # rc9: Read the payload file list from the manifest.
    manifest_path = ROOT / "PAYLOAD_MANIFEST.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        payload_files = set(manifest.get("files", {}).keys())
    else:
        # Fallback: use git ls-files if no manifest exists.
        try:
            res = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True, cwd=ROOT)
            payload_files = set(l.strip() for l in res.stdout.splitlines() if l.strip())
        except Exception:
            payload_files = set()

    # Also include generated attestation artifacts (not in payload manifest).
    # rc9: Include QUALIFICATION_IDENTITY.json as an attestation file.
    artifacts = [
        "PAYLOAD_MANIFEST.json",
        "PAYLOAD_MANIFEST.sha256",
        "QUALIFICATION_IDENTITY.json",
        "QUALIFICATION_REPORT.json",
        "TEST_RESULTS.json",
        "CRASH_MATRIX.json",
        "SECURITY_GATE.json",
        "MIGRATION_GATE.json",
        "RELEASE_ATTESTATION.json",
        "RELEASE_ATTESTATION.json.sha256",
    ]

    all_files = set(payload_files)
    for a in artifacts:
        if (ROOT / a).exists():
            all_files.add(a)

    # Exclude any ZIP files, ZIP hash files, and dist/ paths (hard exclusion).
    all_files = {
        f for f in all_files
        if not f.endswith(".zip")
        and not f.endswith(".zip.sha256")
        and not f.startswith("dist/")
        and not f.startswith("build/")
    }

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(all_files):
            p = ROOT / f
            if p.exists() and p.is_file():
                zf.write(p, f)

    # Compute the external hash (NOT included in the ZIP).
    sha256 = hashlib.sha256(zip_path.read_bytes()).hexdigest()

    # Write the external hash file in dist/.
    hash_file = DIST / f"{zip_name}.sha256"
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
    print(f"  {zip_path.name} → {zip_path.name}.sha256")
    print("  Inside ZIP: RELEASE_ATTESTATION → QUALIFICATION_REPORT → PAYLOAD_MANIFEST → PayloadFiles")
    return 0


if __name__ == "__main__":
    sys.exit(main())
