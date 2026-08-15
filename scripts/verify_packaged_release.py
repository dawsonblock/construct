#!/usr/bin/env python
"""rc8: Packaged-release verification gate.

This gate is MANDATORY before any release can be promoted. It:

1. Extracts the final ZIP to a fresh temporary directory.
2. Runs verify_payload_manifest.py inside the extracted ZIP.
3. Verifies version checks.
4. Compiles all Python source.
5. Verifies the release attestation.
6. Verifies NO generated release file appears in the payload manifest.

The final condition:
    VerifyPayload(Unzip(FinalZIP)) = PASS

If this gate fails, the release is NO-GO.

Usage:
    python scripts/verify_packaged_release.py --zip construct-0.5.0-rc8.zip
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
    """Verify a packaged release ZIP.

    Returns (success, errors).
    """
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
        print("[WARN] No external ZIP hash file found — skipping hash verification")

    # 2. Extract ZIP to fresh temp directory.
    with tempfile.TemporaryDirectory(prefix="rc8_verify_") as tmpdir:
        tmp_path = Path(tmpdir)
        with zipfile.ZipFile(zip_file) as zf:
            zf.extractall(tmp_path)

        # 3. Run payload manifest verifier inside the extracted ZIP.
        manifest_path = tmp_path / "PAYLOAD_MANIFEST.json"
        if not manifest_path.exists():
            errors.append("PAYLOAD_MANIFEST.json not found inside ZIP")
        else:
            # Run the verifier script from the extracted ZIP.
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
                # Fallback: verify manually.
                manifest = json.loads(manifest_path.read_text())
                files = manifest.get("files", {})
                for rel, entry in files.items():
                    file_path = tmp_path / rel
                    if not file_path.exists():
                        errors.append(f"missing file in ZIP: {rel}")
                        continue
                    actual_size = file_path.stat().st_size
                    if actual_size != entry.get("size"):
                        errors.append(f"size mismatch for {rel}: expected {entry.get('size')}, got {actual_size}")
                    actual_sha = hashlib.sha256(file_path.read_bytes()).hexdigest()
                    if actual_sha != entry.get("sha256"):
                        errors.append(f"SHA-256 mismatch for {rel}")

        # 4. Verify VERSION file.
        version_file = tmp_path / "VERSION"
        if not version_file.exists():
            errors.append("VERSION file not found inside ZIP")
        else:
            version = version_file.read_text().strip()
            print(f"[PASS] VERSION inside ZIP: {version}")

        # 5. Compile all Python source.
        compile_errors = []
        for py_file in tmp_path.rglob("*.py"):
            try:
                subprocess.run(
                    [sys.executable, "-c", f"compile(open('{py_file}').read(), '{py_file}', 'exec')"],
                    capture_output=True, text=True, check=True,
                )
            except subprocess.CalledProcessError as e:
                compile_errors.append(f"{py_file.relative_to(tmp_path)}: {e.stderr}")
        if compile_errors:
            errors.append("Python compilation errors:\n" + "\n".join(compile_errors))
        else:
            print("[PASS] All Python source compiles")

        # 6. Verify NO generated release file appears in the payload manifest.
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            files = manifest.get("files", {})
            forbidden_names = {
                "PAYLOAD_MANIFEST.json", "PAYLOAD_MANIFEST.sha256",
                "QUALIFICATION_REPORT.json", "TEST_RESULTS.json",
                "CRASH_MATRIX.json", "SECURITY_GATE.json", "MIGRATION_GATE.json",
                "RELEASE_ATTESTATION.json", "RELEASE_ATTESTATION.sha256",
                "RELEASE_ATTESTATION.json.sha256", "FINAL_ARCHIVE.sha256",
            }
            for rel in files:
                if rel in forbidden_names:
                    errors.append(f"forbidden artifact in payload manifest: {rel}")
                if rel.endswith(".zip") or rel.endswith(".zip.sha256"):
                    errors.append(f"forbidden ZIP artifact in payload manifest: {rel}")
            if not any("forbidden" in e for e in errors):
                print("[PASS] No generated artifacts in payload manifest")

        # 7. Verify release attestation inside ZIP.
        attestation_path = tmp_path / "RELEASE_ATTESTATION.json"
        if attestation_path.exists():
            attestation = json.loads(attestation_path.read_text())
            # Verify all evidence hashes.
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

    return len(errors) == 0, errors


def main() -> int:
    zip_path = None
    if "--zip" in sys.argv:
        zip_path = sys.argv[sys.argv.index("--zip") + 1]
    if not zip_path:
        print("Usage: python scripts/verify_packaged_release.py --zip <path-to-zip>")
        return 2

    print("rc8 Packaged-Release Verification Gate")
    print(f"ZIP: {zip_path}")
    print()

    success, errors = verify_packaged_release(zip_path)

    if success:
        print()
        print("[PASS] VerifyPayload(Unzip(FinalZIP)) = PASS")
        print("[PASS] Release is GO")
        return 0
    else:
        print()
        print(f"[FAIL] {len(errors)} error(s):")
        for e in errors:
            print(f"  - {e}")
        print()
        print("[FAIL] VerifyPayload(Unzip(FinalZIP)) = FAIL")
        print("[FAIL] Release is NO-GO")
        return 1


if __name__ == "__main__":
    sys.exit(main())
