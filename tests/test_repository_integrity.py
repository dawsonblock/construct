"""Repository integrity — the release tree stays clean and self-consistent.

Phase 1 of the v0.4 hardening program introduced this gate. It rejects the
accidental-duplicate patterns that snuck into the v0.3 archive
(`graph_view 2.py`, `__init__ 2.py`) and asserts the version identifiers that the
release manifest, Docker labels, API and package metadata all derive from agree
with one another.

It walks `git ls-files` so it inspects exactly what a release would ship — no
untracked scratch files, no ignored caches.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Suffixes that indicate an accidental duplicate / scratch copy rather than a
# deliberate source file. macOS Finder produces "<name> 2.<ext>"; editors and
# shells produce the rest. A release must contain none of these.
ACCIDENTAL_DUPLICATE_PATTERNS = [
    re.compile(r" 2\.[A-Za-z0-9]+$"),      # "graph_view 2.py"
    re.compile(r" - copy\.[A-Za-z0-9]+$"),  # "main - copy.py"
    re.compile(r"\(copy\)\.[A-Za-z0-9]+$"),  # "main(copy).py"
    re.compile(r"_backup\.[A-Za-z0-9]+$"),  # "models_backup.py"
    re.compile(r"_old\.[A-Za-z0-9]+$"),     # "models_old.py"
    re.compile(r"_copy\.[A-Za-z0-9]+$"),    # "models_copy.py"
]


def _tracked_files() -> list[str]:
    """Files git would ship. Fails closed if git is unavailable."""
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def test_no_accidental_duplicate_suffixes_in_tracked_files():
    tracked = _tracked_files()
    offenders: list[str] = []
    for path in tracked:
        name = Path(path).name
        if any(pattern.search(name) for pattern in ACCIDENTAL_DUPLICATE_PATTERNS):
            offenders.append(path)
    assert not offenders, (
        "accidental duplicate / scratch files must not be tracked; remove or "
        f"explicitly document: {offenders}"
    )


def test_no_tracked_file_has_a_space_before_its_extension():
    """The macOS 'foo 2.py' family always carries a space before the extension."""
    offenders = [p for p in _tracked_files() if re.search(r" .+\.[A-Za-z0-9]+$", Path(p).name)]
    assert not offenders, f"tracked files with a space in the name are suspect: {offenders}"


def test_the_stale_manifest_is_not_misnamed_as_the_authoritative_one():
    """The broken v0.3 manifest must stay preserved as STALE, never restored to the live path."""
    assert not (ROOT / "MANIFEST.sha256.json").exists(), (
        "MANIFEST.sha256.json must not return as the authoritative manifest; the "
        "v0.3 manifest is preserved as MANIFEST.v0.3.STALE.json and a new one is "
        "generated per release by qualification."
    )
    assert (ROOT / "MANIFEST.v0.3.STALE.json").exists(), (
        "MANIFEST.v0.3.STALE.json must remain as historical evidence of the broken baseline"
    )


# --------------------------------------------------------------------------
# Version consistency — every place that carries the version must agree.
# --------------------------------------------------------------------------

CANONICAL_VERSION = (ROOT / "VERSION").read_text().strip()


def _pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text()
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "pyproject.toml has no [project] version"
    return match.group(1)


def test_version_file_matches_pyproject():
    assert _pyproject_version() == CANONICAL_VERSION


def test_version_file_matches_package_dunder_version():
    from construction_ai import __version__

    assert __version__ == CANONICAL_VERSION


def test_version_file_matches_erpnext_stub_app():
    text = (ROOT / "apps" / "erpnext_stub" / "main.py").read_text()
    match = re.search(r'version="([^"]+)"', text)
    assert match, "erpnext stub has no FastAPI(version=...)"
    assert match.group(1) == CANONICAL_VERSION, (
        f"erpnext stub version {match.group(1)!r} != VERSION {CANONICAL_VERSION!r}"
    )


def test_dockerfile_label_defaults_match_version():
    text = (ROOT / "Dockerfile").read_text()
    match = re.search(r"ARG APP_VERSION=([^\s]+)", text)
    assert match, "Dockerfile has no ARG APP_VERSION default"
    assert match.group(1) == CANONICAL_VERSION, (
        f"Dockerfile APP_VERSION default {match.group(1)!r} != VERSION {CANONICAL_VERSION!r}"
    )
