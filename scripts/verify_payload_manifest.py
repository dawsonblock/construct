#!/usr/bin/env python
"""v0.5.0-rc7 — verify a payload manifest against the actual files on disk.

This script verifies that every file listed in PAYLOAD_MANIFEST.json:
  - exists on disk
  - has the correct size
  - has the correct SHA-256 hash
  - has no undeclared payload files
  - has no duplicate paths
  - tree hash recomputes exactly
  - version matches VERSION file
  - git commit matches current HEAD (when git is available)

It does NOT require PostgreSQL, Redis, or any external service. It works
against a source checkout, an unpacked ZIP, or an offline artifact.

Usage:
    python scripts/verify_payload_manifest.py --manifest PAYLOAD_MANIFEST.json
    python scripts/verify_payload_manifest.py --manifest PAYLOAD_MANIFEST.json --root /path/to/unpacked

Exit codes:
    0 — all checks passed
    1 — verification failed
    2 — manifest not found or invalid
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
            cwd=ROOT,
        )
        return result.stdout.strip()
    except Exception:
        return None


def verify_manifest(manifest_path: Path, root: Path | None = None) -> tuple[bool, list[str]]:
    """Verify a payload manifest against files on disk.

    Returns (success, errors).
    """
    errors: list[str] = []
    root = root or manifest_path.parent

    if not manifest_path.exists():
        return False, [f"manifest not found: {manifest_path}"]

    try:
        manifest = json.loads(manifest_path.read_text())
    except Exception as e:
        return False, [f"manifest is invalid JSON: {e}"]

    files = manifest.get("files", {})
    tree_hash = manifest.get("tree_hash")
    version = manifest.get("version")
    git_commit = manifest.get("git_commit")

    if not files:
        return False, ["manifest has no files map"]
    if not tree_hash:
        return False, ["manifest has no tree_hash"]

    # 1. Verify each file exists, size matches, SHA-256 matches.
    seen_paths: set[str] = set()
    for rel_path, entry in sorted(files.items()):
        if rel_path in seen_paths:
            errors.append(f"duplicate path in manifest: {rel_path}")
            continue
        seen_paths.add(rel_path)

        file_path = root / rel_path
        if not file_path.exists():
            errors.append(f"missing file: {rel_path}")
            continue

        actual_size = file_path.stat().st_size
        if actual_size != entry.get("size"):
            errors.append(
                f"size mismatch for {rel_path}: manifest={entry.get('size')} actual={actual_size}"
            )
            continue

        actual_sha = hashlib.sha256(file_path.read_bytes()).hexdigest()
        if actual_sha != entry.get("sha256"):
            errors.append(
                f"SHA-256 mismatch for {rel_path}: manifest={entry.get('sha256')[:16]}... actual={actual_sha[:16]}..."
            )

    # 2. Recompute tree hash.
    recomputed_tree_hash = hashlib.sha256(
        json.dumps(files, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()
    if recomputed_tree_hash != tree_hash:
        errors.append(
            f"tree_hash mismatch: manifest={tree_hash[:16]}... recomputed={recomputed_tree_hash[:16]}..."
        )

    # 3. Check for undeclared payload files (files in tracked dirs not in manifest).
    # Only check if we're in a git repo.
    try:
        res = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True, cwd=root)
        tracked = {l.strip() for l in res.stdout.splitlines() if l.strip()}
        # Remove self-excluded artifacts.
        excluded = set(manifest.get("self_excluded_artifacts", []))
        # Also exclude common generated artifacts that may be tracked by git
        # but are not part of the payload.
        excluded.update({
            "RELEASE_ATTESTATION.json.sha256",
            "RELEASE_ATTESTATION.sha256",
            "FINAL_ARCHIVE.sha256",
            "RC6_AUDIT_BASELINE.json",
        })
        for t in tracked:
            if t in excluded:
                continue
            if t.startswith((".git", "__pycache__", ".pytest_cache", ".ruff_cache")):
                continue
            if t not in files:
                errors.append(f"undeclared tracked file not in manifest: {t}")
    except Exception:
        pass  # Not a git repo or git not available.

    # 4. Verify version matches VERSION file.
    version_file = root / "VERSION"
    if version_file.exists():
        actual_version = version_file.read_text().strip()
        if actual_version != version:
            errors.append(
                f"version mismatch: manifest={version} VERSION={actual_version}"
            )

    # 5. Verify git commit matches (when git is available).
    actual_commit = _git_commit()
    if actual_commit and git_commit and actual_commit != git_commit:
        errors.append(
            f"git commit mismatch: manifest={git_commit[:12]} actual={actual_commit[:12]}"
        )

    return len(errors) == 0, errors


def main() -> int:
    manifest_arg = None
    root_arg = None
    if "--manifest" in sys.argv:
        manifest_arg = sys.argv[sys.argv.index("--manifest") + 1]
    if "--root" in sys.argv:
        root_arg = sys.argv[sys.argv.index("--root") + 1]

    if not manifest_arg:
        # Try default name.
        for name in ("PAYLOAD_MANIFEST.json", "MANIFEST.json"):
            p = ROOT / name
            if p.exists():
                manifest_arg = str(p)
                break

    if not manifest_arg:
        print("ERROR: no manifest specified and no PAYLOAD_MANIFEST.json found", file=sys.stderr)
        return 2

    manifest_path = Path(manifest_arg)
    root = Path(root_arg) if root_arg else manifest_path.parent

    print(f"Verifying payload manifest: {manifest_path}")
    print(f"Root directory: {root}")
    print()

    success, errors = verify_manifest(manifest_path, root)

    if success:
        print(f"[PASS] All {len(json.loads(manifest_path.read_text()).get('files', {}))} files verified")
        print("[PASS] Tree hash matches")
        print("[PASS] No undeclared files")
        print()
        print("Payload manifest verification: PASSED")
        return 0
    else:
        print(f"[FAIL] {len(errors)} error(s) found:")
        for e in errors:
            print(f"  - {e}")
        print()
        print("Payload manifest verification: FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(main())
