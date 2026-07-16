"""
Tests for the Diagnostic Retrieval Engine's recent-CI-test-failure evidence
integration (docs/llds/test-coverage-ci-linking.md § Diagnostic Retrieval
Engine Integration). Written before implementation (Phase 4) — the new
evidence type, traversal function, and DebugPacket field do not exist yet,
so every test here fails with ImportError/AttributeError until Phase 5.

Self-contained (does not import test_dre.py's private helpers), mirroring
tests/test_dre_dependency_evidence.py's pattern for the sibling
`dependency_change` evidence type added earlier this arrow.

Interface assumptions (Phase 5 may adjust):
  - _traverse_test_files_to_recent_failures(test_paths, project_slug, client)
      -> list[dict], each with keys: test_path is NOT included per-item (the
      caller already knows which path it queried) — path, classname,
      test_name, run_id, failure_type, message, observed_at
  - RecentTestFailure dataclass in modok.retrieval.models
  - DebugPacket.recent_test_failures: list[RecentTestFailure]
  - EvidenceItem(type="recent_test_failure", score=9.0) added to
    test_file_evidence BEFORE the covered_tests filtering step

Specs verified: TCLINK-DRE-001 through TCLINK-DRE-005, TCLINK-SCOPE-002.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from modok.quine.models import CustomerIssue


def _issue(project_slug: str = "stagehand", ticket_id: str = "T-001") -> CustomerIssue:
    return CustomerIssue(
        node_type="CustomerIssue",
        project_slug=project_slug,
        source_system="linear",
        ticket_id=ticket_id,
        summary="LONET 2 doesn't work",
        raw_text="LONET 2 doesn't work",
        status="open",
    )


def _failure_row(
    run_id: str = "100",
    classname: str = "tests.test_output_consistency",
    test_name: str = "test_lonet_roundtrip",
    failure_type: str = "AssertionError",
    message: str = "assert False",
    observed_at: str = "2026-07-16T12:00:00Z",
    row_id: int = 700,
) -> list:
    failure = {
        "id": row_id,
        "properties": {
            "node_type": "TestFailure",
            "run_id": run_id,
            "classname": classname,
            "test_name": test_name,
            "failure_type": failure_type,
            "message": message,
            "observed_at": observed_at,
        },
    }
    te = {
        "id": row_id + 1,
        "properties": {
            "node_type": "TestExecution",
            "run_id": run_id,
            "classname": classname,
            "test_name": test_name,
        },
    }
    return [failure, te]


def _side_effect_factory(
    affects_features: list[str] | None = None,
    feature_test_files: dict[str, list[str]] | None = None,
    failures: dict[str, list[list]] | None = None,
):
    affects_features = affects_features or []
    feature_test_files = feature_test_files or {}
    failures = failures or {}

    def _side_effect(cypher: str, params: dict | None = None):
        params = params or {}
        if "AFFECTS" in cypher and "Feature" in cypher:
            return [[slug] for slug in affects_features]
        if "HAS_ERROR" in cypher and "CustomerIssue" in cypher:
            return []
        if "IMPLEMENTED_BY" in cypher:
            return []
        if "HAS_TEST" in cypher:
            slug = params.get("feature_slug", "")
            return [
                [{"properties": {"node_type": "TestFile", "repo_path": p}}]
                for p in feature_test_files.get(slug, [])
            ]
        if "EXECUTES" in cypher:
            path = params.get("file_path") or params.get("path", "")
            return failures.get(path, [])
        return []

    return _side_effect


# ---------------------------------------------------------------------------
# TCLINK-DRE-001 — new evidence type
# ---------------------------------------------------------------------------


# @spec TCLINK-DRE-001
def test_recent_test_failure_not_in_non_corroborating_types():
    from modok.retrieval.engine import _NON_CORROBORATING_TYPES

    assert "recent_test_failure" not in _NON_CORROBORATING_TYPES


# @spec TCLINK-DRE-001, TCLINK-DRE-002
@pytest.mark.asyncio
async def test_covering_test_with_recent_failure_gets_scored_evidence():
    from modok.retrieval.engine import retrieve

    mock_client = AsyncMock()
    mock_client.get_node.return_value = _issue()
    mock_client.query.side_effect = _side_effect_factory(
        affects_features=["lonet-sender"],
        feature_test_files={"lonet-sender": ["client/tests/test_output_consistency.py"]},
        failures={"client/tests/test_output_consistency.py": [_failure_row()]},
    )

    packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)

    by_path = {c.path: c for c in packet.scored_candidates}
    assert "client/tests/test_output_consistency.py" in by_path
    evidence = by_path["client/tests/test_output_consistency.py"].evidence
    matches = [e for e in evidence if e.type == "recent_test_failure"]
    assert len(matches) == 1
    assert matches[0].score == 9.0


# ---------------------------------------------------------------------------
# TCLINK-DRE-002 — promotes out of covered_tests, doesn't stay informational
# ---------------------------------------------------------------------------


# @spec TCLINK-DRE-002
@pytest.mark.asyncio
async def test_covering_test_with_recent_failure_is_not_in_covered_tests():
    from modok.retrieval.engine import retrieve

    mock_client = AsyncMock()
    mock_client.get_node.return_value = _issue()
    mock_client.query.side_effect = _side_effect_factory(
        affects_features=["lonet-sender"],
        feature_test_files={"lonet-sender": ["client/tests/test_output_consistency.py"]},
        failures={"client/tests/test_output_consistency.py": [_failure_row()]},
    )

    packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)

    covered_paths = [ct.path for ct in packet.covered_tests]
    assert "client/tests/test_output_consistency.py" not in covered_paths


# @spec TCLINK-DRE-002
@pytest.mark.asyncio
async def test_covering_test_with_no_failure_still_goes_to_covered_tests():
    """Sibling case, unaffected: a covering test with zero failure evidence
    still moves to covered_tests exactly as DRE-TESTCOV-002 already
    established — this component only adds a new way to earn real evidence,
    it doesn't change what happens with none."""
    from modok.retrieval.engine import retrieve

    mock_client = AsyncMock()
    mock_client.get_node.return_value = _issue()
    mock_client.query.side_effect = _side_effect_factory(
        affects_features=["lonet-sender"],
        feature_test_files={"lonet-sender": ["client/tests/test_no_failures.py"]},
        failures={},
    )

    packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)

    covered_paths = [ct.path for ct in packet.covered_tests]
    assert "client/tests/test_no_failures.py" in covered_paths
    assert "client/tests/test_no_failures.py" not in [c.path for c in packet.scored_candidates]


# ---------------------------------------------------------------------------
# TCLINK-DRE-003 — DebugPacket field on both construction sites
# ---------------------------------------------------------------------------


# @spec TCLINK-DRE-003
@pytest.mark.asyncio
async def test_recent_test_failures_populated_on_partial_and_final_packet():
    from modok.retrieval.engine import retrieve

    mock_client = AsyncMock()
    mock_client.get_node.return_value = _issue()
    mock_client.query.side_effect = _side_effect_factory(
        affects_features=["lonet-sender"],
        feature_test_files={"lonet-sender": ["client/tests/test_output_consistency.py"]},
        failures={"client/tests/test_output_consistency.py": [_failure_row()]},
    )

    partial_packets = []

    def _on_progress(stage, packet):
        if stage == "partial":
            partial_packets.append(packet)

    final = await retrieve(
        issue_id=1, project_slug="stagehand", client=mock_client, on_progress=_on_progress
    )

    assert len(partial_packets) == 1
    assert len(partial_packets[0].recent_test_failures) == 1
    assert len(final.recent_test_failures) == 1
    assert partial_packets[0].recent_test_failures[0].run_id == final.recent_test_failures[0].run_id


# ---------------------------------------------------------------------------
# TCLINK-DRE-004 — mechanical explanation, no LLM
# ---------------------------------------------------------------------------


# @spec TCLINK-DRE-004
@pytest.mark.asyncio
async def test_recent_test_failure_explanation_is_mechanical(_mock_llm_gateway):
    from modok.retrieval.engine import retrieve

    mock_client = AsyncMock()
    mock_client.get_node.return_value = _issue()
    mock_client.query.side_effect = _side_effect_factory(
        affects_features=["lonet-sender"],
        feature_test_files={"lonet-sender": ["client/tests/test_output_consistency.py"]},
        failures={"client/tests/test_output_consistency.py": [_failure_row()]},
    )

    packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)

    assert len(packet.recent_test_failures) == 1
    explanation = packet.recent_test_failures[0].explanation
    assert "test_lonet_roundtrip" in explanation or "tests.test_output_consistency" in explanation
    # No extra LLM call was needed to produce it.
    _mock_llm_gateway.parse_ticket.assert_not_awaited()


# ---------------------------------------------------------------------------
# TCLINK-DRE-005 — scoped to the TestFile only, never propagated to source
# ---------------------------------------------------------------------------


# @spec TCLINK-DRE-005
@pytest.mark.asyncio
async def test_recent_test_failure_does_not_add_evidence_to_source_files():
    from modok.retrieval.engine import retrieve

    mock_client = AsyncMock()
    mock_client.get_node.return_value = _issue()

    def side_effect(cypher, params=None):
        params = params or {}
        if "AFFECTS" in cypher and "Feature" in cypher:
            return [["lonet-sender"]]
        if "HAS_ERROR" in cypher and "CustomerIssue" in cypher:
            return []
        if "IMPLEMENTED_BY" in cypher:
            return [
                [
                    {"properties": {"node_type": "Feature", "feature_slug": "lonet-sender"}},
                    {"properties": {"node_type": "Module", "module_slug": "lonet-sender"}},
                    {"properties": {"node_type": "File", "repo_path": "client/stagehand_client/lonet_sender.py"}},
                ]
            ]
        if "HAS_TEST" in cypher:
            return [[{"properties": {"node_type": "TestFile", "repo_path": "client/tests/test_output_consistency.py"}}]]
        if "EXECUTES" in cypher:
            return [_failure_row()]
        return []

    mock_client.query.side_effect = side_effect

    packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)

    by_path = {c.path: c for c in packet.scored_candidates}
    source_evidence = by_path["client/stagehand_client/lonet_sender.py"].evidence
    assert not any(e.type == "recent_test_failure" for e in source_evidence)


# ---------------------------------------------------------------------------
# TCLINK-SCOPE-002 — no is_current/currently-failing claim
# ---------------------------------------------------------------------------


# @spec TCLINK-SCOPE-002
@pytest.mark.asyncio
async def test_evidence_fires_regardless_of_is_current_field():
    """recent_test_failure evidence must fire on any linked TestFailure —
    this component does not read or depend on TestFailure.is_current
    (reserved, not implemented per continuous-ci-ingestion.md)."""
    from modok.retrieval.engine import retrieve

    mock_client = AsyncMock()
    mock_client.get_node.return_value = _issue()
    row = _failure_row()
    # is_current is deliberately absent from the properties dict — the
    # traversal/evidence logic must not require it to be present at all.
    mock_client.query.side_effect = _side_effect_factory(
        affects_features=["lonet-sender"],
        feature_test_files={"lonet-sender": ["client/tests/test_output_consistency.py"]},
        failures={"client/tests/test_output_consistency.py": [row]},
    )

    packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)

    assert len(packet.recent_test_failures) == 1
