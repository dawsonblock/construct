"""v0.4.3 — approval UI security and CSP (item 21).

Verifies that:
1. The API sets security headers on every response (CSP, X-Content-Type-Options,
   X-Frame-Options, Referrer-Policy, Permissions-Policy).
2. HTML responses get a strict CSP that blocks inline scripts and external
   connections (script-src 'self', connect-src 'self').
3. API/JSON responses get default-src 'none'.
4. The approval HTML page does not use inline scripts, inline event handlers, or
   innerHTML — all dynamic content is rendered through textContent/createElement
   in the external JS file, preventing XSS from server-returned data.
"""
from __future__ import annotations

from pathlib import Path

import pytest

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover
    TestClient = None

WEB_DIR = Path(__file__).resolve().parents[1] / "apps" / "web"


def _client():
    if TestClient is None:
        pytest.skip("FastAPI TestClient not available")
    from apps.api.main import app

    return TestClient(app, raise_server_exceptions=False)


# --------------------------------------------------------------------------
# Security headers on every response
# --------------------------------------------------------------------------

def test_html_response_has_strict_csp():
    with _client() as c:
        r = c.get("/approval-ui")
    assert r.status_code == 200
    csp = r.headers.get("content-security-policy", "")
    # HTML pages must allow only self-sourced scripts, no inline.
    assert "script-src 'self'" in csp
    assert "'unsafe-inline'" not in csp
    assert "'unsafe-eval'" not in csp
    # connect-src must be same-origin only — no exfiltration to external hosts.
    assert "connect-src 'self'" in csp
    # frame-ancestors 'none' prevents clickjacking.
    assert "frame-ancestors 'none'" in csp


def test_json_response_has_default_none_csp():
    with _client() as c:
        r = c.get("/health")
    assert r.status_code == 200
    csp = r.headers.get("content-security-policy", "")
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp


def test_all_responses_have_hardening_headers():
    with _client() as c:
        r = c.get("/health")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"
    assert r.headers.get("referrer-policy") == "no-referrer"
    assert "geolocation=()" in r.headers.get("permissions-policy", "")


# --------------------------------------------------------------------------
# Approval UI has no inline scripts or handlers (required by the strict CSP)
# --------------------------------------------------------------------------

def test_approval_html_has_no_inline_script():
    html = (WEB_DIR / "approval.html").read_text()
    assert "<script>" not in html, "inline <script> blocks are blocked by the CSP"
    assert "onclick" not in html, "inline event handlers are blocked by the CSP"
    assert "onload" not in html
    assert "onerror" not in html
    assert "javascript:" not in html.lower()


def test_approval_html_references_external_script_and_style():
    html = (WEB_DIR / "approval.html").read_text()
    assert '<script src="/static/approval.js">' in html
    assert '<link rel="stylesheet" href="/static/approval.css">' in html


# --------------------------------------------------------------------------
# The external JS file never uses innerHTML (XSS prevention)
# --------------------------------------------------------------------------

def test_approval_js_never_uses_innerHTML():
    import re

    js = (WEB_DIR / "approval.js").read_text()
    # Match actual property access/assignment, not the word in comments.
    assert not re.search(r"\.innerHTML\s*=", js), (
        "innerHTML must not be assigned — all dynamic content must go through "
        "textContent / createElement to prevent XSS from server-returned data"
    )
    assert not re.search(r"\.insertAdjacentHTML\s*\(", js)
    assert "document.write" not in js
    assert "eval(" not in js
    assert "new Function(" not in js


def test_approval_js_uses_textContent_for_dynamic_content():
    js = (WEB_DIR / "approval.js").read_text()
    assert "textContent" in js, "dynamic content must be set via textContent"
    assert "createElement" in js, "DOM elements must be built via createElement"


# --------------------------------------------------------------------------
# Static assets are served from /static
# --------------------------------------------------------------------------

def test_static_js_is_served():
    with _client() as c:
        r = c.get("/static/approval.js")
    assert r.status_code == 200
    assert "javascript" in r.headers.get("content-type", "").lower() or "text" in r.headers.get("content-type", "").lower()


def test_static_css_is_served():
    with _client() as c:
        r = c.get("/static/approval.css")
    assert r.status_code == 200
    assert "css" in r.headers.get("content-type", "").lower() or "text" in r.headers.get("content-type", "").lower()
