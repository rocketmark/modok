"""
Tests for manifest detection and v1 (pypi) parsing
(docs/llds/dependency-graph-ingestion.md § Tracked-Manifest Detection,
§ File Format Parsing). Written before implementation (Phase 4) — the module
these tests target does not exist yet, so every test here fails with
ImportError until Phase 5.

Module name assumption: src/modok/ingestion/dependency_ingestion.py, mirroring
the existing src/modok/ingestion/ci_ingestion.py sibling module. Phase 5 may
rename; the behavioral requirements below (DEPG-PARSE-*, DEPG-DETECT-*) do not
depend on the exact module path.

Interface assumptions:
  - is_manifest_path(path: str, globs: list[str] | None = None) -> bool
  - manifest_ecosystem_for_path(path: str) -> str | None
  - is_parseable_ecosystem(ecosystem: str) -> bool
  - parse_manifest(path: str, content: str) -> dict[str, str] | None
      (None for a detected-only, not-yet-parsed ecosystem)
  - normalize_package_name(name: str) -> str
  - build_purl(ecosystem: str, name: str) -> str

Specs verified: DEPG-DETECT-001 through DEPG-DETECT-003,
DEPG-PARSE-001 through DEPG-PARSE-004.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# DEPG-DETECT-001 — static filename table
# ---------------------------------------------------------------------------


# @spec DEPG-DETECT-001
def test_requirements_txt_variants_detected_as_pypi_manifest():
    from modok.ingestion.dependency_ingestion import (
        is_manifest_path,
        manifest_ecosystem_for_path,
    )

    for path in ("client/requirements.txt", "requirements-dev.txt", "requirements.txt"):
        assert is_manifest_path(path)
        assert manifest_ecosystem_for_path(path) == "pypi"


# @spec DEPG-DETECT-001
def test_pyproject_toml_detected_as_pypi_manifest():
    from modok.ingestion.dependency_ingestion import (
        is_manifest_path,
        manifest_ecosystem_for_path,
    )

    assert is_manifest_path("pyproject.toml")
    assert manifest_ecosystem_for_path("pyproject.toml") == "pypi"


# @spec DEPG-DETECT-001
def test_non_manifest_source_file_not_detected():
    from modok.ingestion.dependency_ingestion import is_manifest_path

    assert not is_manifest_path("client/stagehand_client/stagehand_ble.py")


# @spec DEPG-DETECT-001
def test_npm_and_nuget_manifests_detected_with_correct_ecosystem():
    from modok.ingestion.dependency_ingestion import manifest_ecosystem_for_path

    assert manifest_ecosystem_for_path("package.json") == "npm"
    assert manifest_ecosystem_for_path("package-lock.json") == "npm"
    assert manifest_ecosystem_for_path("Directory.Packages.props") == "nuget"


# ---------------------------------------------------------------------------
# DEPG-DETECT-002 — "detected only" ecosystems recognized, not parsed
# ---------------------------------------------------------------------------


# @spec DEPG-DETECT-002
def test_pypi_and_pep621_are_parseable_but_others_are_not():
    from modok.ingestion.dependency_ingestion import is_parseable_ecosystem

    assert is_parseable_ecosystem("pypi")
    for ecosystem in ("npm", "nuget", "rubygems", "go", "cargo"):
        assert not is_parseable_ecosystem(ecosystem)


# @spec DEPG-DETECT-002
def test_parse_manifest_returns_none_for_detected_only_ecosystem():
    from modok.ingestion.dependency_ingestion import parse_manifest

    assert parse_manifest("package.json", '{"dependencies": {"left-pad": "1.0.0"}}') is None


# ---------------------------------------------------------------------------
# DEPG-DETECT-003 — optional dependency_manifest_globs narrowing
# ---------------------------------------------------------------------------


# @spec DEPG-DETECT-003
def test_globs_unset_tracks_every_detected_manifest_path():
    from modok.ingestion.dependency_ingestion import is_manifest_path

    assert is_manifest_path("client/requirements.txt", globs=None)
    assert is_manifest_path("vendor/third_party/requirements.txt", globs=None)


# @spec DEPG-DETECT-003
def test_globs_set_narrows_to_matching_paths_only():
    from modok.ingestion.dependency_ingestion import is_manifest_path

    globs = ["client/**"]
    assert is_manifest_path("client/requirements.txt", globs=globs)
    assert not is_manifest_path("vendor/third_party/requirements.txt", globs=globs)


# ---------------------------------------------------------------------------
# DEPG-PARSE-001 — requirements.txt line parsing
# ---------------------------------------------------------------------------


# @spec DEPG-PARSE-001
def test_parses_simple_pinned_and_ranged_requirements_lines():
    from modok.ingestion.dependency_ingestion import parse_manifest

    content = "bleak>=0.22.0\nnumpy==1.24.0\n"
    result = parse_manifest("client/requirements.txt", content)
    assert result == {"bleak": ">=0.22.0", "numpy": "==1.24.0"}


# @spec DEPG-PARSE-001
def test_requirements_txt_comments_blanks_and_directives_are_skipped():
    from modok.ingestion.dependency_ingestion import parse_manifest

    content = (
        "# top-level comment\n"
        "\n"
        "-r requirements-base.txt\n"
        "-e .\n"
        "--index-url https://pypi.org/simple\n"
        "bleak>=0.22.0\n"
    )
    result = parse_manifest("client/requirements.txt", content)
    assert result == {"bleak": ">=0.22.0"}


# @spec DEPG-PARSE-001
def test_requirements_txt_records_full_comparator_string_not_just_number():
    from modok.ingestion.dependency_ingestion import parse_manifest

    result = parse_manifest("requirements.txt", "cbor2>=5.4.6,<6\n")
    assert result["cbor2"] == ">=5.4.6,<6"


# ---------------------------------------------------------------------------
# DEPG-PARSE-002 — pyproject.toml PEP 621 dependencies
# ---------------------------------------------------------------------------


# @spec DEPG-PARSE-002
def test_pyproject_toml_parses_pep621_dependencies_array():
    from modok.ingestion.dependency_ingestion import parse_manifest

    content = (
        "[project]\n"
        'name = "example"\n'
        'dependencies = [\n'
        '  "bleak>=0.22.0",\n'
        '  "numpy==1.24.0",\n'
        "]\n"
    )
    result = parse_manifest("pyproject.toml", content)
    assert result == {"bleak": ">=0.22.0", "numpy": "==1.24.0"}


# @spec DEPG-PARSE-002
def test_pyproject_toml_poetry_dependencies_table_recognized_not_parsed():
    from modok.ingestion.dependency_ingestion import parse_manifest

    content = (
        "[tool.poetry.dependencies]\n"
        'python = "^3.11"\n'
        'bleak = ">=0.22.0"\n'
    )
    # Recognized as a tracked manifest (is_manifest_path is True elsewhere),
    # but the poetry-table shape is not parsed in v1 — no dependencies extracted.
    result = parse_manifest("pyproject.toml", content)
    assert not result


# ---------------------------------------------------------------------------
# DEPG-PARSE-003 — malformed line tolerance
# ---------------------------------------------------------------------------


# @spec DEPG-PARSE-003
def test_malformed_line_skipped_rest_of_file_still_parses():
    from modok.ingestion.dependency_ingestion import parse_manifest

    content = "bleak>=0.22.0\n!!!not a valid requirement line!!!\nnumpy==1.24.0\n"
    result = parse_manifest("requirements.txt", content)
    assert result == {"bleak": ">=0.22.0", "numpy": "==1.24.0"}


# ---------------------------------------------------------------------------
# DEPG-PARSE-004 — PEP 503 name normalization
# ---------------------------------------------------------------------------


# @spec DEPG-PARSE-004
def test_normalize_package_name_collapses_separators_and_case():
    from modok.ingestion.dependency_ingestion import normalize_package_name

    assert normalize_package_name("PySide6") == normalize_package_name("pyside6")
    # PEP 503: runs of -/_/. collapse to a single "-", so "PySide-6" and
    # "PySide_6" are the same normalized name — but "PySide-6" and "PySide6"
    # are genuinely different names (the hyphen is real content, not noise).
    assert normalize_package_name("PySide-6") == normalize_package_name("PySide_6")
    assert normalize_package_name("Foo_Bar.Baz") == normalize_package_name("foo-bar-baz")


# @spec DEPG-PARSE-004
def test_build_purl_uses_normalized_name():
    from modok.ingestion.dependency_ingestion import build_purl

    assert build_purl("pypi", "Bleak") == "pkg:pypi/bleak"
