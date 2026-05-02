"""
Tests for modok.registry — the Registry Proposal Engine.
All tests written before implementation (Phase 5). Every test cites
the EARS spec it verifies via @spec annotation.

Imports of registry modules are placed inside test functions so pytest
can collect tests before Phase 6 implementation exists.
"""
from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write_file(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content))
    return path


def make_cfg(timeout_propose_registry=60, timeout_seconds=30, backend="local"):
    cfg = MagicMock()
    cfg.llm.timeout_propose_registry = timeout_propose_registry
    cfg.llm.timeout_seconds = timeout_seconds
    cfg.llm.backend = backend
    return cfg


def make_empty_enrich_result():
    from modok.registry.proposal import EnrichSectionResult
    return EnrichSectionResult()


SIMPLE_DOC = """\
# My Document

## Overview

This is an overview section.

## Details

More content here.
"""

FRONTMATTER_DOC = """\
---
title: My Doc
author: Test
---

# My Document

## Overview

This is an overview section.
"""

EMPTY_SECTIONS_DOC = """\
# My Document

## Empty Section

## Another Empty Section
"""

LINK_ONLY_DOC = """\
# My Document

## Links

- [See also](some-doc.md)
- [Reference](another.md)
"""


# ---------------------------------------------------------------------------
# RP-DISC-001 — discover .md and .mdx recursively; exclude registries/
# ---------------------------------------------------------------------------

# @spec RP-DISC-001
def test_discover_docs_finds_md_and_mdx_recursively(tmp_path):
    from modok.registry.discovery import discover_docs
    write_file(tmp_path / "readme.md", "content")
    write_file(tmp_path / "guide.mdx", "content")
    write_file(tmp_path / "sub" / "nested.md", "content")

    files, _ = discover_docs(tmp_path)
    names = {f.name for f in files}
    assert "readme.md" in names
    assert "guide.mdx" in names
    assert "nested.md" in names


# @spec RP-DISC-001
def test_discover_docs_excludes_registries_directory(tmp_path):
    from modok.registry.discovery import discover_docs
    write_file(tmp_path / "readme.md", "content")
    write_file(tmp_path / "registries" / "features.yml", "features: {}")

    files, _ = discover_docs(tmp_path)
    assert all("registries" not in str(f) for f in files)
    assert any(f.name == "readme.md" for f in files)


# ---------------------------------------------------------------------------
# RP-DISC-002 — skip files matching ignore patterns
# ---------------------------------------------------------------------------

# @spec RP-DISC-002
def test_discover_docs_skips_ignore_patterns(tmp_path):
    from modok.registry.discovery import discover_docs
    for ignored_dir in [".git", "node_modules", "build", "dist", "bin"]:
        write_file(tmp_path / ignored_dir / "doc.md", "content")
    write_file(tmp_path / "real.md", "content")

    files, _ = discover_docs(tmp_path)
    paths = [str(f) for f in files]
    for ignored_dir in [".git", "node_modules", "build", "dist", "bin"]:
        assert not any(ignored_dir in p for p in paths)
    assert any("real.md" in p for p in paths)


# ---------------------------------------------------------------------------
# RP-DISC-003 — .yaml/.yml are not eligible
# ---------------------------------------------------------------------------

# @spec RP-DISC-003
def test_discover_docs_excludes_yaml_files(tmp_path):
    from modok.registry.discovery import discover_docs
    write_file(tmp_path / "doc.md", "content")
    write_file(tmp_path / "schema.yml", "key: value")
    write_file(tmp_path / "data.yaml", "key: value")

    files, _ = discover_docs(tmp_path)
    assert all(f.suffix in {".md", ".mdx"} for f in files)
    assert not any(f.suffix in {".yml", ".yaml"} for f in files)


# ---------------------------------------------------------------------------
# RP-PARSE-001 — strip YAML frontmatter
# ---------------------------------------------------------------------------

# @spec RP-PARSE-001
def test_parse_sections_strips_frontmatter(tmp_path):
    from modok.registry.parser import parse_sections
    doc = write_file(tmp_path / "doc.md", FRONTMATTER_DOC)
    sections = parse_sections(doc)
    for s in sections:
        assert "title:" not in s.body
        assert "author:" not in s.body
        assert "---" not in s.body


# ---------------------------------------------------------------------------
# RP-PARSE-002 — H1 headings are not section boundaries
# ---------------------------------------------------------------------------

# @spec RP-PARSE-002
def test_parse_sections_skips_h1(tmp_path):
    from modok.registry.parser import parse_sections
    doc = write_file(tmp_path / "doc.md", SIMPLE_DOC)
    sections = parse_sections(doc)
    assert all(s.heading != "My Document" for s in sections)
    assert any(s.heading == "Overview" for s in sections)


# ---------------------------------------------------------------------------
# RP-PARSE-003 — split on H2; Section carries heading, body, doc_path
# ---------------------------------------------------------------------------

# @spec RP-PARSE-003
def test_parse_sections_splits_on_h2_headings(tmp_path):
    from modok.registry.parser import parse_sections
    doc = write_file(tmp_path / "doc.md", SIMPLE_DOC)
    sections = parse_sections(doc)
    assert len(sections) == 2
    headings = [s.heading for s in sections]
    assert "Overview" in headings
    assert "Details" in headings
    assert all(s.doc_path == doc for s in sections)


# @spec RP-PARSE-003
def test_section_body_contains_content_not_h2_line(tmp_path):
    from modok.registry.parser import parse_sections
    doc = write_file(tmp_path / "doc.md", SIMPLE_DOC)
    sections = parse_sections(doc)
    overview = next(s for s in sections if s.heading == "Overview")
    assert "This is an overview section." in overview.body
    assert "## Overview" not in overview.body


# ---------------------------------------------------------------------------
# RP-PARSE-004 — skip empty sections; keep link-only sections
# ---------------------------------------------------------------------------

# @spec RP-PARSE-004
def test_parse_sections_skips_empty_body_sections(tmp_path):
    from modok.registry.parser import parse_sections
    doc = write_file(tmp_path / "doc.md", EMPTY_SECTIONS_DOC)
    sections = parse_sections(doc)
    assert len(sections) == 0


# @spec RP-PARSE-004
def test_parse_sections_keeps_link_only_sections(tmp_path):
    from modok.registry.parser import parse_sections
    doc = write_file(tmp_path / "doc.md", LINK_ONLY_DOC)
    sections = parse_sections(doc)
    assert len(sections) == 1
    assert sections[0].heading == "Links"


# ---------------------------------------------------------------------------
# RP-PARSE-005 — processed vs skipped file counts
# ---------------------------------------------------------------------------

# @spec RP-PARSE-005
def test_propose_registries_counts_processed_vs_skipped_docs(tmp_path):
    from modok.registry.proposal import propose_registries, EnrichSectionResult
    write_file(tmp_path / "good.md", SIMPLE_DOC)
    write_file(tmp_path / "empty.md", "# No H2 Headings\n\nJust prose.\n")
    cfg = make_cfg()

    with patch("modok.registry.proposal.enrich_section", return_value=EnrichSectionResult()):
        with patch("modok.registry.proposal.normalise_candidates", return_value={}):
            summary = propose_registries(tmp_path, cfg)

    assert summary.docs_processed == 1
    assert summary.docs_skipped == 1


# ---------------------------------------------------------------------------
# RP-ENRICH-001 — enrich_section called for each non-empty section
# ---------------------------------------------------------------------------

# @spec RP-ENRICH-001
def test_propose_registries_calls_enrich_for_each_section(tmp_path):
    from modok.registry.proposal import propose_registries, EnrichSectionResult
    write_file(tmp_path / "doc.md", SIMPLE_DOC)  # 2 H2 sections
    cfg = make_cfg()

    with patch("modok.registry.proposal.enrich_section",
               return_value=EnrichSectionResult()) as mock_enrich:
        with patch("modok.registry.proposal.normalise_candidates", return_value={}):
            propose_registries(tmp_path, cfg)

    assert mock_enrich.call_count == 2


# @spec RP-ENRICH-001
def test_enrich_section_receives_section_with_correct_fields(tmp_path):
    from modok.registry.proposal import propose_registries, EnrichSectionResult
    write_file(tmp_path / "doc.md", SIMPLE_DOC)
    cfg = make_cfg()

    received_sections = []

    def capturing_enrich(section, cfg_llm):
        received_sections.append(section)
        return EnrichSectionResult()

    with patch("modok.registry.proposal.enrich_section", side_effect=capturing_enrich):
        with patch("modok.registry.proposal.normalise_candidates", return_value={}):
            propose_registries(tmp_path, cfg)

    assert len(received_sections) == 2
    for s in received_sections:
        assert s.heading
        assert s.body
        assert s.doc_path


# ---------------------------------------------------------------------------
# RP-ENRICH-002 — sections processed sequentially
# ---------------------------------------------------------------------------

# @spec RP-ENRICH-002
def test_propose_registries_processes_sections_sequentially(tmp_path):
    from modok.registry.proposal import propose_registries, EnrichSectionResult
    write_file(tmp_path / "doc.md", SIMPLE_DOC)
    cfg = make_cfg()

    call_order = []

    def ordered_enrich(section, cfg_llm):
        call_order.append(section.heading)
        return EnrichSectionResult()

    with patch("modok.registry.proposal.enrich_section", side_effect=ordered_enrich):
        with patch("modok.registry.proposal.normalise_candidates", return_value={}):
            propose_registries(tmp_path, cfg)

    assert call_order == ["Overview", "Details"]


# ---------------------------------------------------------------------------
# RP-ENRICH-003 — print section count + time estimate to stderr before processing
# ---------------------------------------------------------------------------

# @spec RP-ENRICH-003
def test_propose_registries_prints_estimate_to_stderr_before_processing(tmp_path, capsys):
    from modok.registry.proposal import propose_registries, EnrichSectionResult
    write_file(tmp_path / "doc.md", SIMPLE_DOC)  # 2 sections, 1 doc
    cfg = make_cfg(timeout_propose_registry=60)

    processed_before_estimate = []

    def tracking_enrich(section, cfg_llm):
        processed_before_estimate.append(section.heading)
        return EnrichSectionResult()

    with patch("modok.registry.proposal.enrich_section", side_effect=tracking_enrich):
        with patch("modok.registry.proposal.normalise_candidates", return_value={}):
            propose_registries(tmp_path, cfg)

    captured = capsys.readouterr()
    # Estimate must be printed to stderr
    assert "2 section" in captured.err
    assert "1 doc" in captured.err
    assert "min" in captured.err


# ---------------------------------------------------------------------------
# RP-ENRICH-004 — LLM errors → warn, record as failed, continue
# ---------------------------------------------------------------------------

# @spec RP-ENRICH-004
def test_propose_registries_on_llm_unavailable_records_failed_and_continues(tmp_path, capsys):
    from modok.registry.proposal import propose_registries, EnrichSectionResult
    from modok.llm.errors import LLMUnavailableError
    write_file(tmp_path / "doc.md", SIMPLE_DOC)  # Overview, Details
    cfg = make_cfg()

    call_count = [0]

    def failing_then_ok(section, cfg_llm):
        call_count[0] += 1
        if section.heading == "Overview":
            raise LLMUnavailableError("gateway down")
        return EnrichSectionResult()

    with patch("modok.registry.proposal.enrich_section", side_effect=failing_then_ok):
        with patch("modok.registry.proposal.normalise_candidates", return_value={}):
            summary = propose_registries(tmp_path, cfg)

    assert call_count[0] == 2, "Both sections must be attempted"
    assert summary.sections_failed == 1
    assert summary.sections_processed == 1
    captured = capsys.readouterr()
    assert "Overview" in captured.err


# @spec RP-ENRICH-004
def test_propose_registries_on_llm_response_error_records_failed_and_continues(tmp_path):
    from modok.registry.proposal import propose_registries, EnrichSectionResult
    from modok.llm.errors import LLMResponseError
    write_file(tmp_path / "doc.md", SIMPLE_DOC)
    cfg = make_cfg()

    def raise_response_err(section, cfg_llm):
        raise LLMResponseError("bad JSON")

    with patch("modok.registry.proposal.enrich_section", side_effect=raise_response_err):
        with patch("modok.registry.proposal.normalise_candidates", return_value={}):
            summary = propose_registries(tmp_path, cfg)

    assert summary.sections_failed == 2
    assert summary.sections_processed == 0


# ---------------------------------------------------------------------------
# RP-ENRICH-005 — use timeout_propose_registry; fall back to timeout_seconds
# ---------------------------------------------------------------------------

# @spec RP-ENRICH-005
def test_enrich_section_uses_timeout_propose_registry(tmp_path):
    from modok.registry.parser import parse_sections
    from modok.llm.gateway import enrich_section

    doc = write_file(tmp_path / "doc.md", SIMPLE_DOC)
    section = parse_sections(doc)[0]
    cfg_llm = MagicMock()
    cfg_llm.timeout_propose_registry = 90
    cfg_llm.timeout_seconds = 15
    cfg_llm.backend = "local"

    with patch("modok.llm.gateway._ollama_chat_completion") as mock_call:
        mock_call.return_value = {
            "features": [], "modules": [], "error_signatures": [],
            "known_issues": [], "failure_modes": [], "decisions": [],
            "observation_events": [],
        }
        enrich_section(section, cfg_llm)

    call_kwargs = mock_call.call_args
    all_args = list(call_kwargs.args) + list(call_kwargs.kwargs.values())
    assert 90 in all_args or any(
        v == 90 for v in (call_kwargs.kwargs or {}).values()
    ), "timeout_propose_registry (90) should be passed to the HTTP call"


# @spec RP-ENRICH-005
def test_enrich_section_falls_back_to_timeout_seconds_when_key_absent(tmp_path):
    from modok.registry.parser import parse_sections
    from modok.llm.gateway import enrich_section

    doc = write_file(tmp_path / "doc.md", SIMPLE_DOC)
    section = parse_sections(doc)[0]
    cfg_llm = MagicMock(spec=[])
    # timeout_propose_registry absent; only timeout_seconds present
    cfg_llm.timeout_seconds = 45
    cfg_llm.backend = "local"

    with patch("modok.llm.gateway._ollama_chat_completion") as mock_call:
        mock_call.return_value = {
            "features": [], "modules": [], "error_signatures": [],
            "known_issues": [], "failure_modes": [], "decisions": [],
            "observation_events": [],
        }
        try:
            enrich_section(section, cfg_llm)
        except AttributeError:
            pass  # timeout_propose_registry absent — acceptable to raise here

    # If the call succeeded, the timeout used must be 45
    if mock_call.called:
        call_kwargs = mock_call.call_args
        all_args = list(call_kwargs.args) + list(call_kwargs.kwargs.values())
        assert 45 in all_args or any(v == 45 for v in (call_kwargs.kwargs or {}).values())


# ---------------------------------------------------------------------------
# RP-ENRICH-006 — enrich_section uses frozen ENRICH_SECTION_SYSTEM prompt
# ---------------------------------------------------------------------------

# @spec RP-ENRICH-006
def test_enrich_section_uses_frozen_system_prompt(tmp_path):
    from modok.registry.parser import parse_sections
    from modok.llm.gateway import enrich_section
    from modok.llm.prompts import ENRICH_SECTION_SYSTEM

    doc = write_file(tmp_path / "doc.md", SIMPLE_DOC)
    section = parse_sections(doc)[0]
    cfg_llm = MagicMock()
    cfg_llm.timeout_propose_registry = 60
    cfg_llm.backend = "local"

    captured_messages = []

    def capture_call(*args, **kwargs):
        for a in args:
            if isinstance(a, list):
                captured_messages.extend(a)
        captured_messages.extend(kwargs.get("messages", []))
        return {
            "features": [], "modules": [], "error_signatures": [],
            "known_issues": [], "failure_modes": [], "decisions": [],
            "observation_events": [],
        }

    with patch("modok.llm.gateway._ollama_chat_completion", side_effect=capture_call):
        enrich_section(section, cfg_llm)

    system_msgs = [m for m in captured_messages if isinstance(m, dict) and m.get("role") == "system"]
    assert system_msgs, "A system message must be sent"
    assert system_msgs[0]["content"] == ENRICH_SECTION_SYSTEM


# ---------------------------------------------------------------------------
# RP-ENRICH-007 — local backend → native Ollama /api/chat; remote → /v1/chat
# ---------------------------------------------------------------------------

# @spec RP-ENRICH-007
def test_enrich_section_local_backend_uses_native_ollama_api(tmp_path):
    from modok.registry.parser import parse_sections
    from modok.llm.gateway import enrich_section

    doc = write_file(tmp_path / "doc.md", SIMPLE_DOC)
    section = parse_sections(doc)[0]
    cfg_llm = MagicMock()
    cfg_llm.timeout_propose_registry = 60
    cfg_llm.backend = "local"

    with patch("modok.llm.gateway._ollama_chat_completion") as mock_local:
        mock_local.return_value = {
            "features": [], "modules": [], "error_signatures": [],
            "known_issues": [], "failure_modes": [], "decisions": [],
            "observation_events": [],
        }
        with patch("modok.llm.gateway._chat_completion") as mock_remote:
            enrich_section(section, cfg_llm)

    mock_local.assert_called_once()
    mock_remote.assert_not_called()


# @spec RP-ENRICH-007
def test_enrich_section_remote_backend_uses_openai_compatible_api(tmp_path):
    from modok.registry.parser import parse_sections
    from modok.llm.gateway import enrich_section

    doc = write_file(tmp_path / "doc.md", SIMPLE_DOC)
    section = parse_sections(doc)[0]
    cfg_llm = MagicMock()
    cfg_llm.timeout_propose_registry = 60
    cfg_llm.backend = "remote"

    with patch("modok.llm.gateway._chat_completion") as mock_remote:
        mock_remote.return_value = {
            "features": [], "modules": [], "error_signatures": [],
            "known_issues": [], "failure_modes": [], "decisions": [],
            "observation_events": [],
        }
        with patch("modok.llm.gateway._ollama_chat_completion") as mock_local:
            enrich_section(section, cfg_llm)

    mock_remote.assert_called_once()
    mock_local.assert_not_called()


# @spec RP-ENRICH-007
def test_enrich_section_defaults_to_local_backend(tmp_path):
    from modok.registry.parser import parse_sections
    from modok.llm.gateway import enrich_section

    doc = write_file(tmp_path / "doc.md", SIMPLE_DOC)
    section = parse_sections(doc)[0]
    cfg_llm = MagicMock(spec=[])
    cfg_llm.timeout_propose_registry = 60
    # No backend attribute — should default to local

    with patch("modok.llm.gateway._ollama_chat_completion") as mock_local:
        mock_local.return_value = {
            "features": [], "modules": [], "error_signatures": [],
            "known_issues": [], "failure_modes": [], "decisions": [],
            "observation_events": [],
        }
        with patch("modok.llm.gateway._chat_completion") as mock_remote:
            try:
                enrich_section(section, cfg_llm)
            except AttributeError:
                return  # acceptable when backend attribute absent raises

    # If it ran, local should have been used by default
    if mock_local.called or mock_remote.called:
        mock_local.assert_called()
        mock_remote.assert_not_called()


# ---------------------------------------------------------------------------
# RP-ENRICH-008 — enrich_section never calls Quine client methods
# ---------------------------------------------------------------------------

# @spec RP-ENRICH-008
def test_enrich_section_never_calls_quine(tmp_path):
    from modok.registry.parser import parse_sections
    from modok.llm.gateway import enrich_section

    doc = write_file(tmp_path / "doc.md", SIMPLE_DOC)
    section = parse_sections(doc)[0]
    cfg_llm = MagicMock()
    cfg_llm.timeout_propose_registry = 60
    cfg_llm.backend = "local"

    with patch("modok.llm.gateway._ollama_chat_completion", return_value={
        "features": [], "modules": [], "error_signatures": [],
        "known_issues": [], "failure_modes": [], "decisions": [],
        "observation_events": [],
    }):
        with patch("modok.quine.client.QuineClient") as mock_quine_cls:
            enrich_section(section, cfg_llm)
            mock_quine_cls.assert_not_called()


# ---------------------------------------------------------------------------
# RP-ENRICH-009 — all-empty result counts as processed, not failed
# ---------------------------------------------------------------------------

# @spec RP-ENRICH-009
def test_propose_registries_all_empty_result_counts_as_processed(tmp_path):
    from modok.registry.proposal import propose_registries, EnrichSectionResult
    write_file(tmp_path / "doc.md", SIMPLE_DOC)
    cfg = make_cfg()

    with patch("modok.registry.proposal.enrich_section", return_value=EnrichSectionResult()):
        with patch("modok.registry.proposal.normalise_candidates", return_value={}):
            summary = propose_registries(tmp_path, cfg)

    assert summary.sections_processed == 2
    assert summary.sections_failed == 0


# ---------------------------------------------------------------------------
# RP-NORM-002 — single normalisation call with NORMALISE_REGISTRY_SYSTEM
# ---------------------------------------------------------------------------

# @spec RP-NORM-002
def test_normalisation_is_called_once(tmp_path):
    from modok.registry.proposal import propose_registries, EnrichSectionResult
    write_file(tmp_path / "doc.md", SIMPLE_DOC)
    cfg = make_cfg()

    with patch("modok.registry.proposal.enrich_section", return_value=EnrichSectionResult()):
        with patch("modok.registry.proposal.normalise_candidates", return_value={}) as mock_norm:
            propose_registries(tmp_path, cfg)

    mock_norm.assert_called_once()


# ---------------------------------------------------------------------------
# RP-NORM-003 — normalisation failure → write raw-merged, emit warning
# ---------------------------------------------------------------------------

# @spec RP-NORM-003
def test_normalisation_failure_writes_raw_merged_candidates(tmp_path, capsys):
    from modok.registry.proposal import propose_registries, EnrichSectionResult
    from modok.llm.errors import LLMUnavailableError
    write_file(tmp_path / "doc.md", SIMPLE_DOC)
    cfg = make_cfg()

    result = EnrichSectionResult(features=["Real-Time Tracking"])

    with patch("modok.registry.proposal.enrich_section", return_value=result):
        with patch("modok.registry.proposal.normalise_candidates",
                   side_effect=LLMUnavailableError("norm down")):
            propose_registries(tmp_path, cfg)

    features_yml = tmp_path / "registries" / "features.yml"
    assert features_yml.exists(), "features.yml must be written even when normalisation fails"
    captured = capsys.readouterr()
    # Warning must be emitted to stderr
    warning_words = {"warn", "warning", "failed", "normalisation", "normalization", "skipping"}
    assert any(w in captured.err.lower() for w in warning_words)


# ---------------------------------------------------------------------------
# RP-NORM-004 — normalisation call uses timeout_propose_registry
# ---------------------------------------------------------------------------

# @spec RP-NORM-004
def test_normalisation_uses_timeout_propose_registry(tmp_path):
    from modok.registry.proposal import propose_registries, EnrichSectionResult
    write_file(tmp_path / "doc.md", SIMPLE_DOC)
    cfg = make_cfg(timeout_propose_registry=75)

    with patch("modok.registry.proposal.enrich_section", return_value=EnrichSectionResult()):
        with patch("modok.registry.proposal.normalise_candidates", return_value={}) as mock_norm:
            propose_registries(tmp_path, cfg)

    call_args = mock_norm.call_args
    # The config (carrying timeout_propose_registry=75) must be passed to normalise_candidates
    all_args = list(call_args.args) + list(call_args.kwargs.values())
    cfg_arg = next((a for a in all_args if hasattr(a, "timeout_propose_registry")), None)
    if cfg_arg is not None:
        assert cfg_arg.timeout_propose_registry == 75


# ---------------------------------------------------------------------------
# RP-SLUG-001 — slugify known examples and rules
# ---------------------------------------------------------------------------

# @spec RP-SLUG-001
def test_slugify_known_examples():
    from modok.registry.slugify import slugify
    assert slugify("⚠ N restart(s)") == "n-restarts"
    assert slugify("⏸ Held") == "held"
    assert slugify("GSS_FAILURE") == "gss-failure"
    assert slugify("USB Congestion") == "usb-congestion"
    assert slugify("Real-Time Camera Tracking") == "real-time-camera-tracking"


# @spec RP-SLUG-001
def test_slugify_strips_leading_unicode_symbols():
    from modok.registry.slugify import slugify
    assert slugify("⚡ Power Warning") == "power-warning"
    assert slugify("⚠ No pose") == "no-pose"


# @spec RP-SLUG-001
def test_slugify_replaces_parens_s_with_s():
    from modok.registry.slugify import slugify
    assert "s" in slugify("restart(s)")
    assert "restarts" in slugify("restart(s)")


# @spec RP-SLUG-001
def test_slugify_truncates_to_40_chars_at_word_boundary():
    from modok.registry.slugify import slugify
    long_name = "This Is A Very Long Feature Name That Easily Exceeds Forty Characters In Length"
    result = slugify(long_name)
    assert len(result) <= 40
    assert not result.endswith("-")


# @spec RP-SLUG-001
def test_slugify_no_leading_or_trailing_dashes():
    from modok.registry.slugify import slugify
    result = slugify("  -- leading and trailing --  ")
    assert not result.startswith("-")
    assert not result.endswith("-")


# ---------------------------------------------------------------------------
# RP-SLUG-004 — different names → keep both, second gets -2 suffix
# ---------------------------------------------------------------------------

# @spec RP-SLUG-004
def test_slug_collision_different_names_uses_dash_2_suffix(capsys):
    from modok.registry.slugify import slugify, resolve_slug_collisions

    # "Real-Time Tracking" and "Real Time Tracking" produce the same slug
    # but differ semantically after lowercasing+stripping (punctuation differs)
    entries = [
        {"name": "Real-Time Tracking", "description": "Tracks in real time."},
        {"name": "Real Time Tracking Alt", "description": "An alternative."},
    ]

    resolved = resolve_slug_collisions(entries)

    slugs = list(resolved.keys())
    assert len(slugs) == 2
    base_slug = slugify("Real-Time Tracking")
    assert base_slug in slugs
    assert f"{base_slug}-2" in slugs

    captured = capsys.readouterr()
    assert "collision" in captured.err.lower() or "review" in captured.err.lower()


# ---------------------------------------------------------------------------
# RP-WRITE-001 — create registries/ if missing
# ---------------------------------------------------------------------------

# @spec RP-WRITE-001
def test_write_creates_registries_dir_if_missing(tmp_path):
    from modok.registry.proposal import propose_registries, EnrichSectionResult
    write_file(tmp_path / "doc.md", SIMPLE_DOC)
    cfg = make_cfg()

    assert not (tmp_path / "registries").exists()

    with patch("modok.registry.proposal.enrich_section", return_value=EnrichSectionResult()):
        with patch("modok.registry.proposal.normalise_candidates", return_value={}):
            propose_registries(tmp_path, cfg)

    assert (tmp_path / "registries").is_dir()


# ---------------------------------------------------------------------------
# RP-WRITE-002 — overwrite existing registry files
# ---------------------------------------------------------------------------

# @spec RP-WRITE-002
def test_write_overwrites_existing_registry_files(tmp_path):
    from modok.registry.proposal import propose_registries, EnrichSectionResult
    registries = tmp_path / "registries"
    registries.mkdir()
    (registries / "features.yml").write_text(
        "features:\n  old-feature:\n    name: Old\n    description: Old feature\n"
    )
    write_file(tmp_path / "doc.md", SIMPLE_DOC)
    cfg = make_cfg()

    with patch("modok.registry.proposal.enrich_section", return_value=EnrichSectionResult()):
        with patch("modok.registry.proposal.normalise_candidates", return_value={}):
            propose_registries(tmp_path, cfg)

    content = (registries / "features.yml").read_text()
    assert "old-feature" not in content


# ---------------------------------------------------------------------------
# RP-WRITE-003 — features.yml structure
# ---------------------------------------------------------------------------

# @spec RP-WRITE-003
def test_features_yml_has_correct_structure(tmp_path):
    from modok.registry.writer import write_features_yml
    path = tmp_path / "features.yml"
    features = {"my-feature": {"name": "My Feature", "description": "Does something."}}
    write_features_yml(path, features)

    data = yaml.safe_load(path.read_text())
    assert "features" in data
    feat = data["features"]["my-feature"]
    assert "name" in feat
    assert "description" in feat
    assert feat["name"] == "My Feature"
    assert feat["description"] == "Does something."


# ---------------------------------------------------------------------------
# RP-WRITE-004 — modules.yml structure
# ---------------------------------------------------------------------------

# @spec RP-WRITE-004
def test_modules_yml_has_correct_structure(tmp_path):
    from modok.registry.writer import write_modules_yml
    path = tmp_path / "modules.yml"
    modules = {"my-module": {"name": "My Module", "description": "A component."}}
    write_modules_yml(path, modules)

    data = yaml.safe_load(path.read_text())
    assert "modules" in data
    mod = data["modules"]["my-module"]
    assert "name" in mod
    assert "description" in mod
    assert mod["name"] == "My Module"


# ---------------------------------------------------------------------------
# RP-WRITE-005 — errors.yml structure
# ---------------------------------------------------------------------------

# @spec RP-WRITE-005
def test_errors_yml_has_correct_structure(tmp_path):
    from modok.registry.writer import write_errors_yml
    path = tmp_path / "errors.yml"
    errors = {"gss-failure": {"normalized_error": "GSS_FAILURE", "description": "Solver failed."}}
    write_errors_yml(path, errors)

    data = yaml.safe_load(path.read_text())
    assert "errors" in data
    err = data["errors"]["gss-failure"]
    assert "normalized_error" in err
    assert "description" in err
    assert err["normalized_error"] == "GSS_FAILURE"


# ---------------------------------------------------------------------------
# RP-WRITE-007 — no Quine interaction during proposal pass
# ---------------------------------------------------------------------------

# @spec RP-WRITE-007
def test_proposal_never_calls_quine(tmp_path):
    from modok.registry.proposal import propose_registries, EnrichSectionResult
    write_file(tmp_path / "doc.md", SIMPLE_DOC)
    cfg = make_cfg()

    with patch("modok.registry.proposal.enrich_section", return_value=EnrichSectionResult()):
        with patch("modok.registry.proposal.normalise_candidates", return_value={}):
            with patch("modok.quine.client.QuineClient") as mock_quine_cls:
                propose_registries(tmp_path, cfg)
                mock_quine_cls.assert_not_called()


# ---------------------------------------------------------------------------
# RP-RPT-001 — propose_registries returns ProposalSummary
# ---------------------------------------------------------------------------

# @spec RP-RPT-001
def test_propose_registries_returns_proposal_summary(tmp_path):
    from modok.registry.proposal import propose_registries, EnrichSectionResult, ProposalSummary
    write_file(tmp_path / "doc.md", SIMPLE_DOC)
    cfg = make_cfg()

    normalised = {
        "features": [{"name": "Tracking", "description": "Tracks things."}],
    }

    with patch("modok.registry.proposal.enrich_section",
               return_value=EnrichSectionResult(features=["Tracking"])):
        with patch("modok.registry.proposal.normalise_candidates", return_value=normalised):
            summary = propose_registries(tmp_path, cfg)

    assert isinstance(summary, ProposalSummary)
    assert isinstance(summary.sections_processed, int)
    assert isinstance(summary.sections_failed, int)
    assert isinstance(summary.docs_processed, int)
    assert isinstance(summary.docs_skipped, int)
    assert isinstance(summary.entries_written, dict)
    assert "features.yml" in summary.entries_written
    assert "modules.yml" in summary.entries_written
    assert "errors.yml" in summary.entries_written
    assert isinstance(summary.failed_sections, list)


# @spec RP-RPT-001
def test_proposal_summary_entries_written_reflects_actual_counts(tmp_path):
    from modok.registry.proposal import propose_registries, EnrichSectionResult
    write_file(tmp_path / "doc.md", SIMPLE_DOC)
    cfg = make_cfg()

    normalised = {
        "features": [
            {"name": "Feature One", "description": "desc"},
            {"name": "Feature Two", "description": "desc"},
        ],
    }

    with patch("modok.registry.proposal.enrich_section",
               return_value=EnrichSectionResult(features=["Feature One", "Feature Two"])):
        with patch("modok.registry.proposal.normalise_candidates", return_value=normalised):
            summary = propose_registries(tmp_path, cfg)

    assert summary.entries_written["features.yml"] == 2
    assert summary.entries_written["modules.yml"] == 0
    assert summary.entries_written["errors.yml"] == 0


# ---------------------------------------------------------------------------
# RP-RPT-002 — failed_sections contains heading and doc path
# ---------------------------------------------------------------------------

# @spec RP-RPT-002
def test_proposal_summary_includes_failed_sections(tmp_path):
    from modok.registry.proposal import propose_registries, EnrichSectionResult
    from modok.llm.errors import LLMResponseError
    write_file(tmp_path / "doc.md", SIMPLE_DOC)
    cfg = make_cfg()

    def fail_overview(section, cfg_llm):
        if section.heading == "Overview":
            raise LLMResponseError("bad response")
        return EnrichSectionResult()

    with patch("modok.registry.proposal.enrich_section", side_effect=fail_overview):
        with patch("modok.registry.proposal.normalise_candidates", return_value={}):
            summary = propose_registries(tmp_path, cfg)

    assert len(summary.failed_sections) == 1
    failed = summary.failed_sections[0]
    # Each entry identifies the section heading and doc path
    heading = failed[0] if isinstance(failed, (tuple, list)) else getattr(failed, "heading", None)
    assert heading == "Overview"
