"""
Tests for the Diagnostic Retrieval Engine's dependency-change evidence
integration (docs/llds/dependency-graph-ingestion.md § Existing Retrieval
Integration). Written before implementation (Phase 4) — the new evidence
type, traversal function, and DebugPacket field do not exist yet, so every
test here fails with ImportError/AttributeError until Phase 5.

This file is self-contained (does not import test_dre.py's private
_make_query_side_effect helper) — it builds its own minimal Cypher-keyword
dispatcher covering only what these tests need: AFFECTS (feature anchor),
IMPLEMENTED_BY (feature -> files), and USES_DEPENDENCY/CHANGED_PACKAGE
(the new dependency-change traversal).

Specs verified: DEPG-DRE-001 through DEPG-DRE-006.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from modok.quine.models import CustomerIssue


def _issue(project_slug: str = "stagehand", ticket_id: str = "T-001") -> CustomerIssue:
    return CustomerIssue(
        node_type="CustomerIssue",
        project_slug=project_slug,
        source_system="linear",
        ticket_id=ticket_id,
        summary="BLE provisioning drops after firmware update",
        raw_text="BLE provisioning drops after firmware update",
        status="open",
    )


def _dependency_change_row(
    package_purl: str = "pkg:pypi/bleak",
    from_version: str | None = "0.21.0",
    to_version: str = "0.22.0",
    commit_sha: str | None = "merge123",
    fix_id: str | None = "gh-77",
    change_id: int = 500,
) -> list:
    dc = {
        "id": change_id,
        "properties": {
            "node_type": "DependencyChange",
            "manifest_path": "client/requirements.txt",
            "change_kind": "changed" if from_version else "added",
        },
    }
    pkg = {"id": change_id + 1, "properties": {"node_type": "DependencyPackage", "purl": package_purl, "name": "bleak"}}
    fv = (
        {"id": change_id + 2, "properties": {"node_type": "DependencyVersion", "version": from_version}}
        if from_version
        else None
    )
    tv = {"id": change_id + 3, "properties": {"node_type": "DependencyVersion", "version": to_version}}
    c = {"id": change_id + 4, "properties": {"node_type": "Commit", "sha": commit_sha}} if commit_sha else None
    fix = {"id": change_id + 5, "properties": {"node_type": "Fix", "fix_id": fix_id}} if fix_id else None
    return [dc, pkg, fv, tv, c, fix]


def _side_effect_factory(
    affects_features: list[str] | None = None,
    feature_files: dict[str, list[str]] | None = None,
    dependency_changes: dict[str, list[list]] | None = None,
):
    affects_features = affects_features or []
    feature_files = feature_files or {}
    dependency_changes = dependency_changes or {}

    def _side_effect(cypher: str, params: dict | None = None):
        params = params or {}
        if "AFFECTS" in cypher and "Feature" in cypher:
            return [[slug] for slug in affects_features]
        if "HAS_ERROR" in cypher and "CustomerIssue" in cypher:
            return []
        if "IMPLEMENTED_BY" in cypher:
            slug = params.get("feature_slug", "")
            files = feature_files.get(slug, [])
            feat = {"id": 0, "properties": {"node_type": "Feature", "feature_slug": slug}}
            mod = {"id": 1, "properties": {"node_type": "Module", "module_slug": slug}}
            return [
                [feat, mod, {"id": i + 2, "properties": {"node_type": "File", "repo_path": p}}]
                for i, p in enumerate(files)
            ]
        if "USES_DEPENDENCY" in cypher:
            file_path = params.get("file_path", "")
            return dependency_changes.get(file_path, [])
        return []

    return _side_effect


# ---------------------------------------------------------------------------
# DEPG-DRE-001 — new evidence type, corroborating
# ---------------------------------------------------------------------------


# @spec DEPG-DRE-001
def test_dependency_change_not_in_non_corroborating_types():
    from modok.retrieval.engine import _NON_CORROBORATING_TYPES

    assert "dependency_change" not in _NON_CORROBORATING_TYPES


# @spec DEPG-DRE-001
@pytest.mark.asyncio
async def test_dependency_change_evidence_has_flat_score():
    from modok.retrieval.engine import retrieve

    mock_client = AsyncMock()
    mock_client.get_node.return_value = _issue()
    mock_client.query.side_effect = _side_effect_factory(
        affects_features=["wifi-provisioning"],
        feature_files={"wifi-provisioning": ["client/stagehand_client/stagehand_ble.py"]},
        dependency_changes={
            "client/stagehand_client/stagehand_ble.py": [_dependency_change_row()]
        },
    )

    packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)

    by_path = {c.path: c for c in packet.scored_candidates}
    evidence = by_path["client/stagehand_client/stagehand_ble.py"].evidence
    dep_items = [e for e in evidence if e.type == "dependency_change"]
    assert len(dep_items) == 1
    assert dep_items[0].score == 5.0


# ---------------------------------------------------------------------------
# DEPG-DRE-002 — never discovers new files
# ---------------------------------------------------------------------------


# @spec DEPG-DRE-002
@pytest.mark.asyncio
async def test_dependency_change_never_adds_a_file_not_already_anchored():
    from modok.retrieval.engine import retrieve

    mock_client = AsyncMock()
    mock_client.get_node.return_value = _issue()
    # The feature anchor resolves to one file. A dependency change exists for
    # a *different* file that this ticket's feature resolution never reaches.
    mock_client.query.side_effect = _side_effect_factory(
        affects_features=["wifi-provisioning"],
        feature_files={"wifi-provisioning": ["client/stagehand_client/stagehand_ble.py"]},
        dependency_changes={
            "client/stagehand_client/lighthouse_ble.py": [_dependency_change_row()]
        },
    )

    packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)

    paths = {c.path for c in packet.scored_candidates}
    assert "client/stagehand_client/lighthouse_ble.py" not in paths
    assert paths == {"client/stagehand_client/stagehand_ble.py"}


# ---------------------------------------------------------------------------
# DEPG-DRE-003 — traversal dedup, no recency sort/cap
# ---------------------------------------------------------------------------


# @spec DEPG-DRE-003
@pytest.mark.asyncio
async def test_traversal_deduplicates_by_dependency_change_id():
    from modok.retrieval.engine import _traverse_files_to_recent_dependency_changes

    mock_client = AsyncMock()
    row = _dependency_change_row(change_id=500)
    mock_client.query = AsyncMock(return_value=[row, row])  # same change returned twice

    result = await _traverse_files_to_recent_dependency_changes(
        ["client/stagehand_client/stagehand_ble.py"], "stagehand", mock_client
    )
    assert len(result) == 1


# @spec DEPG-DRE-003
@pytest.mark.asyncio
async def test_traversal_does_not_cap_at_ten_like_recent_commits():
    from modok.retrieval.engine import _traverse_files_to_recent_dependency_changes

    mock_client = AsyncMock()
    rows = [_dependency_change_row(change_id=100 * i) for i in range(1, 15)]
    mock_client.query = AsyncMock(return_value=rows)

    result = await _traverse_files_to_recent_dependency_changes(
        ["client/stagehand_client/stagehand_ble.py"], "stagehand", mock_client
    )
    assert len(result) == 14


# ---------------------------------------------------------------------------
# DEPG-DRE-004 — populated on both DebugPacket construction sites
# ---------------------------------------------------------------------------


# @spec DEPG-DRE-004
@pytest.mark.asyncio
async def test_recent_dependency_changes_populated_on_partial_and_final_packet():
    from modok.retrieval.engine import retrieve

    mock_client = AsyncMock()
    mock_client.get_node.return_value = _issue()
    mock_client.query.side_effect = _side_effect_factory(
        affects_features=["wifi-provisioning"],
        feature_files={"wifi-provisioning": ["client/stagehand_client/stagehand_ble.py"]},
        dependency_changes={
            "client/stagehand_client/stagehand_ble.py": [_dependency_change_row()]
        },
    )

    partial_packets = []

    def _on_progress(stage, packet):
        if stage == "partial":
            partial_packets.append(packet)

    final = await retrieve(
        issue_id=1, project_slug="stagehand", client=mock_client, on_progress=_on_progress
    )

    assert len(partial_packets) == 1
    assert len(partial_packets[0].recent_dependency_changes) == 1
    assert len(final.recent_dependency_changes) == 1
    assert partial_packets[0].recent_dependency_changes[0].package == final.recent_dependency_changes[0].package


# ---------------------------------------------------------------------------
# DEPG-DRE-005 — mechanical explanation, no LLM call
# ---------------------------------------------------------------------------


# @spec DEPG-DRE-005
def test_dependency_change_explanation_is_a_mechanical_template():
    from modok.retrieval.engine import _format_dependency_change_explanation

    text = _format_dependency_change_explanation(
        package="pkg:pypi/bleak",
        from_version="0.21.0",
        to_version="0.22.0",
        manifest_path="client/requirements.txt",
        files=["client/stagehand_client/stagehand_ble.py"],
    )
    assert "bleak" in text
    assert "0.21.0" in text
    assert "0.22.0" in text
    assert "client/requirements.txt" in text
    assert "client/stagehand_client/stagehand_ble.py" in text


# @spec DEPG-DRE-005
@pytest.mark.asyncio
async def test_retrieve_with_dependency_evidence_does_not_call_llm_extra_times(_mock_llm_gateway):
    from modok.retrieval.engine import retrieve

    mock_client = AsyncMock()
    mock_client.get_node.return_value = _issue()
    mock_client.query.side_effect = _side_effect_factory(
        affects_features=["wifi-provisioning"],
        feature_files={"wifi-provisioning": ["client/stagehand_client/stagehand_ble.py"]},
        dependency_changes={
            "client/stagehand_client/stagehand_ble.py": [_dependency_change_row()]
        },
    )

    await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)

    # Only the existing summarise_packet call — no additional LLM call was
    # introduced to produce the RecentDependencyChange explanation text.
    assert _mock_llm_gateway.summarise_packet.await_count == 1
    _mock_llm_gateway.parse_ticket.assert_not_awaited()


# ---------------------------------------------------------------------------
# DEPG-DRE-006 — recency-independence property [P]
# ---------------------------------------------------------------------------


# @spec DEPG-DRE-006
@given(observed_at=st.text(alphabet="0123456789T:-Z", min_size=10, max_size=20))
@settings(max_examples=15)
def test_unrelated_dependency_change_never_reaches_this_ticket_regardless_of_recency(observed_at):
    """The mock dispatcher only returns rows for the exact file_path Quine
    would filter on (WHERE id(f) = idFrom('file', project_slug, file_path)) —
    an unrelated file's DependencyChange structurally cannot appear for this
    ticket's evidence, no matter what observed_at value it carries."""
    import asyncio

    from modok.retrieval.engine import retrieve

    async def _run():
        mock_client = AsyncMock()
        mock_client.get_node.return_value = _issue()
        row = _dependency_change_row(change_id=900)
        row[0]["properties"]["observed_at"] = observed_at
        mock_client.query.side_effect = _side_effect_factory(
            affects_features=["wifi-provisioning"],
            feature_files={"wifi-provisioning": ["client/stagehand_client/stagehand_ble.py"]},
            dependency_changes={"some/other/unrelated_file.py": [row]},
        )
        packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)
        # The unrelated file was never anchored, so it never became a
        # file_path the traversal queried against — regardless of observed_at.
        assert packet.recent_dependency_changes == []

    asyncio.run(_run())
