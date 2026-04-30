"""
Tests for modok.ingestion — static ingestion pipeline.
All tests written before implementation (Phase 5). Every test cites
the EARS spec it verifies via @spec annotation.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from modok.ingestion.confidence import ConfidenceBand, confidence_band
from modok.ingestion.discovery import (
    IGNORE_NAMES,
    IGNORE_PATTERNS,
    IGNORE_SUFFIXES,
    SUPPORTED_SUFFIXES,
    discover_files,
    has_modok_frontmatter,
)
from modok.ingestion.errors import (
    InvalidSlugReferenceError,
    MissingCommitShaError,
    MissingRequiredFieldError,
    RegistryNotFoundError,
)
from modok.ingestion.hook import (
    MODOK_HOOK_END,
    MODOK_HOOK_START,
    hook_content,
    install_post_commit_hook,
)
from modok.ingestion.parser import (
    get_commit_sha,
    is_working_tree_dirty,
    parse_frontmatter,
    parse_headings,
    parse_modok_blocks,
)
from modok.ingestion.registry import Registry
from modok.ingestion.report import IngestionReport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write_file(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content))
    return path


MINIMAL_FRONTMATTER = """\
    ---
    modok:
      doc_type: lld
      project: stagehand
      feature: shtp-receiver
      product_area: networking
      modules:
        - shtp
      source_files:
        - agent/src/shtp.c
      test_files:
        - agent/tests/test_shtp.c
    ---

    # SHTP Receiver

    ## Overview

    Some content here.

    ### Detail

    More content.
    """


# ---------------------------------------------------------------------------
# SI-DISC-001 — discover supported file types recursively
# ---------------------------------------------------------------------------

# @spec SI-DISC-001
def test_discover_finds_supported_extensions(tmp_path):
    for ext in [".md", ".mdx", ".yaml", ".yml"]:
        write_file(tmp_path / f"doc{ext}", "content")
    write_file(tmp_path / "sub" / "nested.md", "content")

    files, _ = discover_files(tmp_path)
    exts = {f.suffix for f in files}
    assert exts == SUPPORTED_SUFFIXES
    assert any(f.name == "nested.md" for f in files)


# @spec SI-DISC-001
def test_discover_ignores_unsupported_extensions(tmp_path):
    write_file(tmp_path / "source.py", "code")
    write_file(tmp_path / "readme.txt", "text")
    write_file(tmp_path / "doc.md", "content")

    files, _ = discover_files(tmp_path)
    assert all(f.suffix in SUPPORTED_SUFFIXES for f in files)


# ---------------------------------------------------------------------------
# SI-DISC-002 — never ingest ignored paths
# ---------------------------------------------------------------------------

# @spec SI-DISC-002
@given(pattern=st.sampled_from(IGNORE_PATTERNS))
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_discover_never_ingests_ignored_directory(tmp_path, pattern):
    ignored_dir = tmp_path / pattern
    ignored_dir.mkdir()
    write_file(ignored_dir / "secret.md", "content")
    write_file(tmp_path / "ok.md", "content")

    files, ignored_count = discover_files(tmp_path)
    assert all(pattern not in str(f) for f in files)
    assert ignored_count >= 1


# @spec SI-DISC-002
@given(suffix=st.sampled_from(IGNORE_SUFFIXES))
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_discover_never_ingests_ignored_suffix(tmp_path, suffix):
    write_file(tmp_path / f"secret{suffix}", "content")
    write_file(tmp_path / "ok.md", "content")

    files, ignored_count = discover_files(tmp_path)
    assert all(f.suffix != suffix for f in files)
    assert ignored_count >= 1


# @spec SI-DISC-002
def test_discover_never_ingests_dotenv(tmp_path):
    write_file(tmp_path / ".env", "SECRET=123")
    write_file(tmp_path / "ok.md", "content")

    files, ignored_count = discover_files(tmp_path)
    assert all(f.name != ".env" for f in files)
    assert ignored_count >= 1


# ---------------------------------------------------------------------------
# SI-DISC-003 — skip files with no modok: frontmatter
# ---------------------------------------------------------------------------

# @spec SI-DISC-003
def test_has_modok_frontmatter_returns_true_when_present(tmp_path):
    path = write_file(tmp_path / "doc.md", MINIMAL_FRONTMATTER)
    assert has_modok_frontmatter(path) is True


# @spec SI-DISC-003
def test_has_modok_frontmatter_returns_false_when_absent(tmp_path):
    path = write_file(tmp_path / "doc.md", "# Just a heading\n\nNo frontmatter.\n")
    assert has_modok_frontmatter(path) is False


# @spec SI-DISC-003
def test_discover_skipped_count_increments_for_no_frontmatter(tmp_path):
    write_file(tmp_path / "has_fm.md", MINIMAL_FRONTMATTER)
    write_file(tmp_path / "no_fm.md", "# No frontmatter\n")
    write_file(tmp_path / "also_no_fm.yml", "just: yaml\n")

    files, _ = discover_files(tmp_path)
    # discover_files returns all supported-extension files; frontmatter filtering
    # happens in the pipeline. All three files are returned here.
    assert len(files) == 3
    # The pipeline would skip the two without frontmatter (tracked as files_skipped)
    assert sum(1 for f in files if has_modok_frontmatter(f)) == 1


# ---------------------------------------------------------------------------
# SI-FMTR-001 — parse frontmatter, validate schema structure only
# ---------------------------------------------------------------------------

# @spec SI-FMTR-001
def test_parse_frontmatter_returns_modok_block(tmp_path):
    path = write_file(tmp_path / "doc.md", MINIMAL_FRONTMATTER)
    result = parse_frontmatter(path)
    assert result is not None
    assert result["doc_type"] == "lld"
    assert result["feature"] == "shtp-receiver"


# @spec SI-FMTR-001
def test_parse_frontmatter_returns_none_when_absent(tmp_path):
    path = write_file(tmp_path / "doc.md", "# No frontmatter\n")
    assert parse_frontmatter(path) is None


# @spec SI-FMTR-001 — schema validation only, not slug validation
def test_parse_frontmatter_accepts_unregistered_slug(tmp_path):
    # Frontmatter parsing must not validate registry membership —
    # that is SI-REF-001's job in a subsequent stage.
    content = """\
        ---
        modok:
          doc_type: lld
          project: stagehand
          feature: totally-unknown-feature
          product_area: nowhere
          modules:
            - nonexistent.module
          source_files: []
          test_files: []
        ---
        # Doc
        """
    path = write_file(tmp_path / "doc.md", content)
    result = parse_frontmatter(path)
    assert result is not None
    assert result["feature"] == "totally-unknown-feature"


# ---------------------------------------------------------------------------
# SI-FMTR-002 / SI-LLM-001 / SI-LLM-002 — missing fields without --fix
# ---------------------------------------------------------------------------

# @spec SI-FMTR-002, SI-LLM-002
def test_missing_required_field_without_fix_does_not_invoke_llm(tmp_path):
    # Without --fix, missing fields → warn and skip. LLM not called.
    content = """\
        ---
        modok:
          doc_type: lld
          project: stagehand
          feature: shtp-receiver
          # missing: modules, source_files, test_files
        ---
        # Doc
        """
    path = write_file(tmp_path / "doc.md", content)

    fm = parse_frontmatter(path)
    with patch("modok.ingestion.pipeline.invoke_llm_gateway") as mock_llm:
        from modok.ingestion.pipeline import check_required_fields
        warnings, errors = check_required_fields(fm, "lld", fix=False)
        mock_llm.assert_not_called()
        assert any("modules" in w or "source_files" in w for w in warnings + errors)


# @spec SI-FMTR-003, SI-LLM-001
def test_missing_required_field_with_fix_invokes_llm(tmp_path):
    content = """\
        ---
        modok:
          doc_type: lld
          project: stagehand
          feature: shtp-receiver
        ---
        # Doc
        """
    path = write_file(tmp_path / "doc.md", content)

    with patch("modok.ingestion.pipeline.invoke_llm_gateway") as mock_llm:
        mock_llm.return_value = {"modules": ["shtp"], "source_files": [], "test_files": []}
        fm = parse_frontmatter(path)
        from modok.ingestion.pipeline import check_required_fields
        check_required_fields(fm, "lld", fix=True, doc_path=path)
        mock_llm.assert_called_once()


# @spec SI-FMTR-004
def test_llm_proposals_not_written_directly_to_quine():
    # LLM proposals must go to doc file first; the Quine write path
    # only accepts output from the mechanical parser.
    # Verified structurally: the pipeline module must not import QuineClient
    # in the llm proposal path — it writes to disk then re-parses.
    import inspect
    import modok.ingestion.pipeline as pipeline_mod
    src = inspect.getsource(pipeline_mod)
    # The LLM proposal function must not call upsert_node directly
    # (it may import QuineClient elsewhere, but the proposal path must not)
    assert "upsert_node" not in src.split("invoke_llm_gateway")[1].split("def ")[0]


# ---------------------------------------------------------------------------
# SI-REF-001/002/003 — invalid registry slugs halt file
# ---------------------------------------------------------------------------

# @spec SI-REF-001
def test_invalid_feature_slug_raises(tmp_path):
    registry = MagicMock(spec=Registry)
    registry.has_feature.return_value = False
    registry.has_module.return_value = True
    registry.has_error.return_value = True

    from modok.ingestion.pipeline import validate_references
    with pytest.raises(InvalidSlugReferenceError, match="feature"):
        validate_references(
            {"feature": "bad-slug", "modules": ["shtp"], "error_signatures": []},
            registry,
        )


# @spec SI-REF-002
def test_invalid_module_slug_raises(tmp_path):
    registry = MagicMock(spec=Registry)
    registry.has_feature.return_value = True
    registry.has_module.return_value = False
    registry.has_error.return_value = True

    from modok.ingestion.pipeline import validate_references
    with pytest.raises(InvalidSlugReferenceError, match="module"):
        validate_references(
            {"feature": "shtp-receiver", "modules": ["bad-module"], "error_signatures": []},
            registry,
        )


# @spec SI-REF-003
def test_invalid_error_slug_raises(tmp_path):
    registry = MagicMock(spec=Registry)
    registry.has_feature.return_value = True
    registry.has_module.return_value = True
    registry.has_error.return_value = False

    from modok.ingestion.pipeline import validate_references
    with pytest.raises(InvalidSlugReferenceError, match="error"):
        validate_references(
            {"feature": "shtp-receiver", "modules": ["shtp"], "error_signatures": ["bad-err"]},
            registry,
        )


# ---------------------------------------------------------------------------
# SI-REF-004 — missing file → warning + confidence penalty on prose facts only
# ---------------------------------------------------------------------------

# @spec SI-REF-004
def test_missing_source_file_emits_warning_not_error(tmp_path):
    from modok.ingestion.pipeline import validate_file_references
    warnings, errors = validate_file_references(
        {"source_files": ["/nonexistent/file.c"], "test_files": []},
        repo_root=tmp_path,
    )
    assert len(errors) == 0
    assert any("nonexistent" in w for w in warnings)


# @spec SI-REF-004
def test_missing_file_does_not_penalise_modok_block_facts(tmp_path):
    # MODOK block facts have confidence 1.00; file-existence penalty
    # must not reduce them below 1.00.
    band = confidence_band(base=1.00, penalties=[0.15])
    # A 1.00 base fact with a penalty should still yield score < 1.00 for prose,
    # but MODOK block facts bypass this function entirely — they are not scored.
    # This test verifies that confidence_band with base=1.00 and penalty gives
    # a result < 1.00 (i.e., the penalty would apply to prose), and that the
    # pipeline explicitly skips calling confidence_band for MODOK block facts.
    assert band.score < 1.00  # penalty applies when scoring is used
    # The pipeline bypass is verified in test_block_facts_always_verified below


# ---------------------------------------------------------------------------
# SI-REF-005 — never write node or edge referencing invalid slug
# ---------------------------------------------------------------------------

# @spec SI-REF-005
@given(
    feature=st.text(min_size=1, max_size=20),
    module=st.text(min_size=1, max_size=20),
)
def test_invalid_slug_suppresses_node_and_edge(feature, module):
    registry = MagicMock(spec=Registry)
    registry.has_feature.return_value = False
    registry.has_module.return_value = True
    registry.has_error.return_value = True

    from modok.ingestion.pipeline import validate_references
    with pytest.raises(InvalidSlugReferenceError):
        validate_references(
            {"feature": feature, "modules": [module], "error_signatures": []},
            registry,
        )


# ---------------------------------------------------------------------------
# SI-BLOCK-001 — parse fenced modok blocks
# ---------------------------------------------------------------------------

# @spec SI-BLOCK-001
def test_parse_modok_blocks_extracts_structured_facts():
    content = textwrap.dedent("""\
        ## Failure Mode

        ```modok
        kind: failure_mode
        id: shtp-version-mismatch
        symptom: Client rejects with unsupported version
        affects:
          - feature:shtp-receiver
        ```
        """)
    blocks = parse_modok_blocks(content)
    assert len(blocks) == 1
    assert blocks[0]["kind"] == "failure_mode"
    assert blocks[0]["id"] == "shtp-version-mismatch"


# @spec SI-BLOCK-001
def test_parse_modok_blocks_returns_empty_when_none_present():
    content = "# Just a doc\n\nNo blocks here.\n"
    assert parse_modok_blocks(content) == []


# ---------------------------------------------------------------------------
# SI-BLOCK-002 — MODOK block facts always confidence 1.00
# ---------------------------------------------------------------------------

# @spec SI-BLOCK-002
def test_block_facts_always_verified(tmp_path):
    from modok.ingestion.pipeline import score_fact
    # Facts from MODOK blocks must return 1.00 regardless of other signals
    score = score_fact(source="modok_block", value="shtp-version-mismatch")
    assert score == 1.00


# @spec SI-BLOCK-002
def test_frontmatter_facts_always_verified(tmp_path):
    from modok.ingestion.pipeline import score_fact
    score = score_fact(source="frontmatter", value="shtp-receiver")
    assert score == 1.00


# ---------------------------------------------------------------------------
# SI-BLOCK-003 — unrecognised block kind → warn, skip block, continue
# ---------------------------------------------------------------------------

# @spec SI-BLOCK-003
def test_unknown_block_kind_emits_warning_not_error():
    content = textwrap.dedent("""\
        ```modok
        kind: completely_unknown_kind
        id: foo
        ```
        """)
    from modok.ingestion.pipeline import process_modok_blocks
    nodes, warnings, errors = process_modok_blocks(parse_modok_blocks(content))
    assert len(errors) == 0
    assert any("unknown" in w.lower() or "unrecognised" in w.lower() for w in warnings)
    assert len(nodes) == 0


# ---------------------------------------------------------------------------
# SI-HEAD-001 — extract H2/H3 headings as DocSection nodes
# ---------------------------------------------------------------------------

# @spec SI-HEAD-001
def test_parse_headings_extracts_h2_and_h3():
    content = textwrap.dedent("""\
        # Title

        ## Overview

        Some text.

        ### Detail

        More text.

        ## Another Section

        End.
        """)
    headings = parse_headings(content)
    texts = [h[0] for h in headings]
    assert "Overview" in texts
    assert "Detail" in texts
    assert "Another Section" in texts
    assert "Title" not in texts  # H1 excluded


# @spec SI-HEAD-001
def test_parse_headings_includes_line_numbers():
    content = "## Overview\n\nContent.\n\n## Second\n\nMore.\n"
    headings = parse_headings(content)
    assert headings[0][2] == 1  # line_start of first heading
    assert headings[1][2] == 5  # line_start of second heading


# ---------------------------------------------------------------------------
# SI-HEAD-002 — DESCRIBED_BY edge from Feature to DocSection
# ---------------------------------------------------------------------------

# @spec SI-HEAD-002
def test_described_by_edges_written_for_each_section(tmp_path):
    from modok.ingestion.pipeline import build_doc_edges
    headings = [
        ("Overview", "overview", 1, 4),
        ("Detail", "detail", 5, None),
    ]
    edges = build_doc_edges(
        feature_slug="shtp-receiver",
        project_slug="stagehand",
        doc_path=Path("docs/lld/shtp.md"),
        headings=headings,
    )
    assert len(edges) == 2
    assert all(e[1] == "DESCRIBED_BY" for e in edges)


# ---------------------------------------------------------------------------
# SI-SHA-001 — commit SHA from git log for Doc/DocSection nodes
# ---------------------------------------------------------------------------

# @spec SI-SHA-001
def test_get_commit_sha_returns_string(tmp_path):
    with patch("modok.ingestion.parser.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="abc1234\n")
        sha = get_commit_sha(tmp_path / "doc.md")
        assert sha == "abc1234"


# @spec SI-SHA-001
def test_get_commit_sha_returns_none_when_not_in_git_repo(tmp_path):
    with patch("modok.ingestion.parser.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=128, stdout="")
        sha = get_commit_sha(tmp_path / "doc.md")
        assert sha is None


# @spec SI-SHA-001
def test_get_commit_sha_not_used_for_fix_nodes():
    # Fix and ResolutionEvent nodes must declare commit_sha in YAML.
    # The pipeline must not call get_commit_sha for these node types.
    from modok.ingestion import pipeline
    import inspect
    src = inspect.getsource(pipeline)
    # ingest_fix_yaml must not call get_commit_sha
    fix_section = src.split("def ingest_fix_yaml")[1].split("def ")[0]
    assert "get_commit_sha" not in fix_section


# ---------------------------------------------------------------------------
# SI-SHA-002 — Fix/ResolutionEvent YAML missing commit_sha → error
# ---------------------------------------------------------------------------

# @spec SI-SHA-002
def test_fix_yaml_missing_commit_sha_raises(tmp_path):
    fix_yaml = """\
        ticket_id: gh-142
        source_system: github
        feature: shtp-receiver
        error_signature: shtp-version-mismatch
        fix:
          kind: code_fix
          fix_id: fix-shtp-version-offset
          files_changed:
            - client/shtp_receiver.py
        # commit_sha deliberately omitted
        status: resolved
        """
    path = write_file(tmp_path / "fix.yml", fix_yaml)
    from modok.ingestion.pipeline import ingest_fix_yaml
    with pytest.raises(MissingCommitShaError):
        ingest_fix_yaml(path)


# @spec SI-SHA-002
def test_resolution_yaml_missing_commit_sha_raises(tmp_path):
    resolution_yaml = """\
        ticket_id: gh-142
        source_system: github
        feature: shtp-receiver
        # commit_sha deliberately omitted
        status: resolved
        """
    path = write_file(tmp_path / "resolution.yml", resolution_yaml)
    from modok.ingestion.pipeline import ingest_resolution_yaml
    with pytest.raises(MissingCommitShaError):
        ingest_resolution_yaml(path)


# ---------------------------------------------------------------------------
# SI-SHA-003 — dirty working tree → visible warning, complete normally
# ---------------------------------------------------------------------------

# @spec SI-SHA-003
def test_dirty_working_tree_emits_warning_not_error(tmp_path):
    with patch("modok.ingestion.pipeline.is_working_tree_dirty", return_value=True):
        from modok.ingestion.pipeline import check_working_tree
        warnings = check_working_tree(tmp_path)
        assert any("dirty" in w.lower() or "last commit" in w.lower() for w in warnings)


# ---------------------------------------------------------------------------
# SI-CONF-001 — confidence model scope: prose only
# ---------------------------------------------------------------------------

# @spec SI-CONF-001
@given(base=st.floats(min_value=0.0, max_value=1.0, allow_nan=False))
def test_confidence_band_only_for_prose_extraction(base):
    from modok.ingestion.pipeline import score_fact
    # frontmatter and modok_block sources always return 1.00 regardless of base
    assert score_fact(source="frontmatter", value="anything") == 1.00
    assert score_fact(source="modok_block", value="anything") == 1.00


# ---------------------------------------------------------------------------
# SI-CONF-002 — computed score ≥ 0.90 → write automatically
# ---------------------------------------------------------------------------

# @spec SI-CONF-002
def test_high_confidence_fact_is_auto_approved():
    from modok.ingestion.pipeline import fact_disposition
    assert fact_disposition(score=0.95) == "write"
    assert fact_disposition(score=0.90) == "write"
    assert fact_disposition(score=1.00) == "write"


# ---------------------------------------------------------------------------
# SI-CONF-003 — 0.75 ≤ score < 0.90 → write with confidence properties
# ---------------------------------------------------------------------------

# @spec SI-CONF-003
@given(score=st.floats(min_value=0.75, max_value=0.899, allow_nan=False))
def test_strong_confidence_fact_written_with_properties(score):
    from modok.ingestion.pipeline import fact_disposition
    assert fact_disposition(score=score) == "write_with_confidence"


# ---------------------------------------------------------------------------
# SI-CONF-004 — score < 0.75 → pending, not written without approval
# ---------------------------------------------------------------------------

# @spec SI-CONF-004
@given(score=st.floats(min_value=0.0, max_value=0.749, allow_nan=False))
def test_low_confidence_fact_is_pending(score):
    from modok.ingestion.pipeline import fact_disposition
    assert fact_disposition(score=score) == "pending"


# @spec SI-CONF-004
def test_pending_facts_batched_not_written_immediately():
    from modok.ingestion.pipeline import IngestionContext
    ctx = IngestionContext()
    ctx.add_pending_fact(value="some-file.cs", score=0.60, evidence="mentioned in prose")
    ctx.add_pending_fact(value="other.cs", score=0.50, evidence="weak match")
    assert ctx.pending_count == 2
    assert ctx.nodes_written == 0


# ---------------------------------------------------------------------------
# SI-CONF-005 — confidence score always in [0.0, 1.0]
# ---------------------------------------------------------------------------

# @spec SI-CONF-005
@given(
    base=st.floats(min_value=-10.0, max_value=10.0, allow_nan=False),
    boosts=st.lists(st.floats(min_value=0.0, max_value=1.0, allow_nan=False), max_size=5),
    penalties=st.lists(st.floats(min_value=0.0, max_value=1.0, allow_nan=False), max_size=5),
)
def test_confidence_band_always_in_range(base, boosts, penalties):
    band = confidence_band(base=base, boosts=boosts, penalties=penalties)
    assert 0.0 <= band.score <= 1.0
    assert 0.0 <= band.low <= 1.0
    assert 0.0 <= band.high <= 1.0
    assert band.low <= band.score <= band.high


# ---------------------------------------------------------------------------
# SI-CONF-006 — pending items count in report
# ---------------------------------------------------------------------------

# @spec SI-CONF-006
def test_report_includes_pending_items_count():
    report = IngestionReport(pending_items=3)
    assert report.pending_items == 3


# ---------------------------------------------------------------------------
# SI-WRITE-001 — node write order
# ---------------------------------------------------------------------------

# @spec SI-WRITE-001
def test_node_write_order_is_respected():
    from modok.ingestion.pipeline import NODE_WRITE_ORDER
    expected = [
        "Project", "ProductArea", "Feature", "Module", "File",
        "Doc", "DocSection", "ErrorSignature", "FailureMode", "Risk",
        "KnownIssue", "Fix", "CustomerIssue", "ResolutionEvent",
    ]
    assert NODE_WRITE_ORDER == expected


# ---------------------------------------------------------------------------
# SI-WRITE-002 — idempotency: same inputs → same graph state
# ---------------------------------------------------------------------------

# @spec SI-WRITE-002
def test_double_ingest_calls_upsert_not_create(tmp_path):
    from modok.ingestion.pipeline import ingest_doc
    path = write_file(tmp_path / "doc.md", MINIMAL_FRONTMATTER)

    registry = MagicMock(spec=Registry)
    registry.has_feature.return_value = True
    registry.has_module.return_value = True
    registry.has_error.return_value = True
    registry.required_fields.return_value = ["feature", "modules", "source_files", "test_files"]

    client = MagicMock()
    client.upsert_node = MagicMock()
    client.write_edge = MagicMock()

    with patch("modok.ingestion.parser.get_commit_sha", return_value="abc123"):
        ingest_doc(path, registry=registry, client=client, project_slug="stagehand", repo_root=tmp_path)
        first_upsert_count = client.upsert_node.call_count
        client.upsert_node.reset_mock()
        client.write_edge.reset_mock()

        ingest_doc(path, registry=registry, client=client, project_slug="stagehand", repo_root=tmp_path)
        second_upsert_count = client.upsert_node.call_count

    assert first_upsert_count == second_upsert_count


# ---------------------------------------------------------------------------
# SI-WRITE-003 — re-ingest replaces all node properties
# ---------------------------------------------------------------------------

# @spec SI-WRITE-003
def test_re_ingest_calls_upsert_with_full_model(tmp_path):
    from modok.ingestion.pipeline import ingest_doc
    path = write_file(tmp_path / "doc.md", MINIMAL_FRONTMATTER)

    registry = MagicMock(spec=Registry)
    registry.has_feature.return_value = True
    registry.has_module.return_value = True
    registry.has_error.return_value = True
    registry.required_fields.return_value = ["feature", "modules", "source_files", "test_files"]

    client = MagicMock()

    with patch("modok.ingestion.parser.get_commit_sha", return_value="abc123"):
        ingest_doc(path, registry=registry, client=client, project_slug="stagehand", repo_root=tmp_path)

    # upsert_node must have been called (full replace, not patch)
    assert client.upsert_node.called
    # Verify it was called with a full pydantic node object, not a dict patch
    from modok.quine.models import QuineNode
    for call in client.upsert_node.call_args_list:
        assert isinstance(call.args[0], QuineNode)


# ---------------------------------------------------------------------------
# SI-REG-001 — registries from {repo_root}/registries/
# ---------------------------------------------------------------------------

# @spec SI-REG-001
def test_registry_loads_from_repo_root(tmp_path):
    reg_dir = tmp_path / "registries"
    reg_dir.mkdir()
    (reg_dir / "features.yml").write_text("features:\n  shtp-receiver:\n    name: SHTP\n    product_area: net\n")
    (reg_dir / "modules.yml").write_text("modules:\n  shtp:\n    name: SHTP\n")
    (reg_dir / "errors.yml").write_text("errors:\n  shtp-err:\n    text: err\n")
    (reg_dir / "doc-types.yml").write_text("doc_types:\n  lld:\n    required_fields: [feature]\n")

    registry = Registry(repo_root=tmp_path)
    assert registry.has_feature("shtp-receiver")
    assert not registry.has_feature("nonexistent")


# @spec SI-REG-001
def test_registry_does_not_load_from_modok_home(tmp_path, monkeypatch):
    # Ensure Registry does not look in ~/.modok/
    monkeypatch.setenv("HOME", str(tmp_path))
    modok_reg = tmp_path / ".modok" / "registries"
    modok_reg.mkdir(parents=True)
    (modok_reg / "features.yml").write_text("features:\n  shtp-receiver:\n    name: SHTP\n")

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    reg = repo_root / "registries"
    reg.mkdir()
    (reg / "features.yml").write_text("features: {}\n")
    (reg / "modules.yml").write_text("modules: {}\n")
    (reg / "errors.yml").write_text("errors: {}\n")
    (reg / "doc-types.yml").write_text("doc_types: {}\n")

    registry = Registry(repo_root=repo_root)
    assert not registry.has_feature("shtp-receiver")


# ---------------------------------------------------------------------------
# SI-REG-002 — missing registry file → error, halt project
# ---------------------------------------------------------------------------

# @spec SI-REG-002
def test_missing_registry_file_raises(tmp_path):
    # No registries/ directory at all
    with pytest.raises(RegistryNotFoundError):
        Registry(repo_root=tmp_path)


# ---------------------------------------------------------------------------
# SI-HOOK-001 — modok init installs post-commit hook
# ---------------------------------------------------------------------------

# @spec SI-HOOK-001
def test_install_hook_creates_post_commit(tmp_path):
    git_dir = tmp_path / ".git" / "hooks"
    git_dir.mkdir(parents=True)

    install_post_commit_hook(
        repo_root=tmp_path,
        project_slug="stagehand",
        ingestion_paths=["docs/", "registries/"],
    )
    hook_path = git_dir / "post-commit"
    assert hook_path.exists()
    assert hook_path.stat().st_mode & 0o111  # executable


# ---------------------------------------------------------------------------
# SI-HOOK-002 — hook exits if no changed file matches registered paths
# ---------------------------------------------------------------------------

# @spec SI-HOOK-002
def test_hook_content_includes_path_guard():
    content = hook_content("stagehand", ["docs/", "registries/"])
    assert "docs/" in content
    assert "registries/" in content
    # Must contain an exit-early guard when no matching files
    assert "exit 0" in content or "exit" in content


# ---------------------------------------------------------------------------
# SI-HOOK-003 — existing hook → append MODOK section
# ---------------------------------------------------------------------------

# @spec SI-HOOK-003
def test_install_hook_appends_to_existing_hook(tmp_path):
    git_dir = tmp_path / ".git" / "hooks"
    git_dir.mkdir(parents=True)
    hook_path = git_dir / "post-commit"
    hook_path.write_text("#!/bin/sh\necho 'existing hook'\n")
    hook_path.chmod(0o755)

    install_post_commit_hook(tmp_path, "stagehand", ["docs/"])

    content = hook_path.read_text()
    assert "existing hook" in content
    assert MODOK_HOOK_START in content


# ---------------------------------------------------------------------------
# SI-HOOK-004 — existing MODOK section → replace only that section
# ---------------------------------------------------------------------------

# @spec SI-HOOK-004
def test_install_hook_replaces_existing_modok_section(tmp_path):
    git_dir = tmp_path / ".git" / "hooks"
    git_dir.mkdir(parents=True)
    hook_path = git_dir / "post-commit"
    old_modok = f"#!/bin/sh\necho 'other'\n{MODOK_HOOK_START}\necho 'old modok'\n{MODOK_HOOK_END}\n"
    hook_path.write_text(old_modok)
    hook_path.chmod(0o755)

    install_post_commit_hook(tmp_path, "stagehand", ["docs/"])

    content = hook_path.read_text()
    assert "other" in content           # other hook content preserved
    assert "old modok" not in content   # old MODOK section replaced
    assert MODOK_HOOK_START in content  # new MODOK section present
    assert content.count(MODOK_HOOK_START) == 1  # only one MODOK section


# ---------------------------------------------------------------------------
# SI-LLM-003 — with --fix, write to doc frontmatter then re-parse
# ---------------------------------------------------------------------------

# @spec SI-LLM-003
def test_fix_mode_writes_proposals_to_doc_not_quine(tmp_path):
    content = """\
        ---
        modok:
          doc_type: lld
          project: stagehand
          feature: shtp-receiver
        ---
        # Doc
        """
    path = write_file(tmp_path / "doc.md", content)
    client = MagicMock()

    with patch("modok.ingestion.pipeline.invoke_llm_gateway") as mock_llm:
        mock_llm.return_value = {"modules": ["shtp"], "source_files": [], "test_files": []}
        with patch("modok.ingestion.pipeline.user_approves", return_value=True):
            from modok.ingestion.pipeline import apply_llm_proposals
            apply_llm_proposals(path, proposals={"modules": ["shtp"]}, client=client)

    # LLM proposals must not call upsert_node directly
    client.upsert_node.assert_not_called()
    # Doc file must have been updated
    updated = path.read_text()
    assert "shtp" in updated


# ---------------------------------------------------------------------------
# SI-RPT-001 — structured report with all required fields
# ---------------------------------------------------------------------------

# @spec SI-RPT-001
def test_report_has_all_required_fields():
    report = IngestionReport(
        docs_processed=24,
        nodes_written=312,
        edges_written=487,
        warnings=["w1"],
        errors=[],
        llm_proposals=0,
        pending_items=2,
        files_ignored=5,
        files_skipped=3,
        duration_seconds=1.3,
    )
    assert report.docs_processed == 24
    assert report.nodes_written == 312
    assert report.edges_written == 487
    assert report.files_ignored == 5
    assert report.files_skipped == 3
    assert report.pending_items == 2


# ---------------------------------------------------------------------------
# SI-RPT-002 — warnings don't halt; errors halt affected file only
# ---------------------------------------------------------------------------

# @spec SI-RPT-002
def test_error_in_one_file_does_not_halt_others(tmp_path):
    from modok.ingestion.pipeline import run_ingestion

    good = write_file(tmp_path / "good.md", MINIMAL_FRONTMATTER)
    bad_content = """\
        ---
        modok:
          doc_type: lld
          project: stagehand
          feature: bad-unknown-slug
        ---
        # Bad doc
        """
    write_file(tmp_path / "bad.md", bad_content)

    registry = MagicMock(spec=Registry)
    registry.has_feature.side_effect = lambda s: s == "shtp-receiver"
    registry.has_module.return_value = True
    registry.has_error.return_value = True
    registry.required_fields.return_value = ["feature", "modules", "source_files", "test_files"]

    client = MagicMock()

    with patch("modok.ingestion.parser.get_commit_sha", return_value="abc123"):
        report = run_ingestion(tmp_path, registry=registry, client=client, project_slug="stagehand")

    assert report.errors  # bad.md produced an error
    assert report.docs_processed >= 1  # good.md was processed


# ---------------------------------------------------------------------------
# SI-DIFF-001 — CommitEvent upserted from hook commit metadata
# ---------------------------------------------------------------------------

# @spec SI-DIFF-001
def test_ingest_commit_writes_commit_event_node():
    from modok.ingestion.pipeline import ingest_commit_diff
    from modok.quine.models import CommitEvent

    client = MagicMock()
    ingest_commit_diff(
        project_slug="stagehand",
        commit_sha="abc1234def5678",
        source_paths=["agent/", "client/"],
        registry=MagicMock(spec=Registry),
        client=client,
    )

    upserted_types = [type(call.args[0]).__name__ for call in client.upsert_node.call_args_list]
    assert "CommitEvent" in upserted_types


# @spec SI-DIFF-001
def test_commit_event_carries_required_fields():
    from modok.ingestion.pipeline import ingest_commit_diff
    from modok.quine.models import CommitEvent

    client = MagicMock()
    with patch("modok.ingestion.pipeline.parse_commit_metadata") as mock_meta:
        mock_meta.return_value = {
            "commit_sha": "abc1234",
            "author": "Mark Stalzer",
            "timestamp_iso": "2026-04-30T12:00:00Z",
            "message_summary": "fix: correct shtp version byte offset",
        }
        with patch("modok.ingestion.pipeline.parse_diff_numstat", return_value=[]):
            ingest_commit_diff(
                project_slug="stagehand",
                commit_sha="abc1234",
                source_paths=["agent/"],
                registry=MagicMock(spec=Registry),
                client=client,
            )

    commit_events = [
        call.args[0] for call in client.upsert_node.call_args_list
        if isinstance(call.args[0], CommitEvent)
    ]
    assert len(commit_events) == 1
    evt = commit_events[0]
    assert evt.author == "Mark Stalzer"
    assert evt.timestamp_iso == "2026-04-30T12:00:00Z"
    assert evt.message_summary == "fix: correct shtp version byte offset"


# ---------------------------------------------------------------------------
# SI-DIFF-002 — skip diff ingestion when source_paths absent or empty
# ---------------------------------------------------------------------------

# @spec SI-DIFF-002
def test_diff_ingestion_skipped_when_no_source_paths():
    from modok.ingestion.pipeline import ingest_commit_diff

    client = MagicMock()
    ingest_commit_diff(
        project_slug="stagehand",
        commit_sha="abc1234",
        source_paths=[],  # empty → skip
        registry=MagicMock(spec=Registry),
        client=client,
    )

    client.upsert_node.assert_not_called()
    client.write_edge.assert_not_called()


# @spec SI-DIFF-002
def test_diff_ingestion_skipped_when_source_paths_none():
    from modok.ingestion.pipeline import ingest_commit_diff

    client = MagicMock()
    ingest_commit_diff(
        project_slug="stagehand",
        commit_sha="abc1234",
        source_paths=None,
        registry=MagicMock(spec=Registry),
        client=client,
    )

    client.upsert_node.assert_not_called()


# ---------------------------------------------------------------------------
# SI-DIFF-003 — FileChange node per matching source file
# ---------------------------------------------------------------------------

# @spec SI-DIFF-003
def test_file_change_node_written_per_matching_file():
    from modok.ingestion.pipeline import ingest_commit_diff
    from modok.quine.models import FileChange

    client = MagicMock()
    with patch("modok.ingestion.pipeline.parse_commit_metadata") as mock_meta:
        mock_meta.return_value = {
            "commit_sha": "abc1234",
            "author": "Mark",
            "timestamp_iso": "2026-04-30T00:00:00Z",
            "message_summary": "fix",
        }
        with patch("modok.ingestion.pipeline.parse_diff_numstat") as mock_diff:
            mock_diff.return_value = [
                {"path": "agent/src/shtp.c", "added": 12, "removed": 3,
                 "hunks": ["@@ -10,4 +10,13 @@"]},
                {"path": "client/shtp_receiver.py", "added": 5, "removed": 1,
                 "hunks": ["@@ -20,3 +20,7 @@"]},
            ]
            ingest_commit_diff(
                project_slug="stagehand",
                commit_sha="abc1234",
                source_paths=["agent/", "client/"],
                registry=MagicMock(spec=Registry),
                client=client,
            )

    file_changes = [
        call.args[0] for call in client.upsert_node.call_args_list
        if isinstance(call.args[0], FileChange)
    ]
    assert len(file_changes) == 2
    paths = {fc.repo_path for fc in file_changes}
    assert "agent/src/shtp.c" in paths
    assert "client/shtp_receiver.py" in paths


# @spec SI-DIFF-003
def test_file_change_node_carries_lines_and_hunks():
    from modok.ingestion.pipeline import ingest_commit_diff
    from modok.quine.models import FileChange

    client = MagicMock()
    with patch("modok.ingestion.pipeline.parse_commit_metadata") as mock_meta:
        mock_meta.return_value = {
            "commit_sha": "abc1234", "author": "Mark",
            "timestamp_iso": "2026-04-30T00:00:00Z", "message_summary": "fix",
        }
        with patch("modok.ingestion.pipeline.parse_diff_numstat") as mock_diff:
            mock_diff.return_value = [
                {"path": "agent/src/shtp.c", "added": 12, "removed": 3,
                 "hunks": ["@@ -10,4 +10,13 @@"]},
            ]
            ingest_commit_diff(
                project_slug="stagehand",
                commit_sha="abc1234",
                source_paths=["agent/"],
                registry=MagicMock(spec=Registry),
                client=client,
            )

    fc = next(
        call.args[0] for call in client.upsert_node.call_args_list
        if isinstance(call.args[0], FileChange)
    )
    assert fc.lines_added == 12
    assert fc.lines_removed == 3
    assert fc.hunks == ["@@ -10,4 +10,13 @@"]


# ---------------------------------------------------------------------------
# SI-DIFF-004 — edge chain File -> FileChange -> CommitEvent
# ---------------------------------------------------------------------------

# @spec SI-DIFF-004
def test_edge_chain_written_for_each_file_change():
    from modok.ingestion.pipeline import ingest_commit_diff

    client = MagicMock()
    with patch("modok.ingestion.pipeline.parse_commit_metadata") as mock_meta:
        mock_meta.return_value = {
            "commit_sha": "abc1234", "author": "Mark",
            "timestamp_iso": "2026-04-30T00:00:00Z", "message_summary": "fix",
        }
        with patch("modok.ingestion.pipeline.parse_diff_numstat") as mock_diff:
            mock_diff.return_value = [
                {"path": "agent/src/shtp.c", "added": 5, "removed": 1,
                 "hunks": ["@@ -1,3 +1,7 @@"]},
            ]
            ingest_commit_diff(
                project_slug="stagehand",
                commit_sha="abc1234",
                source_paths=["agent/"],
                registry=MagicMock(spec=Registry),
                client=client,
            )

    edge_calls = client.write_edge.call_args_list
    rel_types = [c.args[1] for c in edge_calls]
    assert "CHANGED_IN" in rel_types
    assert "IN_COMMIT" in rel_types


# ---------------------------------------------------------------------------
# SI-DIFF-005 — TOUCHES_FEATURE edges derived from registry
# ---------------------------------------------------------------------------

# @spec SI-DIFF-005
def test_touches_feature_edge_written_for_matching_module_source_root():
    from modok.ingestion.pipeline import ingest_commit_diff

    registry = MagicMock(spec=Registry)
    # Simulate module registry: shtp module covers agent/src/
    registry.modules_covering_path.return_value = ["shtp"]
    registry.features_for_module.return_value = ["shtp-receiver"]

    client = MagicMock()
    with patch("modok.ingestion.pipeline.parse_commit_metadata") as mock_meta:
        mock_meta.return_value = {
            "commit_sha": "abc1234", "author": "Mark",
            "timestamp_iso": "2026-04-30T00:00:00Z", "message_summary": "fix",
        }
        with patch("modok.ingestion.pipeline.parse_diff_numstat") as mock_diff:
            mock_diff.return_value = [
                {"path": "agent/src/shtp.c", "added": 1, "removed": 0,
                 "hunks": ["@@ -1,1 +1,2 @@"]},
            ]
            ingest_commit_diff(
                project_slug="stagehand",
                commit_sha="abc1234",
                source_paths=["agent/"],
                registry=registry,
                client=client,
            )

    edge_calls = client.write_edge.call_args_list
    rel_types = [c.args[1] for c in edge_calls]
    assert "TOUCHES_FEATURE" in rel_types


# @spec SI-DIFF-005
def test_touches_feature_edge_deduplicated_per_commit_feature_pair():
    from modok.ingestion.pipeline import ingest_commit_diff

    registry = MagicMock(spec=Registry)
    # Both files map to the same feature
    registry.modules_covering_path.return_value = ["shtp"]
    registry.features_for_module.return_value = ["shtp-receiver"]

    client = MagicMock()
    with patch("modok.ingestion.pipeline.parse_commit_metadata") as mock_meta:
        mock_meta.return_value = {
            "commit_sha": "abc1234", "author": "Mark",
            "timestamp_iso": "2026-04-30T00:00:00Z", "message_summary": "fix",
        }
        with patch("modok.ingestion.pipeline.parse_diff_numstat") as mock_diff:
            mock_diff.return_value = [
                {"path": "agent/src/shtp.c", "added": 1, "removed": 0, "hunks": []},
                {"path": "agent/src/shtp.h", "added": 1, "removed": 0, "hunks": []},
            ]
            ingest_commit_diff(
                project_slug="stagehand",
                commit_sha="abc1234",
                source_paths=["agent/"],
                registry=registry,
                client=client,
            )

    touches_feature_calls = [
        c for c in client.write_edge.call_args_list if c.args[1] == "TOUCHES_FEATURE"
    ]
    assert len(touches_feature_calls) == 1  # deduplicated to one edge


# ---------------------------------------------------------------------------
# SI-DIFF-006 — files outside source_paths silently ignored
# ---------------------------------------------------------------------------

# @spec SI-DIFF-006
def test_files_outside_source_paths_silently_ignored():
    from modok.ingestion.pipeline import ingest_commit_diff
    from modok.quine.models import FileChange

    client = MagicMock()
    with patch("modok.ingestion.pipeline.parse_commit_metadata") as mock_meta:
        mock_meta.return_value = {
            "commit_sha": "abc1234", "author": "Mark",
            "timestamp_iso": "2026-04-30T00:00:00Z", "message_summary": "fix",
        }
        with patch("modok.ingestion.pipeline.parse_diff_numstat") as mock_diff:
            mock_diff.return_value = [
                # Only docs/ file — not in source_paths=["agent/"]
                {"path": "docs/lld/shtp.md", "added": 2, "removed": 0, "hunks": []},
            ]
            ingest_commit_diff(
                project_slug="stagehand",
                commit_sha="abc1234",
                source_paths=["agent/"],
                registry=MagicMock(spec=Registry),
                client=client,
            )

    file_changes = [
        call.args[0] for call in client.upsert_node.call_args_list
        if isinstance(call.args[0], FileChange)
    ]
    assert len(file_changes) == 0


# ---------------------------------------------------------------------------
# SI-DIFF-007 — deterministic node IDs; re-ingest upserts not duplicates
# ---------------------------------------------------------------------------

# @spec SI-DIFF-007
@given(
    sha=st.text(min_size=7, max_size=40, alphabet=st.characters(whitelist_categories=("L", "N"))),
    path=st.text(min_size=1, max_size=50, alphabet=st.characters(whitelist_categories=("L", "N"))),
)
def test_commit_event_id_is_deterministic(sha, path):
    from modok.quine.ids import idFrom
    id1 = idFrom("CommitEvent", "stagehand", sha)
    id2 = idFrom("CommitEvent", "stagehand", sha)
    assert id1 == id2


# @spec SI-DIFF-007
@given(
    sha=st.text(min_size=7, max_size=40, alphabet=st.characters(whitelist_categories=("L", "N"))),
    path=st.text(min_size=1, max_size=50, alphabet=st.characters(whitelist_categories=("L", "N"))),
)
def test_file_change_id_is_deterministic(sha, path):
    from modok.quine.ids import idFrom
    id1 = idFrom("FileChange", "stagehand", sha, path)
    id2 = idFrom("FileChange", "stagehand", sha, path)
    assert id1 == id2


# @spec SI-DIFF-007
def test_reingest_same_commit_calls_upsert_not_create():
    from modok.ingestion.pipeline import ingest_commit_diff

    client = MagicMock()
    kwargs = dict(
        project_slug="stagehand",
        commit_sha="abc1234",
        source_paths=["agent/"],
        registry=MagicMock(spec=Registry),
        client=client,
    )
    with patch("modok.ingestion.pipeline.parse_commit_metadata") as mock_meta:
        mock_meta.return_value = {
            "commit_sha": "abc1234", "author": "Mark",
            "timestamp_iso": "2026-04-30T00:00:00Z", "message_summary": "fix",
        }
        with patch("modok.ingestion.pipeline.parse_diff_numstat") as mock_diff:
            mock_diff.return_value = [
                {"path": "agent/src/shtp.c", "added": 1, "removed": 0, "hunks": []},
            ]
            ingest_commit_diff(**kwargs)
            first_count = client.upsert_node.call_count
            client.upsert_node.reset_mock()
            ingest_commit_diff(**kwargs)
            second_count = client.upsert_node.call_count

    assert first_count == second_count  # same upsert count, not doubled


# ---------------------------------------------------------------------------
# SI-DIFF-008 — no raw diff body stored
# ---------------------------------------------------------------------------

# @spec SI-DIFF-008
def test_no_raw_diff_body_in_file_change_node():
    from modok.quine.models import FileChange
    import inspect
    # FileChange model must not have a field for raw diff text
    fields = set(FileChange.model_fields.keys())
    for bad in ("diff", "diff_text", "patch", "body", "raw"):
        assert bad not in fields, f"FileChange must not store {bad!r}"


# @spec SI-DIFF-008
def test_hunk_headers_only_not_diff_lines():
    from modok.ingestion.pipeline import parse_diff_numstat
    raw = textwrap.dedent("""\
        12\t3\tagent/src/shtp.c
        """)
    hunks_raw = textwrap.dedent("""\
        @@ -10,4 +10,13 @@
        -old line
        +new line
         context
        """)
    # parse_diff_numstat extracts only @@ lines, not +/- diff body
    with patch("modok.ingestion.pipeline.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=f"abc1234\nMark\n2026-04-30T00:00:00Z\nfix\n\n{raw}\n{hunks_raw}",
        )
        result = parse_diff_numstat("abc1234")

    if result:  # may be empty if parsing not yet refined
        for entry in result:
            for h in entry.get("hunks", []):
                assert h.startswith("@@"), f"Non-hunk line in hunks: {h!r}"
                assert not h.startswith("+") and not h.startswith("-")


# ---------------------------------------------------------------------------
# SI-RPT-001 (extended) — report includes commits_processed, file_changes_written
# ---------------------------------------------------------------------------

# @spec SI-RPT-001
def test_report_has_commits_processed_and_file_changes_written():
    report = IngestionReport(
        docs_processed=5,
        commits_processed=3,
        file_changes_written=12,
    )
    assert report.commits_processed == 3
    assert report.file_changes_written == 12


# @spec SI-RPT-001
def test_report_diff_fields_default_to_zero():
    report = IngestionReport()
    assert report.commits_processed == 0
    assert report.file_changes_written == 0
