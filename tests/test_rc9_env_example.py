"""rc9 Phase 7: Verify .env.example documents all required environment variables.

The .env.example file is the canonical documentation of what environment
variables the system needs. This test ensures it doesn't drift from the
actual environment variables referenced in the codebase.
"""
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_env_example_documents_database_url():
    """rc9: .env.example must document DATABASE_URL."""
    content = (ROOT / ".env.example").read_text()
    assert "DATABASE_URL=" in content, "DATABASE_URL not documented in .env.example"


def test_env_example_documents_erp_url():
    """rc9: .env.example must document ERP_NEXT_URL."""
    content = (ROOT / ".env.example").read_text()
    assert "ERP_NEXT_URL=" in content, "ERP_NEXT_URL not documented in .env.example"


def test_env_example_documents_redis():
    """rc9: .env.example must document REDIS_URL."""
    content = (ROOT / ".env.example").read_text()
    assert "REDIS_URL=" in content, "REDIS_URL not documented in .env.example"


def test_env_example_documents_object_store():
    """rc9: .env.example must document object storage config."""
    content = (ROOT / ".env.example").read_text()
    assert "OBJECT_STORE_BACKEND=" in content, "OBJECT_STORE_BACKEND not documented"
    assert "OBJECT_STORE_ROOT=" in content, "OBJECT_STORE_ROOT not documented"


def test_env_example_documents_app_database_url():
    """rc9: .env.example must document APP_DATABASE_URL (RLS app role)."""
    content = (ROOT / ".env.example").read_text()
    assert "APP_DATABASE_URL=" in content, "APP_DATABASE_URL not documented"


def test_env_example_has_no_real_credentials():
    """rc9: .env.example must not contain real credentials.

    The file is a template with local-only defaults. No real API keys,
    secrets, or passwords should appear.
    """
    content = (ROOT / ".env.example").read_text()
    # Check that no line has a non-empty value for sensitive keys
    # (these should be empty or have local-only placeholders).
    sensitive_patterns = [
        r"ERP_NEXT_API_KEY=\S+",
        r"ERP_NEXT_API_SECRET=\S+",
        r"GMAIL_ACCESS_TOKEN=\S+",
        r"MICROSOFT_GRAPH_ACCESS_TOKEN=\S+",
    ]
    for pattern in sensitive_patterns:
        matches = re.findall(pattern, content)
        assert matches == [], (
            f".env.example contains a non-empty sensitive value: {matches}"
        )


def test_env_example_in_payload_manifest():
    """rc9: .env.example must be in the payload manifest."""
    import json
    manifest_path = ROOT / "PAYLOAD_MANIFEST.json"
    if not manifest_path.exists():
        import pytest
        pytest.skip("no payload manifest — run release_manifest.py")
    manifest = json.loads(manifest_path.read_text())
    assert ".env.example" in manifest.get("files", {}), (
        ".env.example not in payload manifest"
    )
