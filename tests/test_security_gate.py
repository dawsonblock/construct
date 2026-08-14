"""v0.4.7 — security gate test (item 40).

Runs the security gate script and verifies all checks pass.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_security_gate_passes():
    """The security gate script exits 0 with all checks passing."""
    script = Path(__file__).parent.parent / "scripts" / "security_gate.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"security gate failed:\n{result.stdout}\n{result.stderr}"
    assert "All" in result.stdout
    assert "security checks passed" in result.stdout
    # Verify all expected checks ran.
    expected_checks = [
        "app_role_not_superuser",
        "app_role_no_bypassrls",
        "rls_enabled",
        "audit_append_only",
        "external_actions_no_delete",
        "cross_tenant_isolation",
        "migration_checksums",
    ]
    for check in expected_checks:
        assert check in result.stdout, f"missing check {check} in output"
