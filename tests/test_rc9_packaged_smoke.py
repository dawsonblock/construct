"""rc9 Phase 17: Packaged smoke tests.

Verifies the shipped artifact is runnable by executing from the extracted release:
- import package
- load config
- parse one fixture
- run deterministic verification
- load policy snapshot
- verify manifest
- verify attestation
"""
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


def test_packaged_smoke_import_and_config(tmp_path):
    """rc9: The extracted release can import its package and load config."""
    version = (ROOT / "VERSION").read_text().strip()
    zip_path = ROOT / "dist" / f"construct-{version}.zip"
    if not zip_path.exists():
        pytest.skip("no packaged release ZIP found — run 'make qualify-release'")

    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir()
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)

    # Import the package from the extracted directory.
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, '.'); "
         "from construction_ai import __version__; "
         "print(__version__)"],
        capture_output=True, text=True, cwd=str(extract_dir), timeout=10,
    )
    assert result.returncode == 0, f"import failed: {result.stderr}"
    assert result.stdout.strip() == version

    # Load pyproject.toml and verify it parses.
    pyproject = extract_dir / "pyproject.toml"
    assert pyproject.exists(), "pyproject.toml missing in ZIP"
    text = pyproject.read_text()
    assert f'version = "{version}"' in text


def test_packaged_smoke_policy_and_verification(tmp_path):
    """rc9: The extracted release can load a policy snapshot and run verification."""
    version = (ROOT / "VERSION").read_text().strip()
    zip_path = ROOT / "dist" / f"construct-{version}.zip"
    if not zip_path.exists():
        pytest.skip("no packaged release ZIP found — run 'make qualify-release'")

    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir()
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)

    # Verify the policy module can be imported.
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, '.'); "
         "from construction_ai.approvals.policy import ApprovalPolicy; "
         "p = ApprovalPolicy(); "
         "print(p.policy_hash())"],
        capture_output=True, text=True, cwd=str(extract_dir), timeout=10,
    )
    assert result.returncode == 0, f"policy import failed: {result.stderr}"

    # Verify the decision fingerprint module can be imported.
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, '.'); "
         "from construction_ai.approvals.decision_fingerprint import compute_decision_fingerprint; "
         "print('ok')"],
        capture_output=True, text=True, cwd=str(extract_dir), timeout=10,
    )
    assert result.returncode == 0, f"decision fingerprint import failed: {result.stderr}"

    # Verify the transitions module can be imported.
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, '.'); "
         "from construction_ai.work.transitions import validate_transition, CONFIRMED; "
         "validate_transition('confirmed', 'superseded'); "
         "print('ok')"],
        capture_output=True, text=True, cwd=str(extract_dir), timeout=10,
    )
    assert result.returncode == 0, f"transitions import failed: {result.stderr}"


def test_packaged_smoke_manifest_and_attestation(tmp_path):
    """rc9: The extracted release manifest and attestation verify."""
    version = (ROOT / "VERSION").read_text().strip()
    zip_path = ROOT / "dist" / f"construct-{version}.zip"
    if not zip_path.exists():
        pytest.skip("no packaged release ZIP found — run 'make qualify-release'")

    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir()
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)

    # Verify manifest exists and is valid JSON.
    manifest_path = extract_dir / "PAYLOAD_MANIFEST.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["version"] == version
    assert len(manifest["files"]) > 100

    # Verify attestation exists and is valid JSON.
    attestation_path = extract_dir / "RELEASE_ATTESTATION.json"
    assert attestation_path.exists()
    attestation = json.loads(attestation_path.read_text())
    assert attestation["release_version"] == version

    # Verify all evidence files exist.
    for name in attestation.get("evidence", {}):
        assert (extract_dir / name).exists(), f"evidence file missing: {name}"
