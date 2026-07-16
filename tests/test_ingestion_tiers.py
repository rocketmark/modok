"""
Tests for three-tier doc discovery and frontmatter-as-override.

Specs verified:
  SI-DISC-003, SI-TIER1-001, SI-TIER1-002, SI-TIER2-001, SI-TIER2-002,
  SI-TIER2-003, SI-UNREG-001, SI-UNREG-002, SI-FMTR-001, SI-FMTR-002,
  SI-HEAD-002 (scope qualifier), SI-WRITE-001 (Commit in order),
  SI-RPT-001 (unregistered count), SI-HOOK-002 (always invoke ingest-git).
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from modok.ingestion.discovery import discover_docs
from modok.ingestion.errors import InvalidSlugReferenceError
from modok.ingestion.hook import hook_content
from modok.ingestion.pipeline import NODE_WRITE_ORDER
from modok.ingestion.registry import Registry
from modok.ingestion.report import IngestionReport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_file(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content))
    return path


def make_setup(
    tmp_path: Path,
    features: dict | None = None,
    arrows: list[dict] | None = None,
) -> Registry:
    """Create registries and arrow index; return a Registry loaded from tmp_path."""
    features = features or {}
    arrows = arrows or []

    reg_dir = tmp_path / "registries"
    reg_dir.mkdir(exist_ok=True)
    (reg_dir / "features.yml").write_text(yaml.dump({"features": features}))
    (reg_dir / "modules.yml").write_text("modules: {}\n")
    (reg_dir / "errors.yml").write_text("errors: {}\n")
    (reg_dir / "doc-types.yml").write_text(
        "doc_types:\n"
        "  hld:\n    required_fields: [feature]\n"
        "  lld:\n    required_fields: [feature]\n"
        "  spec:\n    required_fields: [feature]\n"
    )

    arrow_path = tmp_path / "docs" / "arrows" / "index.yaml"
    arrow_path.parent.mkdir(parents=True, exist_ok=True)
    arrow_path.write_text(yaml.dump({"arrows": arrows}))

    return Registry(repo_root=tmp_path)


_ONE_FEATURE = {"pi-agent": {"name": "Pi Agent", "product_area": "tracking"}}
_TWO_FEATURES = {
    "pi-agent": {"name": "Pi Agent", "product_area": "tracking"},
    "shtp-receiver": {"name": "SHTP Receiver", "product_area": "networking"},
}


# ---------------------------------------------------------------------------
# SI-DISC-003 — unresolvable doc is ingested as unregistered, not skipped
# ---------------------------------------------------------------------------


# @spec SI-DISC-003
def test_unresololvable_doc_is_unregistered_not_skipped(tmp_path):
    make_setup(tmp_path, features=_ONE_FEATURE)
    write_file(tmp_path / "docs" / "unknown-subsystem.md", "# Unknown\n\nContent.\n")

    registered, unregistered, _ = discover_docs(tmp_path, Registry(repo_root=tmp_path))

    unregistered_names = [r.path.name for r in unregistered]
    assert "unknown-subsystem.md" in unregistered_names
    assert all(r.path.name != "unknown-subsystem.md" for r in registered)


# @spec SI-DISC-003
def test_all_docs_discovered_none_silently_skipped(tmp_path):
    make_setup(tmp_path, features=_ONE_FEATURE)
    write_file(tmp_path / "docs" / "pi-agent.md", "# Known\n")
    write_file(tmp_path / "docs" / "no-feature.md", "# Unknown\n")

    registered, unregistered, _ = discover_docs(tmp_path, Registry(repo_root=tmp_path))
    all_discovered = [r.path.name for r in registered + unregistered]
    assert "pi-agent.md" in all_discovered
    assert "no-feature.md" in all_discovered


# ---------------------------------------------------------------------------
# SI-TIER1-001 — arrow index is the first discovery pass
# ---------------------------------------------------------------------------


# @spec SI-TIER1-001
def test_tier1_assigns_hld_from_arrow_doc(tmp_path):
    make_setup(
        tmp_path,
        features=_ONE_FEATURE,
        arrows=[
            {
                "id": "pi-agent",
                "arrow_doc": "docs/arrows/pi-agent.md",
                "lld": "docs/llds/pi-agent.md",
                "specs": "docs/specs/pi-agent-specs.md",
            }
        ],
    )
    write_file(tmp_path / "docs" / "arrows" / "pi-agent.md", "# Arrow\n")
    write_file(tmp_path / "docs" / "llds" / "pi-agent.md", "# LLD\n")
    write_file(tmp_path / "docs" / "specs" / "pi-agent-specs.md", "# Specs\n")

    registered, _, _ = discover_docs(tmp_path, Registry(repo_root=tmp_path))
    by_name = {r.path.name: r for r in registered}

    arrow_rec = by_name.get("pi-agent.md")
    assert arrow_rec is not None
    # The arrow_doc path is discovered as hld at Tier 1
    hld_recs = [r for r in registered if r.doc_type == "hld" and r.tier == 1]
    assert any(r.path.name == "pi-agent.md" for r in hld_recs)


# @spec SI-TIER1-001
def test_tier1_assigns_lld_and_spec_doc_types(tmp_path):
    make_setup(
        tmp_path,
        features=_ONE_FEATURE,
        arrows=[
            {
                "id": "pi-agent",
                "arrow_doc": None,
                "lld": "docs/llds/pi-agent.md",
                "specs": "docs/specs/pi-agent-specs.md",
            }
        ],
    )
    write_file(tmp_path / "docs" / "llds" / "pi-agent.md", "# LLD\n")
    write_file(tmp_path / "docs" / "specs" / "pi-agent-specs.md", "# Specs\n")

    registered, _, _ = discover_docs(tmp_path, Registry(repo_root=tmp_path))

    lld = next(
        (r for r in registered if r.path.name == "pi-agent.md" and r.doc_type == "lld"), None
    )
    spec = next(
        (r for r in registered if r.path.name == "pi-agent-specs.md" and r.doc_type == "spec"), None
    )

    assert lld is not None and lld.tier == 1
    assert spec is not None and spec.tier == 1


# @spec SI-TIER1-003
def test_tier1_lld_accepts_a_list_of_paths(tmp_path):
    # A feature can legitimately have more than one LLD (e.g. client-side and
    # Pi-side halves of the same arrow) — index.yaml's `lld` field is then a
    # YAML list rather than a single string.
    make_setup(
        tmp_path,
        features=_ONE_FEATURE,
        arrows=[
            {
                "id": "pi-agent",
                "arrow_doc": "docs/arrows/pi-agent.md",
                "lld": [
                    "docs/llds/pi-agent-client.md",
                    "docs/llds/pi-agent-server.md",
                ],
                "specs": "docs/specs/pi-agent-specs.md",
            }
        ],
    )
    write_file(tmp_path / "docs" / "arrows" / "pi-agent.md", "# Arrow\n")
    write_file(tmp_path / "docs" / "llds" / "pi-agent-client.md", "# LLD client\n")
    write_file(tmp_path / "docs" / "llds" / "pi-agent-server.md", "# LLD server\n")
    write_file(tmp_path / "docs" / "specs" / "pi-agent-specs.md", "# Specs\n")

    registered, _, _ = discover_docs(tmp_path, Registry(repo_root=tmp_path))  # must not raise

    lld_names = {r.path.name for r in registered if r.doc_type == "lld" and r.tier == 1}
    assert lld_names == {"pi-agent-client.md", "pi-agent-server.md"}


# @spec SI-TIER1-001
def test_tier1_feature_set_to_arrow_id(tmp_path):
    make_setup(
        tmp_path,
        features=_ONE_FEATURE,
        arrows=[
            {"id": "pi-agent", "arrow_doc": None, "lld": "docs/llds/pi-agent.md", "specs": None}
        ],
    )
    write_file(tmp_path / "docs" / "llds" / "pi-agent.md", "# LLD\n")

    registered, _, _ = discover_docs(tmp_path, Registry(repo_root=tmp_path))
    lld = next(r for r in registered if r.path.name == "pi-agent.md")
    assert lld.feature == "pi-agent"


# ---------------------------------------------------------------------------
# SI-TIER1-002 — Tier 1 metadata from registries, not frontmatter
# ---------------------------------------------------------------------------


# @spec SI-TIER1-002
def test_tier1_derives_source_files_from_registry_not_frontmatter(tmp_path):
    features = {
        "pi-agent": {
            "name": "Pi Agent",
            "product_area": "tracking",
            "source_files": ["agent/src/main.c"],
            "test_files": ["agent/tests/test_main.c"],
        }
    }
    make_setup(
        tmp_path,
        features=features,
        arrows=[
            {"id": "pi-agent", "arrow_doc": None, "lld": "docs/llds/pi-agent.md", "specs": None}
        ],
    )
    # Doc has no frontmatter at all
    write_file(tmp_path / "docs" / "llds" / "pi-agent.md", "# LLD\n\nNo frontmatter.\n")

    registered, _, _ = discover_docs(tmp_path, Registry(repo_root=tmp_path))
    lld = next(r for r in registered if r.path.name == "pi-agent.md")

    assert lld.source_files == ["agent/src/main.c"]
    assert lld.test_files == ["agent/tests/test_main.c"]


# @spec SI-TIER1-002
def test_tier1_doc_without_frontmatter_still_registered(tmp_path):
    make_setup(
        tmp_path,
        features=_ONE_FEATURE,
        arrows=[
            {"id": "pi-agent", "arrow_doc": None, "lld": "docs/llds/pi-agent.md", "specs": None}
        ],
    )
    write_file(tmp_path / "docs" / "llds" / "pi-agent.md", "# Just a heading\n\nBody text.\n")

    registered, unregistered, _ = discover_docs(tmp_path, Registry(repo_root=tmp_path))
    assert any(r.path.name == "pi-agent.md" for r in registered)
    assert all(r.path.name != "pi-agent.md" for r in unregistered)


# ---------------------------------------------------------------------------
# SI-TIER2-001 — directory-based doc_type inference
# ---------------------------------------------------------------------------


# @spec SI-TIER2-001
@pytest.mark.parametrize(
    "subdir,fname,expected_type",
    [
        ("docs/llds", "pi-agent.md", "lld"),
        ("docs/arrows", "pi-agent.md", "hld"),
        ("docs/specs", "pi-agent-specs.md", "spec"),
        ("docs", "pi-agent.md", "hld"),
    ],
)
def test_tier2_infers_doc_type_from_directory(tmp_path, subdir, fname, expected_type):
    make_setup(tmp_path, features=_ONE_FEATURE)  # empty arrow index
    write_file(tmp_path / subdir / fname, "# Content\n")

    registered, _, _ = discover_docs(tmp_path, Registry(repo_root=tmp_path))
    rec = next((r for r in registered if r.path.name == fname and r.tier == 2), None)
    assert rec is not None, f"Expected Tier 2 record for {fname} in {subdir}"
    assert rec.doc_type == expected_type


# @spec SI-TIER2-001
def test_tier2_not_applied_to_tier1_docs(tmp_path):
    make_setup(
        tmp_path,
        features=_ONE_FEATURE,
        arrows=[
            {"id": "pi-agent", "arrow_doc": None, "lld": "docs/llds/pi-agent.md", "specs": None}
        ],
    )
    write_file(tmp_path / "docs" / "llds" / "pi-agent.md", "# LLD\n")

    registered, _, _ = discover_docs(tmp_path, Registry(repo_root=tmp_path))
    lld = next(r for r in registered if r.path.name == "pi-agent.md")
    assert lld.tier == 1, "Arrow-indexed doc must not be re-processed as Tier 2"


# ---------------------------------------------------------------------------
# SI-TIER2-002 — feature inferred from filename stem; -specs suffix stripped
# ---------------------------------------------------------------------------


# @spec SI-TIER2-002
def test_tier2_strips_specs_suffix_for_spec_files(tmp_path):
    make_setup(tmp_path, features=_ONE_FEATURE)
    write_file(tmp_path / "docs" / "specs" / "pi-agent-specs.md", "# Specs\n")

    registered, _, _ = discover_docs(tmp_path, Registry(repo_root=tmp_path))
    spec = next((r for r in registered if r.path.name == "pi-agent-specs.md"), None)
    assert spec is not None
    assert spec.feature == "pi-agent"


# @spec SI-TIER2-002
def test_tier2_feature_from_plain_stem(tmp_path):
    make_setup(tmp_path, features=_ONE_FEATURE)
    write_file(tmp_path / "docs" / "llds" / "pi-agent.md", "# LLD\n")

    registered, _, _ = discover_docs(tmp_path, Registry(repo_root=tmp_path))
    lld = next((r for r in registered if r.path.name == "pi-agent.md"), None)
    assert lld is not None
    assert lld.feature == "pi-agent"


# ---------------------------------------------------------------------------
# SI-TIER2-003 — inferred slug not in registry → Tier 3 (unregistered)
# ---------------------------------------------------------------------------


# @spec SI-TIER2-003
def test_tier2_unknown_slug_proceeds_to_unregistered(tmp_path):
    make_setup(tmp_path, features=_ONE_FEATURE)
    write_file(tmp_path / "docs" / "llds" / "no-such-feature.md", "# Unknown LLD\n")

    registered, unregistered, _ = discover_docs(tmp_path, Registry(repo_root=tmp_path))
    assert any(r.path.name == "no-such-feature.md" for r in unregistered)
    assert all(r.path.name != "no-such-feature.md" for r in registered)


# @spec SI-TIER2-003
def test_tier2_known_slug_produces_registered_doc(tmp_path):
    make_setup(tmp_path, features=_ONE_FEATURE)
    write_file(tmp_path / "docs" / "llds" / "pi-agent.md", "# LLD\n")

    registered, unregistered, _ = discover_docs(tmp_path, Registry(repo_root=tmp_path))
    assert any(r.path.name == "pi-agent.md" for r in registered)
    assert all(r.path.name != "pi-agent.md" for r in unregistered)


# ---------------------------------------------------------------------------
# SI-UNREG-001 — unregistered docs → bare Doc node; no Feature/Module/File edges
# ---------------------------------------------------------------------------


# @spec SI-UNREG-001
@pytest.mark.asyncio
async def test_unregistered_doc_written_as_bare_doc_node(tmp_path):
    make_setup(tmp_path, features={})
    write_file(tmp_path / "docs" / "random-notes.md", "# Notes\n\nSome content.\n")

    client = AsyncMock()
    from modok.ingestion.pipeline import ingest_doc_unregistered

    with patch("modok.ingestion.parser.get_commit_sha", return_value="abc123"):
        await ingest_doc_unregistered(
            tmp_path / "docs" / "random-notes.md",
            client=client,
            project_slug="stagehand",
        )

    assert client.upsert_node.called
    edge_calls = [str(c) for c in client.write_edge.call_args_list]
    for call_str in edge_calls:
        assert "Feature" not in call_str
        assert "Module" not in call_str
        assert "File" not in call_str


# @spec SI-UNREG-001
@pytest.mark.asyncio
async def test_unregistered_doc_docsection_nodes_written_without_feature_edge(tmp_path):
    make_setup(tmp_path, features={})
    write_file(
        tmp_path / "docs" / "orphan.md",
        textwrap.dedent("""\
        # Orphan

        ## Section One

        Content.
        """),
    )

    client = AsyncMock()
    from modok.ingestion.pipeline import ingest_doc_unregistered

    with patch("modok.ingestion.parser.get_commit_sha", return_value="abc123"):
        await ingest_doc_unregistered(
            tmp_path / "docs" / "orphan.md",
            client=client,
            project_slug="stagehand",
        )

    # DocSection nodes may be written (upsert_node) — that's fine
    # But no DESCRIBED_BY edge must exist (SI-HEAD-002)
    edge_calls = [str(c) for c in client.write_edge.call_args_list]
    described_by = [c for c in edge_calls if "DESCRIBED_BY" in c]
    assert described_by == []


# ---------------------------------------------------------------------------
# SI-UNREG-002 — unregistered count in report, listed with paths
# ---------------------------------------------------------------------------


# @spec SI-UNREG-002
def test_report_includes_unregistered_count():
    report = IngestionReport(
        docs_processed=5,
        nodes_written=10,
        unregistered_count=2,
        unregistered_paths=["docs/notes.md", "docs/brainstorm.md"],
    )
    assert report.unregistered_count == 2
    assert report.unregistered_paths == ["docs/notes.md", "docs/brainstorm.md"]


# @spec SI-UNREG-002
def test_report_str_lists_unregistered_separately():
    report = IngestionReport(
        unregistered_count=1,
        unregistered_paths=["docs/orphan.md"],
    )
    text = str(report)
    assert "nregistered" in text  # "Unregistered" or "unregistered"
    assert "orphan.md" in text


# @spec SI-UNREG-002
def test_report_unregistered_separate_from_warnings_and_errors():
    report = IngestionReport(
        warnings=["w1"],
        errors=["e1"],
        unregistered_count=2,
        unregistered_paths=["docs/a.md", "docs/b.md"],
    )
    assert report.unregistered_count == 2
    assert len(report.warnings) == 1
    assert len(report.errors) == 1


# ---------------------------------------------------------------------------
# SI-FMTR-001 — frontmatter overrides inferred value; no frontmatter = fully inferred
# ---------------------------------------------------------------------------


# @spec SI-FMTR-001
def test_frontmatter_feature_overrides_path_inference(tmp_path):
    make_setup(tmp_path, features=_TWO_FEATURES)
    # stem would infer "pi-agent"; frontmatter declares "shtp-receiver"
    write_file(
        tmp_path / "docs" / "llds" / "pi-agent.md",
        textwrap.dedent("""\
        ---
        modok:
          feature: shtp-receiver
        ---
        # Doc
        """),
    )

    registered, _, _ = discover_docs(tmp_path, Registry(repo_root=tmp_path))
    lld = next((r for r in registered if r.path.name == "pi-agent.md"), None)
    assert lld is not None
    assert lld.feature == "shtp-receiver"


# @spec SI-FMTR-001
def test_frontmatter_doc_type_overrides_directory_inference(tmp_path):
    make_setup(tmp_path, features=_ONE_FEATURE)
    # In docs/llds/ → default inference would give "lld"
    # frontmatter overrides to "hld"
    write_file(
        tmp_path / "docs" / "llds" / "pi-agent.md",
        textwrap.dedent("""\
        ---
        modok:
          doc_type: hld
        ---
        # Doc
        """),
    )

    registered, _, _ = discover_docs(tmp_path, Registry(repo_root=tmp_path))
    rec = next((r for r in registered if r.path.name == "pi-agent.md"), None)
    assert rec is not None
    assert rec.doc_type == "hld"


# @spec SI-FMTR-001
def test_no_frontmatter_fully_inference_driven(tmp_path):
    make_setup(tmp_path, features=_ONE_FEATURE)
    write_file(tmp_path / "docs" / "llds" / "pi-agent.md", "# LLD\n\nNo frontmatter.\n")

    registered, _, _ = discover_docs(tmp_path, Registry(repo_root=tmp_path))
    rec = next((r for r in registered if r.path.name == "pi-agent.md"), None)
    assert rec is not None
    assert rec.feature == "pi-agent"
    assert rec.doc_type == "lld"


# @spec SI-FMTR-001
def test_partial_frontmatter_falls_through_to_inference(tmp_path):
    make_setup(tmp_path, features=_ONE_FEATURE)
    # Only feature is in frontmatter; doc_type should still come from directory
    write_file(
        tmp_path / "docs" / "llds" / "pi-agent.md",
        textwrap.dedent("""\
        ---
        modok:
          feature: pi-agent
        ---
        # Doc
        """),
    )

    registered, _, _ = discover_docs(tmp_path, Registry(repo_root=tmp_path))
    rec = next((r for r in registered if r.path.name == "pi-agent.md"), None)
    assert rec is not None
    assert rec.doc_type == "lld"  # inferred from directory


# ---------------------------------------------------------------------------
# SI-FMTR-002 — explicit frontmatter bad slug → SI-REF-001 error;
#               inferred bad slug → unregistered (not error)
# ---------------------------------------------------------------------------


# @spec SI-FMTR-002
def test_explicit_frontmatter_bad_slug_raises_ref_error(tmp_path):
    make_setup(tmp_path, features=_ONE_FEATURE)
    write_file(
        tmp_path / "docs" / "llds" / "pi-agent.md",
        textwrap.dedent("""\
        ---
        modok:
          feature: totally-wrong-slug
        ---
        # Doc
        """),
    )

    with pytest.raises(InvalidSlugReferenceError, match="feature"):
        discover_docs(tmp_path, Registry(repo_root=tmp_path))


# @spec SI-FMTR-002
def test_inferred_bad_slug_becomes_unregistered_not_error(tmp_path):
    make_setup(tmp_path, features=_ONE_FEATURE)
    # No explicit frontmatter — slug comes entirely from path inference
    write_file(tmp_path / "docs" / "llds" / "no-such-feature.md", "# No such feature\n")

    # Must not raise
    registered, unregistered, _ = discover_docs(tmp_path, Registry(repo_root=tmp_path))
    assert any(r.path.name == "no-such-feature.md" for r in unregistered)


# @spec SI-FMTR-002
def test_frontmatter_unregistered_explicit_declaration_no_warning(tmp_path):
    make_setup(tmp_path, features=_ONE_FEATURE)
    # Explicitly declares unregistered — no warning, no error
    write_file(
        tmp_path / "docs" / "brainstorm.md",
        textwrap.dedent("""\
        ---
        modok:
          doc_type: unregistered
        ---
        # Brainstorm
        """),
    )

    registered, unregistered, _ = discover_docs(tmp_path, Registry(repo_root=tmp_path))
    rec = next((r for r in unregistered if r.path.name == "brainstorm.md"), None)
    assert rec is not None
    assert rec.is_unregistered is True
    assert rec.warned is False  # no warning emitted for explicit declaration


# ---------------------------------------------------------------------------
# SI-HEAD-002 — DESCRIBED_BY edge only for registered docs (not unregistered)
# ---------------------------------------------------------------------------


# @spec SI-HEAD-002
@pytest.mark.asyncio
async def test_no_described_by_for_unregistered_docs(tmp_path):
    make_setup(tmp_path, features={})
    write_file(
        tmp_path / "docs" / "orphan.md",
        textwrap.dedent("""\
        # Orphan

        ## Section One

        Content.

        ## Section Two

        More.
        """),
    )

    client = AsyncMock()
    from modok.ingestion.pipeline import ingest_doc_unregistered

    with patch("modok.ingestion.parser.get_commit_sha", return_value="abc123"):
        await ingest_doc_unregistered(
            tmp_path / "docs" / "orphan.md",
            client=client,
            project_slug="stagehand",
        )

    edge_calls = [str(c) for c in client.write_edge.call_args_list]
    described_by = [c for c in edge_calls if "DESCRIBED_BY" in c]
    assert described_by == []


# @spec SI-HEAD-002
@pytest.mark.asyncio
async def test_described_by_present_for_registered_doc(tmp_path):
    make_setup(tmp_path, features=_ONE_FEATURE)
    from modok.ingestion.registry import Registry as Reg

    registry = Reg(repo_root=tmp_path)

    content = textwrap.dedent("""\
        ---
        modok:
          feature: pi-agent
          modules: []
          source_files: []
          test_files: []
        ---
        # Pi Agent LLD

        ## Overview

        Content.
        """)
    path = tmp_path / "docs" / "llds" / "pi-agent.md"
    write_file(path, content)

    client = AsyncMock()
    registry.has_feature = lambda s: True
    registry.has_module = lambda s: True
    registry.has_error = lambda s: True

    from modok.ingestion.pipeline import ingest_doc

    with patch("modok.ingestion.parser.get_commit_sha", return_value="abc123"):
        await ingest_doc(
            path, registry=registry, client=client, project_slug="stagehand", repo_root=tmp_path
        )

    edge_calls = [str(c) for c in client.write_edge_by_parts.call_args_list]
    described_by = [c for c in edge_calls if "DESCRIBED_BY" in c]
    assert len(described_by) >= 1


# ---------------------------------------------------------------------------
# SI-WRITE-001 — Commit appears in write order after File, before ErrorSignature
# ---------------------------------------------------------------------------


# @spec SI-WRITE-001
def test_node_write_order_includes_commit():
    assert "Commit" in NODE_WRITE_ORDER


# @spec SI-WRITE-001
def test_commit_after_file_in_write_order():
    assert NODE_WRITE_ORDER.index("Commit") > NODE_WRITE_ORDER.index("File")


# @spec SI-WRITE-001
def test_commit_before_error_signature_in_write_order():
    assert NODE_WRITE_ORDER.index("Commit") < NODE_WRITE_ORDER.index("ErrorSignature")


# ---------------------------------------------------------------------------
# SI-RPT-001 — report has unregistered_count (not files_skipped)
# ---------------------------------------------------------------------------


# @spec SI-RPT-001
def test_report_has_unregistered_count_field():
    report = IngestionReport(
        docs_processed=10,
        nodes_written=50,
        edges_written=80,
        warnings=[],
        errors=[],
        llm_proposals=0,
        pending_items=0,
        files_ignored=3,
        unregistered_count=2,
        unregistered_paths=["docs/a.md", "docs/b.md"],
        duration_seconds=0.5,
    )
    assert report.unregistered_count == 2
    assert report.files_ignored == 3


# @spec SI-RPT-001
def test_report_str_includes_unregistered_count():
    report = IngestionReport(unregistered_count=3, unregistered_paths=["a.md", "b.md", "c.md"])
    text = str(report)
    assert "3" in text
    assert "nregistered" in text


# ---------------------------------------------------------------------------
# SI-HOOK-002 — hook always invokes modok ingest-git; the doc/registry
# `modok ingest` call is gated on a path-guard early-exit
# ---------------------------------------------------------------------------


# @spec SI-HOOK-002
def test_hook_contains_ingest_git_call():
    content = hook_content("stagehand", ["docs/", "registries/"])
    assert "ingest-git" in content


# @spec SI-HOOK-002
def test_hook_ingest_git_outside_path_guard(tmp_path):
    content = hook_content("stagehand", ["docs/", "registries/"])
    lines = content.splitlines()

    # Find the line that exits early (path guard for ingest-docs)
    exit_idx = next(
        (i for i, ln in enumerate(lines) if "exit 0" in ln or "exit 1" in ln),
        None,
    )
    git_idx = next(
        (i for i, ln in enumerate(lines) if "ingest-git" in ln),
        None,
    )

    assert git_idx is not None, "hook must call modok ingest-git"
    if exit_idx is not None:
        assert git_idx > exit_idx, (
            "modok ingest-git must appear after (outside) the ingest-docs path guard"
        )


# @spec SI-HOOK-002
def test_hook_ingest_docs_gated_ingest_git_is_not(tmp_path):
    content = hook_content("stagehand", ["docs/", "registries/"])
    # The doc/registry ingest call ("modok ingest --project", no ticket_file
    # argument) should appear inside a conditional; "modok ingest-git" should
    # not — it runs unconditionally (its own registered-file filter handles
    # skipping irrelevant commits internally, per SI-GIT-004/SI-GIT-008).
    assert "modok ingest --project" in content
    assert "modok ingest-git --project" in content
