"""v0.5.0-rc7 Phase 35 — Critical test classification map.

Explicit classification of which test categories are critical for
qualification. Criticality is NOT inferred from filename dynamically.

Classification:
  security       → critical
  tenant/RLS     → critical
  approval       → critical
  ERP effect     → critical
  crash recovery → critical
  migrations     → critical
  release attest → critical
  UI visual      → noncritical (allowed to skip)

Qualification fails if any critical test is skipped.
"""
from __future__ import annotations

#: Explicit critical test classification map.
#: Maps test file patterns to criticality.
CRITICAL_TEST_PATTERNS = {
    # Security tests
    "tests/test_security_gate.py": "critical",
    "tests/test_ui_security.py": "critical",
    "tests/test_api_auth.py": "critical",
    # Tenant/RLS tests
    "tests/test_tenant_isolation.py": "critical",
    "tests/test_rls.py": "critical",
    # Approval tests
    "tests/test_approval_service.py": "critical",
    "tests/test_decision_fingerprint.py": "critical",
    "tests/test_stale_approval.py": "critical",
    "tests/test_execution_preconditions.py": "critical",
    # ERP effect tests
    "tests/test_external_effects.py": "critical",
    "tests/test_executor.py": "critical",
    "tests/test_reconciliation.py": "critical",
    "tests/test_recovery_daemon.py": "critical",
    "tests/test_reconcile_unknown.py": "critical",
    # Crash recovery tests
    "tests/test_crash_injection.py": "critical",
    "tests/test_crash_injection_erp.py": "critical",
    # Migration tests
    "tests/test_upgrade_reproducibility.py": "critical",
    "tests/test_migrations.py": "critical",
    # Release attestation tests
    "tests/test_release_artifact_integrity.py": "critical",
    "tests/test_rc6_hardening.py": "critical",
    "tests/test_rc7_hardening.py": "critical",
    # Noncritical
    "tests/test_ui_visual.py": "noncritical",
}

#: Categories that are always critical.
CRITICAL_CATEGORIES = {
    "security",
    "tenant/rls",
    "approval",
    "erp_effect",
    "crash_recovery",
    "migrations",
    "release_attest",
}

#: Categories that may be skipped without failing qualification.
NONCRITICAL_CATEGORIES = {
    "ui_visual",
}


def classify_test(test_file: str) -> str:
    """Classify a test file as critical or noncritical.

    Returns "critical" or "noncritical".
    """
    if test_file in CRITICAL_TEST_PATTERNS:
        return CRITICAL_TEST_PATTERNS[test_file]
    # Default: critical for unknown tests (fail-safe).
    return "critical"


def is_critical(test_file: str) -> bool:
    """Return True if the test file is critical."""
    return classify_test(test_file) == "critical"
