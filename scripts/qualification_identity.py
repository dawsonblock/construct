"""rc8: Shared qualification identity helpers.

Every gate artifact generator MUST use these helpers to compute identity
fields. This prevents the rc7 defect where different scripts computed
dependency_lock_hash differently (one hashed both lock files, another
hashed only requirements.lock.txt).

Usage:
    from qualification_identity import compute_dependency_lock_hash, generate_qualification_identity

    dep_hash = compute_dependency_lock_hash()
    identity = generate_qualification_identity(payload_tree_hash=tree_hash, schema_fingerprint=schema_fp)
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent


def compute_dependency_lock_hash() -> str:
    """rc8: Canonical dependency lock hash.

    Hashes BOTH requirements.lock.txt and requirements-dev.lock.txt.
    This is the ONE function every gate must call. Do not independently
    recompute dependency hashes in gate generators.
    """
    hasher = hashlib.sha256()
    for name in ("requirements.lock.txt", "requirements-dev.lock.txt"):
        path = ROOT / name
        if path.exists():
            hasher.update(path.read_bytes())
        hasher.update(b"\x00")  # separator
    return hasher.hexdigest()


def compute_runtime_lock_hash() -> str:
    """rc8: Hash of requirements.lock.txt only (runtime dependencies)."""
    path = ROOT / "requirements.lock.txt"
    if path.exists():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    return "missing"


def compute_dev_lock_hash() -> str:
    """rc8: Hash of requirements-dev.lock.txt only (dev dependencies)."""
    path = ROOT / "requirements-dev.lock.txt"
    if path.exists():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    return "missing"


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, cwd=ROOT,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def _git_branch() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, check=True, cwd=ROOT,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def _version() -> str:
    version_file = ROOT / "VERSION"
    if version_file.exists():
        return version_file.read_text().strip()
    return "unknown"


def _qualification_run_id(git_commit: str) -> str:
    """Generate a deterministic qualification run ID from commit and date."""
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"qual-{today}-{git_commit[:12]}"


def generate_qualification_identity(
    *,
    payload_tree_hash: str,
    schema_fingerprint: str | None = None,
    migration_fingerprint: str | None = None,
) -> dict[str, Any]:
    """rc8: Generate the ONE immutable qualification identity object.

    Every gate artifact must embed this exact object. Do not independently
    recompute identity fields in gate generators — read them from here.

    This prevents the rc7 defect where dependency_lock_hash differed
    between QUALIFICATION_REPORT.json and the gate artifacts.
    """
    git_commit = _git_commit()
    return {
        "release_version": _version(),
        "git_commit": git_commit,
        "git_branch": _git_branch(),
        "payload_tree_hash": payload_tree_hash,
        "schema_fingerprint": schema_fingerprint or "offline",
        "migration_fingerprint": migration_fingerprint or "unknown",
        "runtime_lock_hash": compute_runtime_lock_hash(),
        "dev_lock_hash": compute_dev_lock_hash(),
        "dependency_lock_hash": compute_dependency_lock_hash(),
        "qualification_run_id": _qualification_run_id(git_commit),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def write_qualification_identity(identity: dict[str, Any], output: Path | None = None) -> Path:
    """Write the qualification identity to QUALIFICATION_IDENTITY.json."""
    out = output or (ROOT / "QUALIFICATION_IDENTITY.json")
    out.write_text(json.dumps(identity, indent=2, sort_keys=True, default=str))
    return out
