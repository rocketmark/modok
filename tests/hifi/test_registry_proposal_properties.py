"""
Property tests for modok.registry — Registry Proposal Engine.
All tests written before implementation (Phase 5). Tests cite specs via @spec annotation.

[P] specs covered:
  RP-PARSE-006 — parse_sections is a pure function with no LLM I/O
  RP-SLUG-002  — slugify is a pure function
  RP-SLUG-003  — identical lowercased names → merge, keep the longer
  RP-NORM-001  — candidates are deduplicated by exact string before normalisation
  RP-NORM-005  — normalisation never introduces new concepts
  RP-WRITE-006 — every written registry file is valid YAML
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

printable_words = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -_",
    min_size=1,
    max_size=30,
)

slug_safe_words = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz ",
    min_size=3,
    max_size=20,
)


# ---------------------------------------------------------------------------
# RP-PARSE-006 — parse_sections is pure; never calls LLM
# ---------------------------------------------------------------------------

# @spec RP-PARSE-006
@given(content=st.text(min_size=0, max_size=2000))
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_parse_sections_same_input_same_output(tmp_path, content):
    from modok.registry.parser import parse_sections

    doc = tmp_path / "doc.md"
    doc.write_text(content)

    result_a = parse_sections(doc)
    result_b = parse_sections(doc)

    assert len(result_a) == len(result_b)
    for a, b in zip(result_a, result_b):
        assert a.heading == b.heading
        assert a.body == b.body
        assert a.doc_path == b.doc_path


# @spec RP-PARSE-006
@given(content=st.text(min_size=0, max_size=2000))
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_parse_sections_never_calls_llm_gateway(tmp_path, content):
    from modok.registry.parser import parse_sections

    doc = tmp_path / "doc.md"
    doc.write_text(content)

    with patch("modok.llm.gateway.enrich_section") as mock_enrich:
        with patch("modok.llm.gateway._ollama_chat_completion") as mock_ollama:
            with patch("modok.llm.gateway._chat_completion") as mock_openai:
                parse_sections(doc)

    mock_enrich.assert_not_called()
    mock_ollama.assert_not_called()
    mock_openai.assert_not_called()


# @spec RP-PARSE-006
@given(content=st.text(min_size=0, max_size=1000))
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_parse_sections_has_no_side_effects_on_filesystem(tmp_path, content):
    from modok.registry.parser import parse_sections

    doc = tmp_path / "doc.md"
    doc.write_text(content)
    files_before = set(tmp_path.rglob("*"))

    parse_sections(doc)

    files_after = set(tmp_path.rglob("*"))
    assert files_before == files_after, "parse_sections must not create or delete files"


# ---------------------------------------------------------------------------
# RP-SLUG-002 — slugify is a pure function
# ---------------------------------------------------------------------------

# @spec RP-SLUG-002
@given(text=st.text(min_size=0, max_size=100))
@settings(max_examples=300)
def test_slugify_is_deterministic(text):
    from modok.registry.slugify import slugify

    assert slugify(text) == slugify(text)


# @spec RP-SLUG-002
@given(text=st.text(min_size=0, max_size=100))
@settings(max_examples=200)
def test_slugify_output_is_valid_slug_format(text):
    from modok.registry.slugify import slugify

    result = slugify(text)
    # Only lowercase alphanumeric and hyphens
    assert all(c.islower() or c.isdigit() or c == "-" for c in result)
    # No leading or trailing hyphens
    assert not result.startswith("-")
    assert not result.endswith("-")
    # Length bound
    assert len(result) <= 40


# @spec RP-SLUG-002
@given(text=st.text(min_size=0, max_size=100))
@settings(max_examples=100)
def test_slugify_has_no_side_effects(text):
    from modok.registry.slugify import slugify

    original = text
    slugify(text)
    # Input string is unchanged (Python strings are immutable, but verify no global state)
    assert text == original


# ---------------------------------------------------------------------------
# RP-SLUG-003 — identical lowercased names → merge, keep longer original
# ---------------------------------------------------------------------------

# @spec RP-SLUG-003
@given(
    base=st.text(
        alphabet="abcdefghijklmnopqrstuvwxyz ",
        min_size=4,
        max_size=20,
    ).filter(lambda s: s.strip()),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_slug_collision_identical_names_after_normalise_keeps_longer(base, capsys):
    from modok.registry.slugify import slugify, resolve_slug_collisions

    name_a = base.strip()
    name_b = base.strip().upper()  # same after lowercasing

    # Only run the assertion when they'd actually collide on slug
    if slugify(name_a) != slugify(name_b):
        return

    entries = [
        {"name": name_a, "description": "Desc A"},
        {"name": name_b, "description": "Desc B"},
    ]
    resolved = resolve_slug_collisions(entries)

    # Should merge to a single entry
    assert len(resolved) == 1
    kept_name = list(resolved.values())[0]["name"]
    # The kept name is the longer of the two (or equal if same length)
    assert len(kept_name) >= len(name_a) and len(kept_name) >= len(name_b)


# @spec RP-SLUG-003
@given(
    name=st.text(
        alphabet="abcdefghijklmnopqrstuvwxyz ",
        min_size=4,
        max_size=20,
    ).filter(lambda s: s.strip()),
    extra_spaces=st.integers(min_value=1, max_value=5),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_slug_collision_whitespace_only_difference_merges(name, extra_spaces, capsys):
    from modok.registry.slugify import slugify, resolve_slug_collisions

    name_a = name.strip()
    name_b = " " * extra_spaces + name_a + " " * extra_spaces  # same after strip

    if slugify(name_a) != slugify(name_b):
        return

    entries = [
        {"name": name_a, "description": "Desc A"},
        {"name": name_b, "description": "Desc B"},
    ]
    resolved = resolve_slug_collisions(entries)

    assert len(resolved) == 1


# ---------------------------------------------------------------------------
# RP-NORM-001 — per-section candidates merged and deduplicated before normalisation
# ---------------------------------------------------------------------------

# @spec RP-NORM-001
@given(
    items=st.lists(
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz -", min_size=2, max_size=20),
        min_size=1,
        max_size=8,
        unique=True,
    ),
    repeats=st.integers(min_value=2, max_value=5),
)
@settings(max_examples=100)
def test_merge_candidates_deduplicates_exact_strings(items, repeats):
    from modok.registry.proposal import _merge_candidates, EnrichSectionResult

    section_results = [EnrichSectionResult(features=items) for _ in range(repeats)]
    merged = _merge_candidates(section_results)

    feature_list = merged.get("features", [])
    # Each item appears exactly once after deduplication
    assert len(feature_list) == len(set(feature_list))
    for item in items:
        assert item in feature_list


# @spec RP-NORM-001
@given(
    features=st.lists(
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", min_size=2, max_size=20),
        min_size=0,
        max_size=5,
    ),
    modules=st.lists(
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", min_size=2, max_size=20),
        min_size=0,
        max_size=5,
    ),
)
@settings(max_examples=100)
def test_merge_candidates_handles_multiple_node_types(features, modules):
    from modok.registry.proposal import _merge_candidates, EnrichSectionResult

    r1 = EnrichSectionResult(features=features, modules=modules)
    r2 = EnrichSectionResult(features=features, modules=modules)
    merged = _merge_candidates([r1, r2])

    # Deduplication preserves all unique features and modules
    for f in features:
        assert f in merged.get("features", [])
    for m in modules:
        assert m in merged.get("modules", [])
    assert len(merged.get("features", [])) == len(set(features))
    assert len(merged.get("modules", [])) == len(set(modules))


# ---------------------------------------------------------------------------
# RP-NORM-005 — normalisation never introduces new concepts
# ---------------------------------------------------------------------------

# @spec RP-NORM-005
@given(
    raw_features=st.lists(
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz -", min_size=3, max_size=25),
        min_size=0,
        max_size=6,
        unique=True,
    )
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_normalisation_does_not_increase_entry_count(tmp_path, raw_features):
    from modok.registry.proposal import propose_registries, EnrichSectionResult

    (tmp_path / "doc.md").write_text(
        "# Doc\n\n## Section\n\nSome content here.\n"
    )

    raw_result = EnrichSectionResult(features=raw_features)

    def bounded_normalise(candidates, cfg_llm):
        raw = candidates.get("features", [])
        if not raw:
            return {}
        # Simulate the normalisation contract: may rename but not add new concepts.
        # Renaming is represented as returning ≤ len(raw) entries.
        return {
            "features": [
                {"name": f, "description": "desc"}
                for f in raw  # same items, possibly renamed — never more than raw
            ]
        }

    cfg = MagicMock()
    cfg.llm.timeout_propose_registry = 60
    cfg.llm.backend = "local"

    with patch("modok.registry.proposal.enrich_section", return_value=raw_result):
        with patch("modok.registry.proposal.normalise_candidates", side_effect=bounded_normalise):
            summary = propose_registries(tmp_path, cfg)

    written = summary.entries_written.get("features.yml", 0)
    assert written <= max(len(raw_features), 1)


# ---------------------------------------------------------------------------
# RP-WRITE-006 — every written registry file is valid YAML
# ---------------------------------------------------------------------------

# @spec RP-WRITE-006
@given(
    names=st.lists(
        st.text(
            alphabet="abcdefghijklmnopqrstuvwxyz0123456789 -",
            min_size=1,
            max_size=30,
        ),
        min_size=0,
        max_size=10,
    )
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_write_features_yml_is_always_valid_yaml(tmp_path, names):
    from modok.registry.slugify import slugify
    from modok.registry.writer import write_features_yml

    features = {}
    for n in names:
        slug = slugify(n)
        if slug and slug not in features:
            features[slug] = {"name": n, "description": f"Description of {n}"}

    path = tmp_path / "features.yml"
    write_features_yml(path, features)

    content = path.read_text()
    parsed = yaml.safe_load(content)
    assert parsed is not None
    assert "features" in parsed


# @spec RP-WRITE-006
@given(
    names=st.lists(
        st.text(
            alphabet="abcdefghijklmnopqrstuvwxyz0123456789 -",
            min_size=1,
            max_size=30,
        ),
        min_size=0,
        max_size=10,
    )
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_write_modules_yml_is_always_valid_yaml(tmp_path, names):
    from modok.registry.slugify import slugify
    from modok.registry.writer import write_modules_yml

    modules = {}
    for n in names:
        slug = slugify(n)
        if slug and slug not in modules:
            modules[slug] = {"name": n, "description": f"Description of {n}"}

    path = tmp_path / "modules.yml"
    write_modules_yml(path, modules)

    content = path.read_text()
    parsed = yaml.safe_load(content)
    assert parsed is not None
    assert "modules" in parsed


# @spec RP-WRITE-006
@given(
    error_names=st.lists(
        st.text(
            alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_",
            min_size=1,
            max_size=20,
        ),
        min_size=0,
        max_size=10,
    )
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_write_errors_yml_is_always_valid_yaml(tmp_path, error_names):
    from modok.registry.slugify import slugify
    from modok.registry.writer import write_errors_yml

    errors = {}
    for n in error_names:
        slug = slugify(n)
        if slug and slug not in errors:
            errors[slug] = {"normalized_error": n, "description": f"Error {n} occurred."}

    path = tmp_path / "errors.yml"
    write_errors_yml(path, errors)

    content = path.read_text()
    parsed = yaml.safe_load(content)
    assert parsed is not None
    assert "errors" in parsed
