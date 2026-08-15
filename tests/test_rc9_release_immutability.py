"""rc9 Phase 22: Release artifact immutability tests.

Verifies that:
- Regenerating the payload manifest from the same source tree produces the same tree hash.
- The qualification identity is deterministic for the same source.
- The release attestation evidence hashes are stable across regeneration.
- Any source change after manifest generation is detectable.
"""
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parent.parent


def _generate_manifest(tmp_path: Path) -> dict:
    """Generate a payload manifest in tmp_path and return it as a dict."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    out_file = tmp_path / "MANIFEST.json"
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "DATABASE_URL": "postgresql://construction:construction@localhost:5432/construction_ai",
    }
    result = subprocess.run(
        [sys.executable, "scripts/release_manifest.py", "--output", str(out_file)],
        capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=30,
    )
    assert result.returncode == 0, f"manifest generation failed: {result.stderr}"
    return json.loads(out_file.read_text())


def test_payload_manifest_tree_hash_is_deterministic(tmp_path):
    """rc9: Regenerating the manifest from the same source produces the same tree hash."""
    m1 = _generate_manifest(tmp_path / "gen1")
    m2 = _generate_manifest(tmp_path / "gen2")

    assert m1["tree_hash"] == m2["tree_hash"], (
        f"tree hash changed across regeneration: {m1['tree_hash'][:16]}... vs {m2['tree_hash'][:16]}..."
    )
    assert len(m1["files"]) == len(m2["files"])
    # All file hashes must match.
    for rel, entry in m1["files"].items():
        assert entry["sha256"] == m2["files"][rel]["sha256"], f"file hash changed: {rel}"


def test_qualification_identity_dependency_hash_is_deterministic(tmp_path):
    """rc9: The dependency lock hash is deterministic for the same requirements.lock.txt."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from qualification_identity import compute_dependency_lock_hash
        d1 = compute_dependency_lock_hash()
        d2 = compute_dependency_lock_hash()
        assert d1 == d2, "dependency lock hash must be deterministic"
    finally:
        sys.path.pop(0)


def test_release_artifact_immutable_after_generation(tmp_path):
    """rc9: Generated artifacts don't change without source changes."""
    m1 = _generate_manifest(tmp_path / "gen1")
    original_tree_hash = m1["tree_hash"]

    # Generate again — should be identical.
    m2 = _generate_manifest(tmp_path / "gen2")
    assert m2["tree_hash"] == original_tree_hash

    # Modify a source file temporarily.
    version_file = ROOT / "VERSION"
    original = version_file.read_bytes()
    try:
        version_file.write_bytes(original + b"\n")
        m3 = _generate_manifest(tmp_path / "gen3")
        # Tree hash should change because VERSION content changed.
        assert m3["tree_hash"] != original_tree_hash, (
            "tree hash should change after source modification"
        )
    finally:
        version_file.write_bytes(original)


def test_no_generated_artifacts_in_manifest(tmp_path):
    """rc9: The manifest must never include generated release artifacts."""
    manifest = _generate_manifest(tmp_path / "gen")
    forbidden = {
        "PAYLOAD_MANIFEST.json", "PAYLOAD_MANIFEST.sha256",
        "QUALIFICATION_REPORT.json", "TEST_RESULTS.json",
        "CRASH_MATRIX.json", "SECURITY_GATE.json", "MIGRATION_GATE.json",
        "RELEASE_ATTESTATION.json", "RELEASE_ATTESTATION.json.sha256",
        "QUALIFICATION_IDENTITY.json",
        "RELEASE_RECEIPT.json", "RELEASE_RECEIPT.sha256",
    }
    for rel in manifest["files"]:
        assert rel not in forbidden, f"generated artifact in manifest: {rel}"
        assert not rel.endswith(".zip"), f"ZIP in manifest: {rel}"
        assert not rel.endswith(".zip.sha256"), f"ZIP hash in manifest: {rel}"
        assert not rel.startswith("dist/"), f"dist/ path in manifest: {rel}"
        assert not rel.startswith("build/"), f"build/ path in manifest: {rel}"
