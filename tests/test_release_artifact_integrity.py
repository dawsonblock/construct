"""Release-artifact integrity gate (Phase 25 — split from repository integrity).

Where `test_repository_integrity.py` checks the source tree (no accidental
duplicates, version surfaces agree), this gate checks the release artifact:

- The release manifest can be generated and is deterministic.
- Every declared dev dependency appears in the dev lock file.
- Hypothesis is explicitly declared (not just transitively installed).
- All migrations have unique checksums (no two migrations share a hash).
- The manifest includes every required field.
- The lock files are non-empty and well-formed.

This gate fails closed: a missing dependency or malformed lock file means the
release artifact is not qualified, even if all tests pass.
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# Dependency declaration completeness
# --------------------------------------------------------------------------

def _pyproject_dev_deps() -> list[str]:
    """Parse dev dependencies from pyproject.toml [project.optional-dependencies]."""
    text = (ROOT / "pyproject.toml").read_text()
    match = re.search(r'\[project\.optional-dependencies\]\s*\ndev\s*=\s*\[(.*?)\]', text, re.DOTALL)
    assert match, "pyproject.toml has no [project.optional-dependencies] dev list"
    raw = match.group(1)
    # Extract package names (strip version pins and quotes).
    deps = re.findall(r'"([^"]+)"', raw)
    return deps


def _lock_packages(path: Path) -> set[str]:
    """Package names from a pip freeze lock file (strip version pins)."""
    if not path.exists():
        return set()
    packages = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("#") or not line:
            continue
        # "package==1.2.3" → "package"
        name = line.split("==")[0].lower()
        packages.add(name)
    return packages


def test_every_dev_dependency_appears_in_dev_lock():
    """Every package declared in pyproject.toml dev deps must appear in the dev
    lock file — a missing entry means the lock is stale."""
    dev_deps = _pyproject_dev_deps()
    locked = _lock_packages(ROOT / "requirements-dev.lock.txt")
    for dep in dev_deps:
        # Strip version pin: "pytest==8.4.2" → "pytest"
        name = dep.split("==")[0].lower()
        assert name in locked, (
            f"dev dependency {name!r} is declared in pyproject.toml but missing "
            f"from requirements-dev.lock.txt — run 'make lock'"
        )


def test_hypothesis_is_explicitly_declared():
    """Hypothesis must be declared in pyproject.toml dev deps, not just
    transitively installed. A release that relies on an undeclared dependency
    is not reproducible."""
    dev_deps = _pyproject_dev_deps()
    dep_names = [d.split("==")[0].lower() for d in dev_deps]
    assert "hypothesis" in dep_names, (
        "hypothesis is not declared in pyproject.toml dev dependencies — "
        "adversarial/property tests depend on it"
    )


def test_lock_files_are_non_empty_and_well_formed():
    """Both lock files must exist and contain at least one pinned package."""
    for lock_file in ["requirements.lock.txt", "requirements-dev.lock.txt"]:
        path = ROOT / lock_file
        assert path.exists(), f"{lock_file} does not exist"
        packages = _lock_packages(path)
        assert packages, f"{lock_file} contains no pinned packages"


# --------------------------------------------------------------------------
# Migration integrity
# --------------------------------------------------------------------------

def test_all_migrations_have_unique_checksums():
    """No two migration files may share a SHA-256 checksum — a collision means
    a migration was duplicated or a file was accidentally emptied."""
    migrations_dir = ROOT / "migrations"
    checksums: dict[str, str] = {}
    for f in sorted(migrations_dir.glob("*.sql")):
        checksum = hashlib.sha256(f.read_bytes()).hexdigest()
        assert checksum not in checksums, (
            f"migration {f.name} has the same checksum as {checksums[checksum]} — "
            f"duplicate or empty migration"
        )
        checksums[checksum] = f.name


def test_migration_count_is_positive():
    """There must be at least one migration — an empty migrations directory means
    the schema is not managed."""
    migrations = list((ROOT / "migrations").glob("*.sql"))
    assert len(migrations) > 0, "no migrations found"


# --------------------------------------------------------------------------
# Release manifest generation
# --------------------------------------------------------------------------

def test_release_manifest_can_be_generated():
    """The release manifest script runs and produces valid JSON with every
    required field."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import release_manifest
        manifest = release_manifest.generate_manifest()
    finally:
        sys.path.pop(0)

    required_fields = [
        "version", "git_commit", "git_branch", "generated_at",
        "migration_count", "migration_fingerprint", "code_fingerprint",
        "test_fingerprint", "schema_fingerprint", "dependencies", "migrations",
    ]
    for field in required_fields:
        assert field in manifest, f"manifest is missing required field: {field}"

    # Version must match the VERSION file.
    canonical = (ROOT / "VERSION").read_text().strip()
    assert manifest["version"] == canonical, (
        f"manifest version {manifest['version']!r} != VERSION {canonical!r}"
    )

    # Migration count must match the actual file count.
    actual_migrations = len(list((ROOT / "migrations").glob("*.sql")))
    assert manifest["migration_count"] == actual_migrations, (
        f"manifest migration_count {manifest['migration_count']} != "
        f"actual {actual_migrations}"
    )


def test_release_manifest_is_deterministic_for_code_and_migrations():
    """The code_fingerprint and migration_fingerprint are deterministic — same
    files → same hash. The generated_at and git_commit may vary."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import release_manifest
        m1 = release_manifest.generate_manifest()
        m2 = release_manifest.generate_manifest()
    finally:
        sys.path.pop(0)

    assert m1["code_fingerprint"] == m2["code_fingerprint"]
    assert m1["migration_fingerprint"] == m2["migration_fingerprint"]
    assert m1["test_fingerprint"] == m2["test_fingerprint"]


def test_manifest_dependencies_include_hypothesis():
    """The release manifest must record hypothesis as a dependency — a release
    that doesn't ship hypothesis cannot run the property tests."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import release_manifest
        manifest = release_manifest.generate_manifest()
    finally:
        sys.path.pop(0)

    deps = manifest["dependencies"]
    assert "hypothesis" in deps, "manifest dependencies missing hypothesis"
    assert deps["hypothesis"] != "not installed", (
        "hypothesis is declared but not installed — the release artifact is broken"
    )


# --------------------------------------------------------------------------
# Phase 28: Qualification report
# --------------------------------------------------------------------------

def test_qualification_report_can_be_generated():
    """The qualification report script runs and produces valid JSON with every
    required field."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import qualification_report
        report = qualification_report.generate_report(run_tests=False)
    finally:
        sys.path.pop(0)

    required_fields = [
        "version", "git_commit", "git_branch", "generated_at",
        "schema", "dependency_lock_hash", "manifest_sha",
        "test_suite", "adversarial", "crash_recovery",
        "external_effect", "release_integrity", "qualified",
    ]
    for field in required_fields:
        assert field in report, f"qualification report missing field: {field}"

    # Version must match VERSION file.
    canonical = (ROOT / "VERSION").read_text().strip()
    assert report["version"] == canonical

    # Schema version must match actual migration count.
    actual_migrations = len(list((ROOT / "migrations").glob("*.sql")))
    assert report["schema"]["migration_count"] == actual_migrations

    # When run_tests=False, test subsets should be marked as not run.
    assert report["test_suite"]["run"] is False
    assert report["qualified"] is False  # not qualified without running tests


def test_qualification_report_dependency_lock_hash_is_deterministic():
    """The dependency lock hash is deterministic — same files → same hash."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import qualification_report
        r1 = qualification_report.generate_report(run_tests=False)
        r2 = qualification_report.generate_report(run_tests=False)
    finally:
        sys.path.pop(0)

    assert r1["dependency_lock_hash"] == r2["dependency_lock_hash"]


def test_qualification_report_includes_manifest_sha():
    """The qualification report includes the manifest SHA when a manifest exists."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import qualification_report
        report = qualification_report.generate_report(run_tests=False)
    finally:
        sys.path.pop(0)

    # MANIFEST.rc3.json should exist from the release step.
    manifest_path = ROOT / "MANIFEST.rc3.json"
    if manifest_path.exists():
        assert report["manifest_sha"] is not None
        assert len(report["manifest_sha"]) == 64  # SHA-256 hex


# --------------------------------------------------------------------------
# Phase 30: Full qualification runner
# --------------------------------------------------------------------------

def test_full_qualification_runner_defines_all_categories():
    """The full qualification runner must define every required test category."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import full_qualification
        categories = set(full_qualification.TEST_CATEGORIES.keys())
    finally:
        sys.path.pop(0)

    required = {"unit", "integration", "approval", "external_effect", "release_integrity"}
    missing = required - categories
    assert not missing, f"full qualification runner missing categories: {missing}"


def test_full_qualification_categories_are_non_empty():
    """Every test category must list at least one test file — an empty category
    would silently pass without testing anything."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import full_qualification
        for category, files in full_qualification.TEST_CATEGORIES.items():
            assert files, f"test category {category!r} has no test files"
    finally:
        sys.path.pop(0)
