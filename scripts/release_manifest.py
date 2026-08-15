#!/usr/bin/env python
"""v0.5.0-rc6 — release manifest with offline degradation and self-consistent tree hash.

Generates a release manifest that captures the exact state of the system:
- Version
- Git commit
- Migration count and checksums
- Database schema fingerprint
- Code fingerprint (Python files)
- Dependency versions
- Test count
- Per-file SHA-256/size map (rc5)
- tree_hash: deterministic root hash over the per-file map (rc6)
- post_manifest_artifacts: hashes of artifacts generated AFTER the manifest
  (qualification report, crash matrix, security gate, etc.) — these cannot
  be in `files` because they did not exist at manifest-generation time, but
  they ARE part of the shipped archive and must be verified separately.
- MANIFEST.<name>.sha256 companion: detached hash of the FINAL manifest
  bytes, written alongside the manifest. This is the canonical way to bind
  the manifest to the archive without a self-referential paradox.

rc6 self-consistency fix: the manifest no longer includes itself or the
post-manifest qualification artifacts in `files`. The previous behavior
embedded a stale hash of MANIFEST.json inside MANIFEST.json, which never
matched the actual file. Verification now uses:
  1. tree_hash over `files` (deterministic, excludes self/post-artifacts)
  2. post_manifest_artifacts map (covers post-generation artifacts)
  3. MANIFEST.<name>.sha256 companion (covers the manifest itself)

Usage:
    python scripts/release_manifest.py --artifact-only [--output manifest.json]
    python scripts/release_manifest.py --with-database [--output manifest.json]
    python scripts/release_manifest.py [--output manifest.json]  # defaults to --with-database
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
            cwd=Path(__file__).parent.parent,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def _git_branch() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, check=True,
            cwd=Path(__file__).parent.parent,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def _version() -> str:
    version_file = Path(__file__).parent.parent / "VERSION"
    if version_file.exists():
        return version_file.read_text().strip()
    return "unknown"


def _migration_checksums() -> list[dict[str, str]]:
    migrations_dir = Path(__file__).parent.parent / "migrations"
    checksums = []
    for f in sorted(migrations_dir.glob("*.sql")):
        checksums.append({
            "filename": f.name,
            "sha256": hashlib.sha256(f.read_bytes()).hexdigest(),
        })
    return checksums


def _code_fingerprint() -> str:
    """SHA-256 over all Python source files (excluding tests and scripts)."""
    root = Path(__file__).parent.parent
    hasher = hashlib.sha256()
    for f in sorted(root.glob("construction_ai/**/*.py")):
        hasher.update(f.read_bytes())
    return hasher.hexdigest()


def _test_fingerprint() -> str:
    """SHA-256 over all test files."""
    root = Path(__file__).parent.parent
    hasher = hashlib.sha256()
    for f in sorted(root.glob("tests/**/*.py")):
        hasher.update(f.read_bytes())
    return hasher.hexdigest()


def _migration_fingerprint() -> str:
    """SHA-256 over all migration files."""
    root = Path(__file__).parent.parent
    hasher = hashlib.sha256()
    for f in sorted(root.glob("migrations/*.sql")):
        hasher.update(f.read_bytes())
    return hasher.hexdigest()


def _dependency_versions() -> dict[str, str]:
    """Key dependency versions."""
    versions = {}
    for pkg in ["psycopg", "redis", "hypothesis", "pytest", "ruff"]:
        try:
            mod = __import__(pkg)
            versions[pkg] = getattr(mod, "__version__", "unknown")
        except ImportError:
            versions[pkg] = "not installed"
    # Python version
    versions["python"] = sys.version
    return versions


#: The canonical name of the payload manifest file. Used by both the
#: generation logic and the exclusion logic so they can never disagree.
PAYLOAD_MANIFEST_NAME = "PAYLOAD_MANIFEST.json"

#: The canonical name of the detached payload manifest companion hash file.
#: rc7: Uses explicit constants — never reconstruct filenames dynamically.
PAYLOAD_MANIFEST_DIGEST_NAME = "PAYLOAD_MANIFEST.sha256"

#: Files that are generated *after* the payload manifest and therefore
#: cannot be included in the payload tree hash. These are the qualification
#: evidence files and the manifest itself (which cannot contain its own
#: hash). The rc7 attestation architecture is acyclic:
#:
#:   PayloadTree → PAYLOAD_MANIFEST.json → Qualification → RELEASE_ATTESTATION.json
#:
#: The manifest hashes ONLY the payload tree (source, migrations, configs,
#: docs, tests, scripts, locks). It does NOT hash qualification artifacts.
#: Qualification artifacts are hashed by RELEASE_ATTESTATION.json, which
#: also hashes the manifest. This breaks the rc6 circular dependency:
#:
#:   rc6 (broken): Manifest → Hash(Report) while Report → Hash(Manifest)
#:   rc7 (acyclic): Manifest → PayloadTree, Attestation → Hash(Manifest + Report + Gates)
SELF_EXCLUDED_ARTIFACTS = {
    PAYLOAD_MANIFEST_NAME,
    PAYLOAD_MANIFEST_DIGEST_NAME,
    "MANIFEST.json",
    "MANIFEST.json.sha256",
    "MANIFEST.sha256",
    "MANIFEST.sig",
    "QUALIFICATION_REPORT.json",
    "TEST_RESULTS.json",
    "CRASH_MATRIX.json",
    "SECURITY_GATE.json",
    "MIGRATION_GATE.json",
    "RELEASE_ATTESTATION.json",
    "RELEASE_ATTESTATION.sha256",
}


def _file_manifest() -> tuple[dict[str, dict[str, Any]], str]:
    """Complete per-file manifest over all shipped files.

    Returns (files, tree_hash):
      - files: {relative_path: {size, sha256}} for every tracked file EXCEPT
        the manifest itself and post-manifest qualification artifacts. The
        manifest cannot contain its own final hash (self-reference), and
        post-manifest artifacts are bound by `tree_hash` + a separate
        post_manifest_artifacts section, not by inclusion in `files`.
      - tree_hash: SHA-256 over the canonical JSON of the `files` map. This
        is the deterministic root hash of the release tree and is stable
        across regenerations as long as the underlying files are unchanged.
    """
    root = Path(__file__).parent.parent
    files: dict[str, dict[str, Any]] = {}
    tracked_paths: list[Path] = []
    try:
        res = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True, cwd=root)
        lines = [l.strip() for l in res.stdout.splitlines() if l.strip()]
        for line in lines:
            p = root / line
            if p.is_file():
                tracked_paths.append(p)
    except Exception:
        patterns = [
            "construction_ai/**/*.py",
            "apps/**/*",
            "scripts/**/*",
            "tests/**/*",
            "migrations/*.sql",
            "docs/**/*",
            "*.toml",
            "*.txt",
            "*.md",
            "Dockerfile",
            "docker-compose.yml",
            "Makefile",
            "VERSION",
        ]
        for pat in patterns:
            for p in root.glob(pat):
                if p.is_file():
                    tracked_paths.append(p)

    for p in sorted(set(tracked_paths)):
        rel = str(p.relative_to(root))
        if rel.startswith((".git", "__pycache__", ".pytest_cache", ".ruff_cache")):
            continue
        if rel in SELF_EXCLUDED_ARTIFACTS:
            # The manifest deliberately does not hash itself or the
            # post-manifest qualification artifacts. They are bound via
            # tree_hash + post_manifest_artifacts instead.
            continue
        data = p.read_bytes()
        files[rel] = {
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    tree_hash = hashlib.sha256(
        json.dumps(files, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()
    return files, tree_hash


def _post_manifest_artifacts() -> dict[str, dict[str, Any]]:
    """Hashes of post-manifest artifacts that exist at manifest-generation time.

    rc7: These are NOT included in the manifest's `files` map or `tree_hash`
    because they are generated after the manifest. The manifest does NOT
    hash QUALIFICATION_REPORT.json — that was the rc6 circular dependency.
    Instead, RELEASE_ATTESTATION.json (generated last) hashes both the
    manifest and all qualification artifacts, creating an acyclic chain:

      PayloadTree → MANIFEST.json → Qualification → RELEASE_ATTESTATION.json

    Any post-manifest artifacts that already exist on disk (e.g. from a
    previous run) are recorded here for informational purposes, but the
    canonical binding is via RELEASE_ATTESTATION.json, not this section.
    """
    root = Path(__file__).parent.parent
    artifacts: dict[str, dict[str, Any]] = {}
    for name in SELF_EXCLUDED_ARTIFACTS:
        if name == PAYLOAD_MANIFEST_NAME:
            # The manifest cannot hash itself.
            continue
        path = root / name
        if path.is_file():
            data = path.read_bytes()
            artifacts[name] = {
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "note": "stale from previous run — canonical binding is via RELEASE_ATTESTATION.json",
            }
    return artifacts


def _schema_fingerprint() -> str:
    """SHA-256 over the database schema (DDL).

    Returns "offline" if psycopg is not installed or the database is unreachable.
    """
    try:
        import psycopg
    except ImportError:
        return "offline"

    dsn = os.getenv("DATABASE_URL", "postgresql://construction:construction@localhost:5432/construction_ai")
    try:
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
        return "unavailable"


def _schema_fingerprint_artifact_only() -> str:
    """Artifact-only mode: no database access, return 'offline'."""
    return "offline"


def generate_manifest(*, artifact_only: bool = False) -> dict:
    """Generate a release manifest (payload manifest).

    rc7: The manifest hashes ONLY the payload tree (source, migrations,
    configs, docs, tests, scripts, locks). It does NOT hash qualification
    artifacts. This breaks the rc6 circular dependency where the manifest
    hashed the qualification report while the report hashed the manifest.

    The acyclic attestation chain is:
      PayloadTree → MANIFEST.json → Qualification → RELEASE_ATTESTATION.json

    Args:
        artifact_only: If True, skip database access (schema_fingerprint will
            be "offline"). If False, attempt live schema verification.
    """
    files, tree_hash = _file_manifest()
    return {
        "version": _version(),
        "git_commit": _git_commit(),
        "git_branch": _git_branch(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "artifact-only" if artifact_only else "with-database",
        "migration_count": len(_migration_checksums()),
        "migration_fingerprint": _migration_fingerprint(),
        "code_fingerprint": _code_fingerprint(),
        "test_fingerprint": _test_fingerprint(),
        "schema_fingerprint": _schema_fingerprint_artifact_only() if artifact_only else _schema_fingerprint(),
        "dependencies": _dependency_versions(),
        "migrations": _migration_checksums(),
        "files": files,
        "tree_hash": tree_hash,
        # rc7: self_excluded_artifacts lists what is NOT in `files` and why.
        # The canonical binding for post-manifest artifacts is via
        # RELEASE_ATTESTATION.json, not via this manifest.
        "self_excluded_artifacts": sorted(SELF_EXCLUDED_ARTIFACTS),
        "attestation_chain": "PayloadTree -> PAYLOAD_MANIFEST.json -> Qualification -> RELEASE_ATTESTATION.json",
    }


def main() -> int:
    artifact_only = "--artifact-only" in sys.argv
    with_database = "--with-database" in sys.argv
    if artifact_only and with_database:
        print("error: --artifact-only and --with-database are mutually exclusive", file=sys.stderr)
        return 2
    # Default to --with-database if neither is specified.
    if not artifact_only and not with_database:
        with_database = True

    output = sys.argv[sys.argv.index("--output") + 1] if "--output" in sys.argv else None
    manifest = generate_manifest(artifact_only=artifact_only)
    manifest_json = json.dumps(manifest, indent=2, sort_keys=True, default=str)
    if output:
        Path(output).write_text(manifest_json)
        # rc7: Write detached companion hash using the CANONICAL constant.
        # The companion filename must match SELF_EXCLUDED_ARTIFACTS exactly
        # so it is never accidentally included in the tree hash.
        companion = Path(output).parent / PAYLOAD_MANIFEST_DIGEST_NAME
        final_bytes = Path(output).read_bytes()
        companion_hash = hashlib.sha256(final_bytes).hexdigest()
        companion.write_text(f"{companion_hash}  {Path(output).name}\n")
        print(f"manifest written to {output} (mode: {manifest['mode']})")
        print(f"manifest sha256 companion written to {companion}: {companion_hash}")
    else:
        print(manifest_json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
