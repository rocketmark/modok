"""
Tests for version-fidelity source resolution, snapshot diffing, and edge
writing (docs/llds/dependency-graph-ingestion.md § Data Sources and Priority,
§ Snapshot Diffing, § Graph Model and Deterministic IDs — Edges). Written
before implementation (Phase 4) — the module these tests target does not
exist yet, so every test here fails with ImportError until Phase 5.

Module name assumption: src/modok/ingestion/dependency_ingestion.py.
Phase 5 may rename; the behavioral requirements (DEPG-SRC-*, DEPG-DIFF-*,
DEPG-EDGE-*) do not depend on the exact module path.

Interface assumptions:
  - parse_dependabot_bump_title(title: str, package_name: str) -> tuple[str, str] | None
  - resolve_version_for_change(package_name, raw_from, raw_to, *, review_data,
    is_dependabot, pr_title) -> tuple[from_version, to_version, relationship, version_source]
  - find_prior_snapshot(client, project_slug, manifest_path, captured_at) -> dict | None
  - diff_manifest_packages(prior: dict[str, str] | None, new: dict[str, str]) -> list[dict]
      each dict: {"package": str, "change_kind": "added"|"removed"|"changed",
                  "from": str | None, "to": str | None}
  - write_dependency_snapshot(client, project_slug, manifest_path, commit_sha,
    captured_at, packages: dict[str, str], ecosystem="pypi") -> None
  - write_dependency_change(client, project_slug, manifest_path, commit_sha,
    package_name, ecosystem, change_kind, from_version, to_version,
    relationship, version_source, fix_id=None) -> None

Specs verified: DEPG-SRC-001 through DEPG-SRC-004, DEPG-DIFF-002 through
DEPG-DIFF-005, DEPG-EDGE-001 through DEPG-EDGE-006.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st


def _mock_client(node_exists: bool = True) -> MagicMock:
    client = MagicMock()
    client.upsert_node = AsyncMock()
    client.write_edge_by_parts = AsyncMock()
    client.node_exists_by_parts = AsyncMock(return_value=node_exists)
    client.replace_edges_by_parts = AsyncMock()
    client.query = AsyncMock(return_value=[])
    return client


# ---------------------------------------------------------------------------
# DEPG-SRC-001/002 — version-fidelity source priority
# ---------------------------------------------------------------------------


# @spec DEPG-SRC-001
def test_dependency_review_result_wins_when_available():
    from modok.ingestion.dependency_ingestion import resolve_version_for_change

    from_v, to_v, relationship, source = resolve_version_for_change(
        "bleak",
        raw_from=">=0.21.0",
        raw_to=">=0.22.0",
        review_data={"bleak": {"version": "0.22.0", "relationship": "direct"}},
        is_dependabot=True,
        pr_title="Bump bleak from 0.21.0 to 0.22.0",
    )
    assert (from_v, to_v) == (">=0.21.0", "0.22.0")
    assert relationship == "direct"
    assert source == "dependency_review"


# @spec DEPG-SRC-001, DEPG-SRC-002
def test_dependabot_title_used_when_review_data_unavailable():
    from modok.ingestion.dependency_ingestion import resolve_version_for_change

    from_v, to_v, relationship, source = resolve_version_for_change(
        "bleak",
        raw_from=">=0.21.0",
        raw_to=">=0.22.0",
        review_data={},  # source 1 unavailable/empty for this PR
        is_dependabot=True,
        pr_title="Bump bleak from 0.21.0 to 0.22.0",
    )
    assert (from_v, to_v) == ("0.21.0", "0.22.0")
    assert source == "dependabot_title"
    assert relationship == "unknown"


# @spec DEPG-SRC-001
def test_raw_manifest_text_used_when_neither_source_resolves():
    from modok.ingestion.dependency_ingestion import resolve_version_for_change

    from_v, to_v, relationship, source = resolve_version_for_change(
        "bleak",
        raw_from=">=0.21.0",
        raw_to=">=0.22.0",
        review_data={},
        is_dependabot=False,
        pr_title="Manually bump bleak",
    )
    assert (from_v, to_v) == (">=0.21.0", ">=0.22.0")
    assert source == "manifest_diff"


# @spec DEPG-SRC-002
def test_dependency_review_error_does_not_raise_only_falls_through():
    """resolve_version_for_change never raises on missing/partial review_data —
    an unavailable source-1 response is represented as an empty dict by the
    caller (§ fetch layer), not a special exception this function must catch."""
    from modok.ingestion.dependency_ingestion import resolve_version_for_change

    result = resolve_version_for_change(
        "numpy",
        raw_from="==1.24",
        raw_to="==1.25",
        review_data={},
        is_dependabot=False,
        pr_title="",
    )
    assert result[3] == "manifest_diff"


# ---------------------------------------------------------------------------
# DEPG-SRC-003 — Dependabot title parsing
# ---------------------------------------------------------------------------


# @spec DEPG-SRC-003
def test_parse_dependabot_bump_title_extracts_exact_versions():
    from modok.ingestion.dependency_ingestion import parse_dependabot_bump_title

    result = parse_dependabot_bump_title("Bump bleak from 0.21.0 to 0.22.0", "bleak")
    assert result == ("0.21.0", "0.22.0")


# @spec DEPG-SRC-003
def test_parse_dependabot_bump_title_case_insensitive_package_match():
    from modok.ingestion.dependency_ingestion import parse_dependabot_bump_title

    result = parse_dependabot_bump_title("Bump Bleak from 0.21.0 to 0.22.0", "bleak")
    assert result == ("0.21.0", "0.22.0")


# @spec DEPG-SRC-003
def test_parse_dependabot_bump_title_returns_none_for_grouped_update():
    from modok.ingestion.dependency_ingestion import parse_dependabot_bump_title

    result = parse_dependabot_bump_title(
        "Bump the pip group across 1 directory with 3 updates", "bleak"
    )
    assert result is None


# @spec DEPG-SRC-003
def test_parse_dependabot_bump_title_returns_none_for_different_package():
    from modok.ingestion.dependency_ingestion import parse_dependabot_bump_title

    result = parse_dependabot_bump_title("Bump numpy from 1.24.0 to 1.25.0", "bleak")
    assert result is None


# @spec DEPG-SRC-003
def test_dependabot_title_not_consulted_for_non_dependabot_pr():
    from modok.ingestion.dependency_ingestion import resolve_version_for_change

    # Even though the title happens to match the Bump pattern, is_dependabot=False
    # means source 2 must not be trusted (a human could coincidentally title a PR
    # this way without it being an actual Dependabot-resolved version).
    from_v, to_v, relationship, source = resolve_version_for_change(
        "bleak",
        raw_from=">=0.21.0",
        raw_to=">=0.22.0",
        review_data={},
        is_dependabot=False,
        pr_title="Bump bleak from 0.21.0 to 0.22.0",
    )
    assert source == "manifest_diff"
    assert (from_v, to_v) == (">=0.21.0", ">=0.22.0")


# ---------------------------------------------------------------------------
# DEPG-SRC-004 — detection independent of source 1/2 availability
# ---------------------------------------------------------------------------


# @spec DEPG-SRC-004
def test_diff_detects_a_change_even_when_enrichment_sources_absent():
    from modok.ingestion.dependency_ingestion import diff_manifest_packages

    prior = {"bleak": ">=0.21.0"}
    new = {"bleak": ">=0.22.0"}
    diff = diff_manifest_packages(prior, new)
    assert len(diff) == 1
    assert diff[0]["change_kind"] == "changed"


# ---------------------------------------------------------------------------
# DEPG-DIFF-002 — prior snapshot lookup ordering
# ---------------------------------------------------------------------------


# @spec DEPG-DIFF-002
@pytest.mark.asyncio
async def test_find_prior_snapshot_orders_by_captured_at_then_commit_sha():
    from modok.ingestion.dependency_ingestion import find_prior_snapshot

    client = _mock_client()
    await find_prior_snapshot(client, "stagehand", "client/requirements.txt", "2026-07-16T13:00:00Z")

    query_text = client.query.call_args[0][0]
    assert "captured_at" in query_text
    assert "commit_sha" in query_text
    assert "DESC" in query_text.upper()


# @spec DEPG-DIFF-002
@pytest.mark.asyncio
async def test_find_prior_snapshot_returns_none_when_no_rows():
    from modok.ingestion.dependency_ingestion import find_prior_snapshot

    client = _mock_client()
    client.query = AsyncMock(return_value=[])
    result = await find_prior_snapshot(client, "stagehand", "client/requirements.txt", "2026-07-16T13:00:00Z")
    assert result is None


# ---------------------------------------------------------------------------
# DEPG-DIFF-003 — first snapshot never produces changes
# ---------------------------------------------------------------------------


# @spec DEPG-DIFF-003
def test_no_prior_snapshot_produces_no_changes():
    from modok.ingestion.dependency_ingestion import diff_manifest_packages

    new = {"bleak": ">=0.22.0", "numpy": "==1.24.0", "cbor2": ">=5.4.6,<6"}
    assert diff_manifest_packages(None, new) == []


# ---------------------------------------------------------------------------
# DEPG-DIFF-004 — added/removed/changed/unchanged classification [P]
# ---------------------------------------------------------------------------


# @spec DEPG-DIFF-004
def test_diff_classifies_added_removed_changed_and_unchanged():
    from modok.ingestion.dependency_ingestion import diff_manifest_packages

    prior = {"bleak": ">=0.21.0", "numpy": "==1.24.0", "zeroconf": ">=0.149.16"}
    new = {"bleak": ">=0.22.0", "zeroconf": ">=0.149.16", "cbor2": ">=5.4.6,<6"}
    diff = {d["package"]: d for d in diff_manifest_packages(prior, new)}

    assert diff["bleak"]["change_kind"] == "changed"
    assert diff["bleak"]["from"] == ">=0.21.0"
    assert diff["bleak"]["to"] == ">=0.22.0"
    assert diff["numpy"]["change_kind"] == "removed"
    assert diff["numpy"]["to"] is None
    assert diff["cbor2"]["change_kind"] == "added"
    assert diff["cbor2"]["from"] is None
    assert "zeroconf" not in diff  # unchanged — no record


# @spec DEPG-DIFF-004
@given(
    shared=st.dictionaries(st.text(min_size=1, max_size=8), st.text(min_size=1, max_size=8), max_size=5)
)
@settings(max_examples=25)
def test_diffing_identical_snapshots_never_produces_changes(shared):
    from modok.ingestion.dependency_ingestion import diff_manifest_packages

    assert diff_manifest_packages(dict(shared), dict(shared)) == []


# ---------------------------------------------------------------------------
# DEPG-DIFF-005 — idempotent re-diffing [P]
# ---------------------------------------------------------------------------


# @spec DEPG-DIFF-005
def test_diffing_same_pair_twice_produces_identical_results():
    from modok.ingestion.dependency_ingestion import diff_manifest_packages

    prior = {"bleak": ">=0.21.0"}
    new = {"bleak": ">=0.22.0"}
    first = diff_manifest_packages(prior, new)
    second = diff_manifest_packages(dict(prior), dict(new))
    assert first == second


# ---------------------------------------------------------------------------
# DEPG-EDGE-001/002 — VERSION_OF, CONTAINS
# ---------------------------------------------------------------------------


# @spec DEPG-EDGE-001, DEPG-EDGE-002
@pytest.mark.asyncio
async def test_write_dependency_snapshot_writes_version_of_and_contains():
    from modok.ingestion.dependency_ingestion import write_dependency_snapshot

    client = _mock_client()
    await write_dependency_snapshot(
        client,
        "stagehand",
        "client/requirements.txt",
        commit_sha="merge123",
        captured_at="2026-07-16T12:00:00Z",
        packages={"bleak": ">=0.22.0"},
    )
    calls = [str(c) for c in client.write_edge_by_parts.call_args_list]
    assert any("VERSION_OF" in c for c in calls)
    assert any("CONTAINS" in c for c in calls)


# ---------------------------------------------------------------------------
# DEPG-EDGE-003 — FOR_COMMIT gated on Commit existence
# ---------------------------------------------------------------------------


# @spec DEPG-EDGE-003
@pytest.mark.asyncio
async def test_for_commit_edge_skipped_when_commit_absent():
    from modok.ingestion.dependency_ingestion import write_dependency_snapshot

    client = _mock_client(node_exists=False)
    await write_dependency_snapshot(
        client,
        "stagehand",
        "client/requirements.txt",
        commit_sha="merge123",
        captured_at="2026-07-16T12:00:00Z",
        packages={"bleak": ">=0.22.0"},
    )
    calls = [str(c) for c in client.write_edge_by_parts.call_args_list]
    assert not any("FOR_COMMIT" in c for c in calls)


# @spec DEPG-EDGE-003
@pytest.mark.asyncio
async def test_for_commit_edge_written_when_commit_present():
    from modok.ingestion.dependency_ingestion import write_dependency_snapshot

    client = _mock_client(node_exists=True)
    await write_dependency_snapshot(
        client,
        "stagehand",
        "client/requirements.txt",
        commit_sha="merge123",
        captured_at="2026-07-16T12:00:00Z",
        packages={"bleak": ">=0.22.0"},
    )
    calls = [str(c) for c in client.write_edge_by_parts.call_args_list]
    assert any("FOR_COMMIT" in c for c in calls)


# ---------------------------------------------------------------------------
# DEPG-EDGE-004 — FROM_VERSION/TO_VERSION omission by change_kind
# ---------------------------------------------------------------------------


# @spec DEPG-EDGE-004
@pytest.mark.asyncio
async def test_added_change_omits_from_version():
    from modok.ingestion.dependency_ingestion import write_dependency_change

    client = _mock_client()
    await write_dependency_change(
        client, "stagehand", "client/requirements.txt", commit_sha="merge123",
        package_name="cbor2", ecosystem="pypi", change_kind="added",
        from_version=None, to_version="5.4.6", relationship="direct",
        version_source="manifest_diff",
    )
    calls = [str(c) for c in client.write_edge_by_parts.call_args_list]
    assert any("TO_VERSION" in c for c in calls)
    assert not any("FROM_VERSION" in c for c in calls)


# @spec DEPG-EDGE-004
@pytest.mark.asyncio
async def test_removed_change_omits_to_version():
    from modok.ingestion.dependency_ingestion import write_dependency_change

    client = _mock_client()
    await write_dependency_change(
        client, "stagehand", "client/requirements.txt", commit_sha="merge123",
        package_name="numpy", ecosystem="pypi", change_kind="removed",
        from_version="1.24.0", to_version=None, relationship="direct",
        version_source="manifest_diff",
    )
    calls = [str(c) for c in client.write_edge_by_parts.call_args_list]
    assert any("FROM_VERSION" in c for c in calls)
    assert not any("TO_VERSION" in c for c in calls)


# @spec DEPG-EDGE-004
@pytest.mark.asyncio
async def test_changed_change_writes_both_from_and_to_version():
    from modok.ingestion.dependency_ingestion import write_dependency_change

    client = _mock_client()
    await write_dependency_change(
        client, "stagehand", "client/requirements.txt", commit_sha="merge123",
        package_name="bleak", ecosystem="pypi", change_kind="changed",
        from_version="0.21.0", to_version="0.22.0", relationship="direct",
        version_source="dependabot_title",
    )
    calls = [str(c) for c in client.write_edge_by_parts.call_args_list]
    assert any("FROM_VERSION" in c for c in calls)
    assert any("TO_VERSION" in c for c in calls)


# ---------------------------------------------------------------------------
# DEPG-EDGE-005 — INTRODUCED_BY / MERGED_VIA gated, never invent
# ---------------------------------------------------------------------------


# @spec DEPG-EDGE-005
@pytest.mark.asyncio
async def test_introduced_by_and_merged_via_skipped_when_targets_absent():
    from modok.ingestion.dependency_ingestion import write_dependency_change

    client = _mock_client(node_exists=False)
    await write_dependency_change(
        client, "stagehand", "client/requirements.txt", commit_sha="merge123",
        package_name="bleak", ecosystem="pypi", change_kind="changed",
        from_version="0.21.0", to_version="0.22.0", relationship="direct",
        version_source="dependabot_title", fix_id="gh-77",
    )
    calls = [str(c) for c in client.write_edge_by_parts.call_args_list]
    assert not any("INTRODUCED_BY" in c for c in calls)
    assert not any("MERGED_VIA" in c for c in calls)


# @spec DEPG-EDGE-005
@pytest.mark.asyncio
async def test_introduced_by_and_merged_via_written_when_targets_present():
    from modok.ingestion.dependency_ingestion import write_dependency_change

    client = _mock_client(node_exists=True)
    await write_dependency_change(
        client, "stagehand", "client/requirements.txt", commit_sha="merge123",
        package_name="bleak", ecosystem="pypi", change_kind="changed",
        from_version="0.21.0", to_version="0.22.0", relationship="direct",
        version_source="dependabot_title", fix_id="gh-77",
    )
    calls = [str(c) for c in client.write_edge_by_parts.call_args_list]
    assert any("INTRODUCED_BY" in c for c in calls)
    assert any("MERGED_VIA" in c for c in calls)


# @spec DEPG-EDGE-005
@pytest.mark.asyncio
async def test_no_fix_id_means_no_merged_via_attempted():
    from modok.ingestion.dependency_ingestion import write_dependency_change

    client = _mock_client(node_exists=True)
    await write_dependency_change(
        client, "stagehand", "client/requirements.txt", commit_sha="merge123",
        package_name="bleak", ecosystem="pypi", change_kind="changed",
        from_version="0.21.0", to_version="0.22.0", relationship="direct",
        version_source="manifest_diff", fix_id=None,
    )
    calls = [str(c) for c in client.write_edge_by_parts.call_args_list]
    assert not any("MERGED_VIA" in c for c in calls)


# ---------------------------------------------------------------------------
# DEPG-EDGE-006 — DECLARES reconciled to latest snapshot's CONTAINS set
# ---------------------------------------------------------------------------


# @spec DEPG-EDGE-006
@pytest.mark.asyncio
async def test_declares_reconciled_via_replace_edges_by_parts():
    from modok.ingestion.dependency_ingestion import write_dependency_snapshot

    client = _mock_client()
    await write_dependency_snapshot(
        client,
        "stagehand",
        "client/requirements.txt",
        commit_sha="merge123",
        captured_at="2026-07-16T12:00:00Z",
        packages={"bleak": ">=0.22.0", "numpy": "==1.24.0"},
    )
    assert client.replace_edges_by_parts.await_count == 1
    call = client.replace_edges_by_parts.call_args
    from_parts, edge_type, to_parts_list = call[0]
    assert "dependency-manifest" in from_parts
    assert edge_type == "DECLARES"
    assert len(to_parts_list) == 2
