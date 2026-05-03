"""
Tests for modok.retrieval — the Diagnostic Retrieval Engine.
All tests written before implementation (Phase 5). Every test cites
the EARS spec it verifies via @spec annotation.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from modok.llm.errors import LLMResponseError, LLMUnavailableError
from modok.llm.models import TicketParseResult
from modok.quine.models import CustomerIssue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_customer_issue(
    project_slug: str = "stagehand",
    source_system: str = "linear",
    ticket_id: str = "T-001",
    summary: str = "Pose dropout after USB reset",
    raw_text: str | None = "Tracker loses tracking after USB reset on Windows",
    status: str = "open",
) -> CustomerIssue:
    return CustomerIssue(
        node_type="CustomerIssue",
        project_slug=project_slug,
        source_system=source_system,
        ticket_id=ticket_id,
        summary=summary,
        raw_text=raw_text,
        status=status,
    )


def make_ticket_parse_result(
    feature_slug: str | None = "shtp-receiver",
    error_signatures: list[str] | None = None,
    symptoms: list[str] | None = None,
) -> TicketParseResult:
    return TicketParseResult(
        feature_slug=feature_slug,
        error_signatures=error_signatures or ["shtp-version-mismatch"],
        environment={},
        symptoms=symptoms or ["pose dropout"],
        confidence=0.8,
        raw_response="{}",
    )


# ---------------------------------------------------------------------------
# Interface and Project Isolation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_raises_not_found_when_issue_missing():
    # @spec DRE-IFACE-001, DRE-ERR-001
    from modok.retrieval.engine import retrieve
    from modok.retrieval.errors import DRENotFoundError

    mock_client = AsyncMock()
    mock_client.get_node.side_effect = Exception("not found")

    with pytest.raises(DRENotFoundError):
        await retrieve(issue_id=999, project_slug="stagehand", client=mock_client)


@pytest.mark.asyncio
async def test_raises_not_found_when_project_slug_mismatch():
    # @spec DRE-IFACE-001, DRE-ERR-002
    from modok.retrieval.engine import retrieve
    from modok.retrieval.errors import DRENotFoundError

    issue = make_customer_issue(project_slug="other-project")
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue

    with pytest.raises(DRENotFoundError):
        await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)


@pytest.mark.asyncio
async def test_no_queries_run_after_project_slug_mismatch():
    # @spec DRE-IFACE-002
    # Behavioral: project_slug mismatch must abort before any traversal query fires.
    from modok.retrieval.engine import retrieve
    from modok.retrieval.errors import DRENotFoundError

    issue = make_customer_issue(project_slug="other-project")
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue

    with pytest.raises(DRENotFoundError):
        await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)

    mock_client.query.assert_not_called()


@pytest.mark.asyncio
async def test_raises_graph_unavailable_when_quine_unreachable():
    # @spec DRE-IFACE-003, DRE-ERR-004
    from modok.retrieval.engine import retrieve
    from modok.retrieval.errors import DREGraphUnavailableError

    mock_client = AsyncMock()
    mock_client.get_node.side_effect = ConnectionError("quine down")

    with pytest.raises(DREGraphUnavailableError):
        await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)


# ---------------------------------------------------------------------------
# Anchor Extraction — graph-first
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_uses_graph_feature_anchors_skips_llm():
    # @spec DRE-ANCH-001, DRE-ANCH-003
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue()
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=["shtp-receiver"],
        has_errors=[],
    )

    with patch("modok.retrieval.engine.gateway") as mock_gw:
        await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)
        mock_gw.parse_ticket.assert_not_called()


@pytest.mark.asyncio
async def test_uses_graph_error_anchors_skips_llm():
    # @spec DRE-ANCH-002, DRE-ANCH-003
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue()
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=[],
        has_errors=["shtp-version-mismatch"],
    )

    with patch("modok.retrieval.engine.gateway") as mock_gw:
        await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)
        mock_gw.parse_ticket.assert_not_called()


@pytest.mark.asyncio
async def test_graph_anchors_from_different_project_do_not_count():
    # @spec DRE-ANCH-003
    # Sufficiency is evaluated after project-scoped filtering: zero project-matching
    # anchors triggers LLM fallback even if cross-project edges exist in the DB.
    # The mock returns empty results for all project-scoped queries, simulating
    # the case where the graph has edges but none match this project.
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue(raw_text="Tracker loses tracking")
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=[],
        has_errors=[],
    )

    with patch("modok.retrieval.engine.gateway") as mock_gw:
        mock_gw.parse_ticket = AsyncMock(return_value=make_ticket_parse_result())
        await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)
        mock_gw.parse_ticket.assert_called_once()


@pytest.mark.asyncio
async def test_falls_back_to_llm_when_no_graph_anchors():
    # @spec DRE-ANCH-004
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue(raw_text="Tracker loses tracking")
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=[],
        has_errors=[],
    )

    with patch("modok.retrieval.engine.gateway") as mock_gw:
        mock_gw.parse_ticket = AsyncMock(return_value=make_ticket_parse_result())
        packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)
        mock_gw.parse_ticket.assert_called_once_with(
            "Tracker loses tracking", "stagehand", backend="local",
            valid_slugs=None, feature_slugs=[], module_slugs=None,
        )
        assert (
            "shtp-receiver" in packet.anchors.feature_slugs
            or "shtp-version-mismatch" in packet.anchors.error_signatures
        )


@pytest.mark.asyncio
async def test_raises_anchor_error_when_no_graph_anchors_and_no_raw_text():
    # @spec DRE-ANCH-005, DRE-ERR-003
    from modok.retrieval.engine import retrieve
    from modok.retrieval.errors import DREAnchorError

    issue = make_customer_issue(raw_text=None)
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=[],
        has_errors=[],
    )

    with pytest.raises(DREAnchorError):
        await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)


@pytest.mark.asyncio
async def test_raises_anchor_error_when_llm_response_error():
    # @spec DRE-ANCH-006, DRE-ERR-003
    from modok.retrieval.engine import retrieve
    from modok.retrieval.errors import DREAnchorError

    issue = make_customer_issue(raw_text="some text")
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=[],
        has_errors=[],
    )

    with patch("modok.retrieval.engine.gateway") as mock_gw:
        mock_gw.parse_ticket = AsyncMock(side_effect=LLMResponseError("bad json"))
        with pytest.raises(DREAnchorError):
            await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)


@pytest.mark.asyncio
async def test_raises_llm_unavailable_when_gateway_unreachable():
    # @spec DRE-ANCH-007, DRE-ERR-005
    from modok.retrieval.engine import retrieve
    from modok.retrieval.errors import DRELLMUnavailableError

    issue = make_customer_issue(raw_text="some text")
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=[],
        has_errors=[],
    )

    with patch("modok.retrieval.engine.gateway") as mock_gw:
        mock_gw.parse_ticket = AsyncMock(side_effect=LLMUnavailableError("timeout"))
        with pytest.raises(DRELLMUnavailableError):
            await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)


@pytest.mark.asyncio
async def test_symptoms_in_anchor_set_not_in_packet_results():
    # @spec DRE-ANCH-008
    # Behavioral: symptoms surface in anchors.symptoms but produce no known_issues,
    # relevant_files, or recent_fixes entries of their own.
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue(raw_text="Tracker drops out")
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=[],
        has_errors=[],
    )

    with patch("modok.retrieval.engine.gateway") as mock_gw:
        result = make_ticket_parse_result(
            feature_slug=None,
            error_signatures=["shtp-err"],
            symptoms=["pose dropout", "jitter"],
        )
        mock_gw.parse_ticket = AsyncMock(return_value=result)
        packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)

    assert packet.anchors.symptoms == ["pose dropout", "jitter"]
    # Symptoms must not appear as anchor_types in evidence
    for ev in packet.evidence:
        assert ev.anchor_type != "symptom"


# ---------------------------------------------------------------------------
# Graph Traversal
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_feature_anchor_traverses_to_files():
    # @spec DRE-TRAV-001
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue()
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=["shtp-receiver"],
        has_errors=[],
        feature_files={"shtp-receiver": ["agent/src/shtp.c", "agent/src/shtp.h"]},
    )

    packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)
    file_paths = [f.repo_path for f in packet.relevant_files]
    assert "agent/src/shtp.c" in file_paths
    assert "agent/src/shtp.h" in file_paths


@pytest.mark.asyncio
async def test_error_anchor_traverses_to_known_issues():
    # @spec DRE-TRAV-002
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue()
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=[],
        has_errors=["shtp-version-mismatch"],
        error_known_issues={"shtp-version-mismatch": [("KI-001", "SHTP v1 packets rejected", "open")]},
    )

    packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)
    ki_ids = [ki.known_issue_id for ki in packet.known_issues]
    assert "KI-001" in ki_ids


@pytest.mark.asyncio
async def test_known_issue_traverses_to_fixes():
    # @spec DRE-TRAV-003
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue()
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=[],
        has_errors=["shtp-version-mismatch"],
        error_known_issues={"shtp-version-mismatch": [("KI-001", "SHTP v1 rejected", "open")]},
        ki_fixes={"KI-001": [("FIX-001", "Upgrade to SHTP v2", "patch")]},
    )

    packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)
    fix_ids = [f.fix_id for f in packet.recent_fixes]
    assert "FIX-001" in fix_ids


@pytest.mark.asyncio
async def test_similarity_match_candidate_included():
    # @spec DRE-TRAV-004
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue()
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=["shtp-receiver"],
        has_errors=[],
        similarity_matches=[("KI-002", "candidate")],
    )

    packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)
    ki_ids = [ki.known_issue_id for ki in packet.known_issues]
    assert "KI-002" in ki_ids


@pytest.mark.asyncio
async def test_similarity_match_confirmed_included():
    # @spec DRE-TRAV-004
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue()
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=["shtp-receiver"],
        has_errors=[],
        similarity_matches=[("KI-003", "confirmed")],
    )

    packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)
    ki_ids = [ki.known_issue_id for ki in packet.known_issues]
    assert "KI-003" in ki_ids


@pytest.mark.asyncio
async def test_similarity_match_rejected_excluded():
    # @spec DRE-TRAV-004
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue()
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=["shtp-receiver"],
        has_errors=[],
        similarity_matches=[("KI-004", "rejected")],
    )

    packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)
    ki_ids = [ki.known_issue_id for ki in packet.known_issues]
    assert "KI-004" not in ki_ids


@pytest.mark.asyncio
async def test_all_traversal_queries_include_project_slug():
    # @spec DRE-TRAV-005
    # Checks that project_slug appears in every query's params dict.
    # Note: does not catch slug interpolated directly into Cypher strings.
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue()
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=["shtp-receiver"],
        has_errors=["shtp-version-mismatch"],
    )

    await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)

    assert mock_client.query.call_count > 0, "Expected at least one traversal query"
    for call in mock_client.query.call_args_list:
        args, kwargs = call
        params = kwargs.get("params") or (args[1] if len(args) > 1 else {})
        assert params.get("project_slug") == "stagehand", (
            f"Query params missing project_slug: {params}"
        )


@pytest.mark.asyncio
async def test_ki_to_fixes_cypher_filters_by_project_slug():
    # @spec DRE-TRAV-005
    # Structural: the Cypher for _traverse_ki_to_fixes must use $project_slug
    # as a property filter on Fix nodes (not just pass it in params unused).
    from modok.retrieval import engine

    import inspect
    src = inspect.getsource(engine._traverse_ki_to_fixes)
    assert "project_slug" in src, "_traverse_ki_to_fixes must reference project_slug in Cypher"
    # The property filter must appear in the MATCH pattern or WHERE clause on Fix
    assert (
        "Fix {project_slug" in src or "fix.project_slug" in src or
        "Fix{project_slug" in src
    ), (
        "_traverse_ki_to_fixes Cypher does not filter Fix by project_slug: "
        + src
    )


@pytest.mark.asyncio
async def test_ki_to_fixes_excludes_fixes_from_other_project():
    # @spec DRE-TRAV-005
    # Behavioral: _traverse_ki_to_fixes returns empty when Quine (simulated by mock)
    # finds no Fix matching the requested project_slug — foreign-project Fixes excluded.
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue()
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    # Mock simulates Quine filtering: RESOLVED_BY returns no results when
    # the Cypher's project_slug filter would exclude the foreign Fix.
    mock_client.query.side_effect = _make_query_side_effect_cross_project_fix(
        has_errors=["err-a"],
        error_known_issues={"err-a": [("KI-001", "Issue A", "open")]},
        ki_fixes_cross_project={"KI-001": [("FIX-FOREIGN", "Foreign fix", "patch")]},
    )

    packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)
    fix_ids = [f.fix_id for f in packet.recent_fixes]
    assert "FIX-FOREIGN" not in fix_ids, (
        "_traverse_ki_to_fixes returned a Fix from a different project"
    )


@pytest.mark.asyncio
async def test_similarity_cypher_filters_by_project_slug():
    # @spec DRE-TRAV-005
    # Structural: the Cypher for _traverse_similarity must use $project_slug
    # as a property filter on KnownIssue nodes.
    from modok.retrieval import engine

    import inspect
    src = inspect.getsource(engine._traverse_similarity)
    assert "project_slug" in src, "_traverse_similarity must reference project_slug in Cypher"
    assert (
        "KnownIssue {project_slug" in src or "ki.project_slug" in src or
        "KnownIssue{project_slug" in src
    ), (
        "_traverse_similarity Cypher does not filter KnownIssue by project_slug: "
        + src
    )


@pytest.mark.asyncio
async def test_similarity_excludes_known_issues_from_other_project():
    # @spec DRE-TRAV-005
    # Behavioral: _traverse_similarity returns empty when Quine (simulated by mock)
    # finds no KnownIssue matching the requested project_slug.
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue()
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect_cross_project_similarity(
        affects_features=["shtp-receiver"],
        similarity_ki_foreign=[("KI-FOREIGN", "candidate")],
    )

    packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)
    ki_ids = [ki.known_issue_id for ki in packet.known_issues]
    assert "KI-FOREIGN" not in ki_ids, (
        "_traverse_similarity returned a KnownIssue from a different project"
    )


# ---------------------------------------------------------------------------
# Weighted Match Count and Prioritization
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_item_matched_by_two_anchors_has_match_count_2():
    # @spec DRE-SCORE-001
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue()
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=[],
        has_errors=["err-a", "err-b"],
        error_known_issues={
            "err-a": [("KI-001", "Issue A", "open")],
            "err-b": [("KI-001", "Issue A", "open")],
        },
    )

    packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)
    ki = next(k for k in packet.known_issues if k.known_issue_id == "KI-001")
    assert ki.match_count == 2


@pytest.mark.asyncio
async def test_confirmed_similarity_adds_2_to_match_count():
    # @spec DRE-SCORE-002
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue()
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=[],
        has_errors=["err-a"],
        error_known_issues={"err-a": [("KI-001", "Issue A", "open")]},
        similarity_matches=[("KI-001", "confirmed")],
    )

    packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)
    ki = next(k for k in packet.known_issues if k.known_issue_id == "KI-001")
    assert ki.match_count == 3  # 1 from anchor + 2 from confirmed


@pytest.mark.asyncio
async def test_candidate_similarity_adds_1_to_match_count():
    # @spec DRE-SCORE-002
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue()
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=[],
        has_errors=["err-a"],
        error_known_issues={"err-a": [("KI-001", "Issue A", "open")]},
        similarity_matches=[("KI-001", "candidate")],
    )

    packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)
    ki = next(k for k in packet.known_issues if k.known_issue_id == "KI-001")
    assert ki.match_count == 2  # 1 from anchor + 1 from candidate


@given(
    counts=st.lists(
        st.integers(min_value=1, max_value=20),
        min_size=2,
        max_size=15,
        unique=False,
    )
)
@settings(max_examples=200)
def test_sort_and_cap_descending_order(counts):
    # @spec DRE-SCORE-003, DRE-SCORE-005
    # Invariant: for any input, _sort_and_cap produces a non-increasing sequence.
    from modok.retrieval.engine import _sort_and_cap

    items = [{"id": f"item-{i}", "match_count": c} for i, c in enumerate(counts)]
    result = _sort_and_cap(items, cap=len(counts) + 1)
    result_counts = [r["match_count"] for r in result]
    assert result_counts == sorted(result_counts, reverse=True)


@given(
    counts=st.lists(
        st.integers(min_value=1, max_value=20),
        min_size=1,
        max_size=30,
    ),
    cap=st.integers(min_value=1, max_value=15),
)
@settings(max_examples=200)
def test_sort_and_cap_respects_cap_and_keeps_highest(counts, cap):
    # @spec DRE-SCORE-004
    # After capping, every retained item has match_count >= every dropped item.
    from modok.retrieval.engine import _sort_and_cap

    items = [{"id": f"item-{i}", "match_count": c} for i, c in enumerate(counts)]
    result = _sort_and_cap(items, cap=cap)

    assert len(result) <= cap
    if len(result) == cap and len(items) > cap:
        min_retained = min(r["match_count"] for r in result)
        dropped_counts = sorted(counts, reverse=True)[cap:]
        for dropped in dropped_counts:
            assert min_retained >= dropped


@pytest.mark.asyncio
async def test_fix_match_count_summed_across_known_issues():
    # @spec DRE-SCORE-006
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue()
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=[],
        has_errors=["err-a", "err-b"],
        error_known_issues={
            "err-a": [("KI-001", "Issue A", "open")],
            "err-b": [("KI-002", "Issue B", "open")],
        },
        ki_fixes={
            "KI-001": [("FIX-001", "Apply SHTP v2", "patch")],
            "KI-002": [("FIX-001", "Apply SHTP v2", "patch")],
        },
    )

    packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)
    fix = next(f for f in packet.recent_fixes if f.fix_id == "FIX-001")
    assert fix.match_count == 2


@given(
    n_ki=st.integers(min_value=1, max_value=10),
)
@settings(max_examples=50)
def test_fix_match_count_equals_hop_count(n_ki):
    # @spec DRE-SCORE-006
    # Property: a Fix reached by N KnownIssue hops has match_count == N.
    # Tests the accumulator logic in isolation via _accumulate_match_count.
    from modok.retrieval.engine import _accumulate_match_count

    counts: dict[str, int] = {}
    for _ in range(n_ki):
        _accumulate_match_count(counts, "FIX-001", 1)
    assert counts["FIX-001"] == n_ki


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_confidence_is_matched_over_total():
    # @spec DRE-CONF-001
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue()
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    # 2 anchors: 1 feature (produces files) + 1 error (produces nothing)
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=["shtp-receiver"],
        has_errors=["unknown-error"],
        feature_files={"shtp-receiver": ["agent/src/shtp.c"]},
        error_known_issues={},
    )

    packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)
    assert packet.anchor_count == 2
    assert packet.confidence == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_confidence_zero_when_llm_returns_no_anchors():
    # @spec DRE-CONF-002
    # When LLM extraction succeeds but returns zero anchor instances,
    # the DRE raises DREAnchorError (no anchors to traverse with).
    from modok.retrieval.engine import retrieve
    from modok.retrieval.errors import DREAnchorError

    issue = make_customer_issue(raw_text="vague complaint")
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=[],
        has_errors=[],
    )

    empty_result = TicketParseResult(
        feature_slug=None,
        error_signatures=[],
        environment={},
        symptoms=[],
        confidence=0.0,
        raw_response="{}",
    )

    with patch("modok.retrieval.engine.gateway") as mock_gw:
        mock_gw.parse_ticket = AsyncMock(return_value=empty_result)
        with pytest.raises(DREAnchorError):
            await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)


@given(
    n_features=st.integers(min_value=0, max_value=5),
    n_errors=st.integers(min_value=0, max_value=5),
    n_matched=st.integers(min_value=0, max_value=10),
)
@settings(max_examples=200)
def test_confidence_always_in_range(n_features, n_errors, n_matched):
    # @spec DRE-CONF-003
    # For any anchor count and match count, confidence is in [0.0, 1.0].
    from modok.retrieval.engine import _compute_confidence

    total = n_features + n_errors
    matched = min(n_matched, total)  # can't match more than total
    if total == 0:
        confidence = _compute_confidence(matched=0, total=0)
    else:
        confidence = _compute_confidence(matched=matched, total=total)
    assert isinstance(confidence, float)
    assert 0.0 <= confidence <= 1.0


# ---------------------------------------------------------------------------
# Debug Packet Structure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_packet_contains_all_required_fields():
    # @spec DRE-PKT-001
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue()
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=["shtp-receiver"],
        has_errors=[],
    )

    packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)
    for field in ("issue_summary", "anchors", "anchor_count", "known_issues",
                  "recent_fixes", "relevant_files", "evidence", "confidence"):
        assert hasattr(packet, field), f"DebugPacket missing field: {field}"


@pytest.mark.asyncio
async def test_empty_sections_are_lists_not_omitted():
    # @spec DRE-PKT-002
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue()
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=["shtp-receiver"],
        has_errors=[],
        feature_files={"shtp-receiver": []},
    )

    packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)
    assert packet.known_issues == []
    assert packet.recent_fixes == []
    assert packet.relevant_files == []


@pytest.mark.asyncio
async def test_issue_summary_from_customer_issue_node():
    # @spec DRE-PKT-003
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue(summary="Tracker drops out after USB reset")
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=["feat-a"],
        has_errors=[],
    )

    packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)
    assert packet.issue_summary == "Tracker drops out after USB reset"


@pytest.mark.asyncio
async def test_evidence_contains_anchor_and_matched_node_ids():
    # @spec DRE-PKT-004
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue()
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=[],
        has_errors=["shtp-version-mismatch"],
        error_known_issues={"shtp-version-mismatch": [("KI-001", "SHTP issue", "open")]},
    )

    packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)
    assert len(packet.evidence) >= 1
    ev = next(e for e in packet.evidence if e.anchor_value == "shtp-version-mismatch")
    assert ev.anchor_type == "error_signature"
    assert len(ev.matched_node_ids) >= 1


@pytest.mark.asyncio
async def test_anchor_count_equals_total_anchor_instances():
    # @spec DRE-PKT-005
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue()
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=["feat-a"],
        has_errors=["err-a", "err-b"],
    )

    packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)
    assert packet.anchor_count == 3  # 1 feature + 2 errors


# ---------------------------------------------------------------------------
# Write Boundary
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retrieve_does_not_call_write_methods():
    # @spec DRE-WRITE-001
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue()
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=["feat-a"],
        has_errors=[],
    )

    await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)

    mock_client.upsert_node.assert_not_called()
    mock_client.write_edge.assert_not_called()
    mock_client.replace_edges.assert_not_called()


@pytest.mark.asyncio
async def test_retrieve_does_not_write_to_disk(tmp_path, monkeypatch):
    # @spec DRE-WRITE-002
    import builtins
    from modok.retrieval.engine import retrieve

    original_open = builtins.open
    opened_files: list[str] = []

    def tracking_open(file, mode="r", *args, **kwargs):
        if "w" in str(mode) or "a" in str(mode) or "x" in str(mode):
            opened_files.append(str(file))
        return original_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", tracking_open)

    issue = make_customer_issue()
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=["feat-a"],
        has_errors=[],
    )

    await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)

    assert opened_files == [], f"retrieve() wrote to disk: {opened_files}"


@pytest.mark.asyncio
async def test_retrieve_uses_query_not_traverse():
    # @spec DRE-TRAV-006
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue()
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=["feat-a"],
        has_errors=[],
    )

    await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)

    mock_client.traverse.assert_not_called()


# ---------------------------------------------------------------------------
# Query side-effect helper
# ---------------------------------------------------------------------------

def _make_query_side_effect(
    affects_features: list[str] | None = None,
    has_errors: list[str] | None = None,
    feature_files: dict[str, list[str]] | None = None,
    error_known_issues: dict[str, list[tuple[str, str, str]]] | None = None,
    ki_fixes: dict[str, list[tuple[str, str, str]]] | None = None,
    similarity_matches: list[tuple[str, str]] | None = None,
):
    """
    Returns a side_effect for mock_client.query that dispatches on Cypher keywords.
    Each call returns a list of Quine result rows matching the query type.
    """
    affects_features = affects_features or []
    has_errors = has_errors or []
    feature_files = feature_files or {}
    error_known_issues = error_known_issues or {}
    ki_fixes = ki_fixes or {}
    similarity_matches = similarity_matches or []

    # Build a stable integer-ID → issue_id map for KnownIssue nodes.
    # KI nodes get IDs starting at 1000 to avoid collisions with other node types.
    # This map lets the RESOLVED_BY traversal dispatch on the integer node ID
    # that the error→KI traversal returns, matching real Quine behaviour.
    _ki_node_id_map: dict[int, str] = {}  # quine_node_id → issue_id string
    _next_ki_id = 1000
    for kis in error_known_issues.values():
        for (kid, _, _) in kis:
            if not any(v == kid for v in _ki_node_id_map.values()):
                _ki_node_id_map[_next_ki_id] = kid
                _next_ki_id += 1
    _ki_issue_id_to_node_id = {v: k for k, v in _ki_node_id_map.items()}

    def _side_effect(cypher: str, params: dict | None = None):
        params = params or {}
        proj = params.get("project_slug", "stagehand")

        if "AFFECTS" in cypher and "Feature" in cypher:
            return [
                [{"id": i, "properties": {
                    "feature_slug": slug, "project_slug": proj,
                    "node_type": "Feature", "name": slug,
                }}]
                for i, slug in enumerate(affects_features)
            ]

        if "HAS_ERROR" in cypher and "ErrorSignature" in cypher and "CustomerIssue" in cypher:
            return [
                [{"id": i, "properties": {
                    "normalized_error": err, "project_slug": proj,
                    "node_type": "ErrorSignature", "display_text": err,
                }}]
                for i, err in enumerate(has_errors)
            ]

        if "IMPLEMENTED_BY" in cypher:
            slug = params.get("feature_slug", "")
            files = feature_files.get(slug, [])
            return [
                [{"id": i, "properties": {
                    "repo_path": path, "project_slug": proj, "node_type": "File",
                }}]
                for i, path in enumerate(files)
            ]

        if "HAS_ERROR" in cypher and "KnownIssue" in cypher:
            err = params.get("normalized_error", "")
            kis = error_known_issues.get(err, [])
            return [
                [{"id": _ki_issue_id_to_node_id.get(kid, 1000 + i), "properties": {
                    "issue_id": kid, "summary": summary, "status": status,
                    "project_slug": proj, "node_type": "KnownIssue",
                }}]
                for i, (kid, summary, status) in enumerate(kis)
            ]

        if "RESOLVED_BY" in cypher:
            # Dispatch on integer Quine node ID — matches real traversal behaviour.
            ki_node_id = params.get("ki_node_id")
            kid = _ki_node_id_map.get(ki_node_id, "")
            fixes = ki_fixes.get(kid, [])
            return [
                [{"id": i, "properties": {
                    "fix_id": fid, "summary": summary, "kind": kind,
                    "project_slug": proj, "node_type": "Fix",
                }}]
                for i, (fid, summary, kind) in enumerate(fixes)
            ]

        if "HAS_SIMILARITY_MATCH" in cypher:
            non_rejected = [(kid, s) for kid, s in similarity_matches if s != "rejected"]
            return [
                [
                    {"id": i, "properties": {
                        "issue_id": kid, "summary": f"Similar {kid}", "status": "open",
                        "project_slug": proj, "node_type": "KnownIssue",
                    }},
                    {"review_status": status},
                ]
                for i, (kid, status) in enumerate(non_rejected)
            ]

        return []

    return _side_effect


def _make_query_side_effect_cross_project_fix(
    has_errors: list[str],
    error_known_issues: dict[str, list[tuple[str, str, str]]],
    ki_fixes_cross_project: dict[str, list[tuple[str, str, str]]],
):
    """Side-effect where _traverse_ki_to_fixes returns Fix nodes tagged with a
    different project_slug — verifying the Cypher WHERE filters them out."""
    _ki_issue_id_to_node_id: dict[str, int] = {}
    _ki_node_id_map: dict[int, str] = {}
    _next_id = 1000
    for kis in error_known_issues.values():
        for (kid, _, _) in kis:
            if kid not in _ki_issue_id_to_node_id:
                _ki_issue_id_to_node_id[kid] = _next_id
                _ki_node_id_map[_next_id] = kid
                _next_id += 1

    def _side_effect(cypher: str, params: dict | None = None):
        params = params or {}
        proj = params.get("project_slug", "stagehand")

        if "AFFECTS" in cypher and "Feature" in cypher:
            return []

        if "HAS_ERROR" in cypher and "ErrorSignature" in cypher and "CustomerIssue" in cypher:
            return [
                [{"id": i, "properties": {
                    "normalized_error": err, "project_slug": proj,
                    "node_type": "ErrorSignature", "display_text": err,
                }}]
                for i, err in enumerate(has_errors)
            ]

        if "IMPLEMENTED_BY" in cypher:
            return []

        if "HAS_ERROR" in cypher and "KnownIssue" in cypher:
            err = params.get("normalized_error", "")
            kis = error_known_issues.get(err, [])
            return [
                [{"id": _ki_issue_id_to_node_id.get(kid, 1000 + i), "properties": {
                    "issue_id": kid, "summary": summary, "status": status,
                    "project_slug": proj, "node_type": "KnownIssue",
                }}]
                for i, (kid, summary, status) in enumerate(kis)
            ]

        if "RESOLVED_BY" in cypher:
            ki_node_id = params.get("ki_node_id")
            kid = _ki_node_id_map.get(ki_node_id, "")
            fixes = ki_fixes_cross_project.get(kid, [])
            # Simulate Quine filtering: Fix nodes belong to OTHER-PROJECT;
            # with the correct Cypher filter ({project_slug: $project_slug}),
            # Quine returns nothing. We simulate that by checking the cypher.
            if "Fix {project_slug" in cypher or "Fix{project_slug" in cypher:
                return []  # Quine filters out cross-project Fix nodes
            # Without the filter in Cypher, Quine would return them (the old bug).
            return [
                [{"id": i, "properties": {
                    "fix_id": fid, "summary": summary, "kind": kind,
                    "project_slug": "OTHER-PROJECT",
                    "node_type": "Fix",
                }}]
                for i, (fid, summary, kind) in enumerate(fixes)
            ]

        if "HAS_SIMILARITY_MATCH" in cypher:
            return []

        return []

    return _side_effect


def _make_query_side_effect_cross_project_similarity(
    affects_features: list[str],
    similarity_ki_foreign: list[tuple[str, str]],
):
    """Side-effect where _traverse_similarity returns KnownIssue nodes tagged with
    a different project_slug — verifying the Cypher WHERE filters them out."""

    def _side_effect(cypher: str, params: dict | None = None):
        params = params or {}
        proj = params.get("project_slug", "stagehand")

        if "AFFECTS" in cypher and "Feature" in cypher:
            return [
                [{"id": i, "properties": {
                    "feature_slug": slug, "project_slug": proj,
                    "node_type": "Feature", "name": slug,
                }}]
                for i, slug in enumerate(affects_features)
            ]

        if "HAS_ERROR" in cypher and "ErrorSignature" in cypher and "CustomerIssue" in cypher:
            return []

        if "IMPLEMENTED_BY" in cypher:
            return []

        if "HAS_ERROR" in cypher and "KnownIssue" in cypher:
            return []

        if "RESOLVED_BY" in cypher:
            return []

        if "HAS_SIMILARITY_MATCH" in cypher:
            # Simulate Quine filtering: KI nodes belong to OTHER-PROJECT;
            # with the correct Cypher filter ({project_slug: $project_slug}),
            # Quine returns nothing. We simulate that by inspecting the cypher.
            if "KnownIssue {project_slug" in cypher or "KnownIssue{project_slug" in cypher:
                return []  # Quine filters out cross-project KnownIssue nodes
            # Without the filter in Cypher, Quine would return them (the old bug).
            return [
                [
                    {"id": i, "properties": {
                        "issue_id": kid, "summary": f"Foreign {kid}", "status": "open",
                        "project_slug": "OTHER-PROJECT",
                        "node_type": "KnownIssue",
                    }},
                    {"review_status": status},
                ]
                for i, (kid, status) in enumerate(similarity_ki_foreign)
            ]

        return []

    return _side_effect
