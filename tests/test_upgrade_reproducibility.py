"""v0.4.7 — upgrade gate, release manifest, and reproducibility (items 41, 42, 43).

Verifies that:
1. The upgrade gate script passes all checks.
2. The release manifest is stable (same code → same manifest, modulo timestamp).
3. The reproducibility check passes (deterministic system).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path



def test_upgrade_gate_passes():
    """The upgrade gate script exits 0 with all checks passing."""
    script = Path(__file__).parent.parent / "scripts" / "upgrade_gate.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"upgrade gate failed:\n{result.stdout}\n{result.stderr}"
    assert "All" in result.stdout
    assert "upgrade checks passed" in result.stdout
    expected_checks = [
        "migrations_sequential",
        "all_migrations_applied",
        "migration_checksums",
        "database_roles_exist",
        "app_role_select",
        "no_pending_migrations",
    ]
    for check in expected_checks:
        assert check in result.stdout, f"missing check {check}"


def test_release_manifest_is_stable():
    """Generating the manifest twice produces the same fingerprints (modulo timestamp)."""
    script = Path(__file__).parent.parent / "scripts" / "release_manifest.py"
    result1 = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=30)
    assert result1.returncode == 0, f"manifest generation failed: {result1.stderr}"
    result2 = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=30)
    assert result2.returncode == 0, f"second manifest generation failed: {result2.stderr}"

    m1 = json.loads(result1.stdout)
    m2 = json.loads(result2.stdout)
    # Compare everything except generated_at.
    m1.pop("generated_at", None)
    m2.pop("generated_at", None)
    assert m1 == m2, "manifest differs between runs"


def test_release_manifest_has_required_fields():
    """The manifest contains all required fields."""
    script = Path(__file__).parent.parent / "scripts" / "release_manifest.py"
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0
    manifest = json.loads(result.stdout)
    required_fields = [
        "version", "git_commit", "git_branch", "generated_at",
        "migration_count", "migration_fingerprint", "code_fingerprint",
        "test_fingerprint", "schema_fingerprint", "dependencies", "migrations",
    ]
    for field in required_fields:
        assert field in manifest, f"missing field {field} in manifest"
    assert manifest["migration_count"] > 0
    assert len(manifest["migrations"]) == manifest["migration_count"]
    # Each migration has a filename and sha256.
    for m in manifest["migrations"]:
        assert "filename" in m
        assert "sha256" in m
        assert len(m["sha256"]) == 64


def test_release_manifest_migration_checksums_match_files():
    """The manifest's migration checksums match the actual files."""
    script = Path(__file__).parent.parent / "scripts" / "release_manifest.py"
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0
    manifest = json.loads(result.stdout)
    import hashlib
    migrations_dir = Path(__file__).parent.parent / "migrations"
    for m in manifest["migrations"]:
        filepath = migrations_dir / m["filename"]
        assert filepath.exists(), f"migration file {m['filename']} missing"
        computed = hashlib.sha256(filepath.read_bytes()).hexdigest()
        assert computed == m["sha256"], f"checksum mismatch on {m['filename']}"


def test_reproducibility_check_passes():
    """The reproducibility check script exits 0 with all checks passing."""
    script = Path(__file__).parent.parent / "scripts" / "reproducibility_check.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"reproducibility check failed:\n{result.stdout}\n{result.stderr}"
    assert "All" in result.stdout
    assert "reproducibility checks passed" in result.stdout
    expected_checks = [
        "manifest_stable",
        "migration_idempotent",
        "reconstruction_deterministic",
        "test_suite_deterministic",
        "upgrade_gate",
        "security_gate",
    ]
    for check in expected_checks:
        assert check in result.stdout, f"missing check {check}"
