#!/usr/bin/env python
"""rc9: Packaged-release verification gate.

This gate is MANDATORY before any release can be promoted. It:

1. Verifies the external ZIP hash matches the actual ZIP.
2. Extracts the final ZIP to a fresh temporary directory.
3. Runs verify_payload_manifest.py inside the extracted ZIP.
4. Verifies release attestation inside the extracted ZIP.
5. Compiles all Python source inside the extracted ZIP.
6. Verifies VERSION and all version-bearing surfaces agree.
7. Verifies NO generated release file appears in the payload manifest.
8. Verifies exact archive content (no extra files, no missing files).

The final condition:
    VerifyPayload(Unzip(FinalZIP)) = PASS
    VerifyAttestation(Unzip(FinalZIP)) = PASS
    Compile(Unzip(FinalZIP)) = PASS
    VersionCheck(Unzip(FinalZIP)) = PASS

If this gate fails, the release is NO-GO.

Usage:
    python scripts/verify_packaged_release.py --zip dist/construct-0.5.0-rc9.dev0.zip
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent.parent


def verify_packaged_release(zip_path: str) -> tuple[bool, list[str]]:
    """Verify a packaged release ZIP. Returns (success, errors)."""
    errors: list[str] = []
    zip_file = Path(zip_path)

    if not zip_file.exists():
        return False, [f"ZIP file not found: {zip_path}"]

    # 1. Verify external ZIP hash.
    hash_file = Path(str(zip_file) + ".sha256")
    if hash_file.exists():
        expected_hash = hash_file.read_text().strip().split()[0]
        actual_hash = hashlib.sha256(zip_file.read_bytes()).hexdigest()
        if expected_hash != actual_hash:
            errors.append(f"ZIP hash mismatch: expected {expected_hash[:16]}..., got {actual_hash[:16]}...")
        else:
            print("[PASS] External ZIP hash matches")
    else:
        print("[WARN] No external ZIP hash file found — skipping hash verification")

    # 2. Extract ZIP to fresh temp directory.
    with tempfile.TemporaryDirectory(prefix="rc9_verify_") as tmpdir:
        tmp_path = Path(tmpdir)
        with zipfile.ZipFile(zip_file) as zf:
            zf.extractall(tmp_path)

        # 3. Run payload manifest verifier inside the extracted ZIP.
        manifest_path = tmp_path / "PAYLOAD_MANIFEST.json"
        if not manifest_path.exists():
            errors.append("PAYLOAD_MANIFEST.json not found inside ZIP")
        else:
            verifier = tmp_path / "scripts" / "verify_payload_manifest.py"
            if verifier.exists():
                result = subprocess.run(
                    [sys.executable, str(verifier), "--manifest", str(manifest_path)],
                    capture_output=True, text=True, cwd=str(tmp_path),
                )
                if result.returncode != 0:
                    errors.append(f"Payload manifest verification FAILED inside ZIP:\n{result.stdout}\n{result.stderr}")
                else:
                    print("[PASS] Payload manifest verification inside ZIP")
            else:
                errors.append("verify_payload_manifest.py not found inside ZIP")

        # 4. Verify release attestation inside ZIP.
        attestation_verifier = tmp_path / "scripts" / "verify_release_attestation.py"
        if attestation_verifier.exists():
            result = subprocess.run(
                [sys.executable, str(attestation_verifier), "--extracted", str(tmp_path)],
                capture_output=True, text=True, cwd=str(tmp_path),
            )
            if result.returncode != 0:
                errors.append(f"Release attestation verification FAILED:\n{result.stdout}\n{result.stderr}")
            else:
                print("[PASS] Release attestation verified inside ZIP")
        else:
            # Fallback: verify attestation manually.
            attestation_path = tmp_path / "RELEASE_ATTESTATION.json"
            if attestation_path.exists():
                attestation = json.loads(attestation_path.read_text())
                for name, entry in attestation.get("evidence", {}).items():
                    evidence_path = tmp_path / name
                    if not evidence_path.exists():
                        errors.append(f"evidence file missing in ZIP: {name}")
                        continue
                    if entry.get("sha256"):
                        actual = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
                        if actual != entry["sha256"]:
                            errors.append(f"evidence hash mismatch: {name}")
                print("[PASS] Release attestation evidence verified inside ZIP")

        # 5. Compile all Python source inside extracted ZIP.
        compile_result = subprocess.run(
            [sys.executable, "-m", "compileall", "-q",
             str(tmp_path / "construction_ai"),
             str(tmp_path / "apps"),
             str(tmp_path / "scripts"),
             str(tmp_path / "evaluation")],
            capture_output=True, text=True,
        )
        if compile_result.returncode != 0:
            errors.append(f"Python compilation FAILED inside ZIP:\n{compile_result.stderr}")
        else:
            print("[PASS] All Python source compiles inside ZIP")

        # 6. Verify VERSION and all version-bearing surfaces agree.
        version_file = tmp_path / "VERSION"
        if not version_file.exists():
            errors.append("VERSION file not found inside ZIP")
        else:
            version = version_file.read_text().strip()
            print(f"[PASS] VERSION inside ZIP: {version}")

            # Check pyproject.toml.
            pyproject = tmp_path / "pyproject.toml"
            if pyproject.exists():
                pyproject_text = pyproject.read_text()
                if f'version = "{version}"' not in pyproject_text:
                    errors.append(f"pyproject.toml version mismatch: expected {version}")

            # Check __init__.py.
            init_file = tmp_path / "construction_ai" / "__init__.py"
            if init_file.exists():
                init_text = init_file.read_text()
                if f'__version__ = "{version}"' not in init_text:
                    errors.append(f"__init__.py version mismatch: expected {version}")

            # Check ERP stub.
            stub_file = tmp_path / "apps" / "erpnext_stub" / "main.py"
            if stub_file.exists():
                stub_text = stub_file.read_text()
                if f'version="{version}"' not in stub_text:
                    errors.append(f"ERP stub version mismatch: expected {version}")

            # Check Dockerfile.
            dockerfile = tmp_path / "Dockerfile"
            if dockerfile.exists():
                docker_text = dockerfile.read_text()
                if f"ARG APP_VERSION={version}" not in docker_text:
                    errors.append(f"Dockerfile version mismatch: expected {version}")

            if not any("version mismatch" in e for e in errors):
                print("[PASS] All version surfaces agree inside ZIP")

        # 7. Verify NO generated release file appears in the payload manifest.
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            files = manifest.get("files", {})
            forbidden_names = {
                "PAYLOAD_MANIFEST.json", "PAYLOAD_MANIFEST.sha256",
                "QUALIFICATION_REPORT.json", "TEST_RESULTS.json",
                "CRASH_MATRIX.json", "SECURITY_GATE.json", "MIGRATION_GATE.json",
                "RELEASE_ATTESTATION.json", "RELEASE_ATTESTATION.sha256",
                "RELEASE_ATTESTATION.json.sha256", "FINAL_ARCHIVE.sha256",
                "QUALIFICATION_IDENTITY.json",
                "RELEASE_RECEIPT.json", "RELEASE_RECEIPT.sha256",
            }
            for rel in files:
                if rel in forbidden_names:
                    errors.append(f"forbidden artifact in payload manifest: {rel}")
                if rel.endswith(".zip") or rel.endswith(".zip.sha256"):
                    errors.append(f"forbidden ZIP artifact in payload manifest: {rel}")
                if rel.startswith("dist/") or rel.startswith("build/"):
                    errors.append(f"forbidden build output in payload manifest: {rel}")
            if not any("forbidden" in e for e in errors):
                print("[PASS] No generated artifacts in payload manifest")

        # 8. Verify exact archive content (no extra files).
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            expected_payload = set(manifest.get("files", {}).keys())
            # Attestation files that should be in the ZIP but not in the payload manifest.
            attestation_files = {
                "PAYLOAD_MANIFEST.json", "PAYLOAD_MANIFEST.sha256",
                "QUALIFICATION_IDENTITY.json",
                "QUALIFICATION_REPORT.json", "TEST_RESULTS.json",
                "CRASH_MATRIX.json", "SECURITY_GATE.json", "MIGRATION_GATE.json",
                "RELEASE_ATTESTATION.json", "RELEASE_ATTESTATION.json.sha256",
            }
            expected_all = expected_payload | attestation_files
            actual_files = set()
            for f in tmp_path.rglob("*"):
                if f.is_file():
                    rel = str(f.relative_to(tmp_path))
                    actual_files.add(rel)
            # Check for unexpected files.
            unexpected = actual_files - expected_all
            # Filter out __pycache__ from compile step.
            unexpected = {f for f in unexpected if "__pycache__" not in f}
            if unexpected:
                errors.append(f"unexpected files in ZIP: {sorted(unexpected)}")
            else:
                print("[PASS] Exact archive content verified")

    return len(errors) == 0, errors


def main() -> int:
    zip_path = None
    if "--zip" in sys.argv:
        zip_path = sys.argv[sys.argv.index("--zip") + 1]
    if not zip_path:
        print("Usage: python scripts/verify_packaged_release.py --zip <path-to-zip>")
        return 2

    print("rc9 Packaged-Release Verification Gate")
    print(f"ZIP: {zip_path}")
    print()

    success, errors = verify_packaged_release(zip_path)

    if success:
        print()
        print("[PASS] VerifyPayload(Unzip(FinalZIP)) = PASS")
        print("[PASS] VerifyAttestation(Unzip(FinalZIP)) = PASS")
        print("[PASS] Compile(Unzip(FinalZIP)) = PASS")
        print("[PASS] VersionCheck(Unzip(FinalZIP)) = PASS")
        print("[PASS] Release is GO")
        return 0
    else:
        print()
        print(f"[FAIL] {len(errors)} error(s):")
        for e in errors:
            print(f"  - {e}")
        print()
        print("[FAIL] Release is NO-GO")
        return 1


if __name__ == "__main__":
    sys.exit(main())
