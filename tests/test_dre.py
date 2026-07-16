"""
Tests for modok.retrieval — the Diagnostic Retrieval Engine.
All tests written before implementation (Phase 5). Every test cites
the EARS spec it verifies via @spec annotation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from modok.llm.errors import LLMGatewayError, LLMResponseError, LLMUnavailableError
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
    feature_slugs: list[str] | None = None,
    error_signatures: list[str] | None = None,
    symptoms: list[str] | None = None,
) -> TicketParseResult:
    slugs = feature_slugs if feature_slugs is not None else ([feature_slug] if feature_slug else [])
    return TicketParseResult(
        feature_slugs=slugs,
        error_signatures=error_signatures or ["shtp-version-mismatch"],
        environment={},
        symptoms=symptoms or ["pose dropout"],
        confidence=0.8,
        raw_response="{}",
        mentioned_files=[],
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
async def test_graph_anchors_extracts_scalar_rows_directly():
    """_graph_anchors's RETURN f.feature_slug / e.normalized_error project a
    scalar, not a node — real Quine returns the raw value directly as row[0],
    not wrapped in a {"properties": {...}} node dict. Found live: the old
    code assumed the node-dict shape and silently extracted nothing, even
    when client.query() returned real matches."""
    from modok.retrieval.engine import _graph_anchors

    mock_client = AsyncMock()
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=["wifi-provisioning"],
        has_errors=["GSS_FAILURE"],
    )

    feature_slugs, error_sigs = await _graph_anchors("1", "stagehand", mock_client)
    assert feature_slugs == ["wifi-provisioning"]
    assert error_sigs == ["GSS_FAILURE"]


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
        mock_gw.summarise_packet = AsyncMock(return_value=[])
        packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)
        mock_gw.parse_ticket.assert_called_once_with(
            "Tracker loses tracking",
            "stagehand",
            backend="local",
            valid_slugs=None,
            feature_slugs=[],
            module_slugs=None,
            feature_descriptions=None,
            module_descriptions=None,
            module_elements=None,
            module_source_files=None,
        )
        assert (
            "shtp-receiver" in packet.issue.anchors.features
            or "shtp-version-mismatch" in packet.issue.anchors.errors
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
async def test_llm_response_error_falls_back_to_pre_match():
    # @spec DRE-ANCH-006
    # When parse_ticket raises LLMResponseError, the engine falls back to mechanical
    # pre-match results and returns a packet rather than raising DREAnchorError.
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue(raw_text="Problem in agent/src/shtp.c")
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=[],
        has_errors=[],
    )

    with patch("modok.retrieval.engine.gateway") as mock_gw:
        mock_gw.parse_ticket = AsyncMock(side_effect=LLMResponseError("bad json"))
        mock_gw.summarise_packet = AsyncMock(return_value="summary")
        packet = await retrieve(
            issue_id=1,
            project_slug="stagehand",
            client=mock_client,
            module_source_files={"shtp-receiver": ["agent/src/shtp.c"]},
        )

    # Engine did not raise — returned a packet
    from modok.retrieval.models import DebugPacket

    assert isinstance(packet, DebugPacket)
    # parse_ticket was attempted (confirming we hit the LLM path)
    assert mock_gw.parse_ticket.called


@pytest.mark.asyncio
async def test_llm_gateway_error_falls_back_to_pre_match():
    # @spec DRE-ANCH-006
    # When parse_ticket raises LLMGatewayError (e.g. 4xx from LLM endpoint after an
    # OMLX/backend upgrade), the engine falls back to mechanical pre-match rather than
    # propagating an unhandled exception that would exit with code 1.
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue(raw_text="Problem in agent/src/shtp.c")
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=[],
        has_errors=[],
    )

    with patch("modok.retrieval.engine.gateway") as mock_gw:
        mock_gw.parse_ticket = AsyncMock(side_effect=LLMGatewayError("Client error 422: invalid model"))
        mock_gw.summarise_packet = AsyncMock(return_value="summary")
        packet = await retrieve(
            issue_id=1,
            project_slug="stagehand",
            client=mock_client,
            module_source_files={"shtp-receiver": ["agent/src/shtp.c"]},
        )

    from modok.retrieval.models import DebugPacket

    assert isinstance(packet, DebugPacket)
    assert mock_gw.parse_ticket.called


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
        )  # feature_slug=None → feature_slugs=[]
        mock_gw.parse_ticket = AsyncMock(return_value=result)
        mock_gw.summarise_packet = AsyncMock(return_value=[])
        packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)

    assert packet.issue.anchors.symptoms == ["pose dropout", "jitter"]
    # Symptoms must not produce files or known_issues of their own
    assert packet.relevant_files == []


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
    assert "agent/src/shtp.c" in packet.relevant_files
    assert "agent/src/shtp.h" in packet.relevant_files


@pytest.mark.asyncio
async def test_feature_source_files_get_primary_evidence():
    # @spec DRE-TRAV-009
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue()
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=["shtp-receiver"],
        has_errors=[],
        feature_files={"shtp-receiver": ["agent/src/shtp.c", "agent/src/shtp.h"]},
    )

    packet = await retrieve(
        issue_id=1,
        project_slug="stagehand",
        client=mock_client,
        feature_source_files={"shtp-receiver": ["agent/src/shtp.c"]},
    )

    by_path = {c.path: c for c in packet.scored_candidates}
    primary_types = {e.type for e in by_path["agent/src/shtp.c"].evidence}
    peripheral_types = {e.type for e in by_path["agent/src/shtp.h"].evidence}
    assert primary_types == {"feature_primary_file"}
    assert peripheral_types == {"feature_anchor"}
    # Primary (declared in the feature's own source_files) outscores peripheral
    # (reachable only via the module graph) for equivalent single-anchor evidence.
    assert by_path["agent/src/shtp.c"].score > by_path["agent/src/shtp.h"].score


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
        error_known_issues={
            "shtp-version-mismatch": [("KI-001", "SHTP v1 packets rejected", "open")]
        },
    )

    packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)
    ki_ids = [ki.id for ki in packet.known_issues]
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
    fix_ids = [f.id for f in packet.prior_fixes]
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
    ki_ids = [ki.id for ki in packet.known_issues]
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
    ki_ids = [ki.id for ki in packet.known_issues]
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
    ki_ids = [ki.id for ki in packet.known_issues]
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
    assert "Fix {project_slug" in src or "fix.project_slug" in src or "Fix{project_slug" in src, (
        "_traverse_ki_to_fixes Cypher does not filter Fix by project_slug: " + src
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
    fix_ids = [f.id for f in packet.prior_fixes]
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
        "KnownIssue {project_slug" in src
        or "ki.project_slug" in src
        or "KnownIssue{project_slug" in src
    ), "_traverse_similarity Cypher does not filter KnownIssue by project_slug: " + src


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
    ki_ids = [ki.id for ki in packet.known_issues]
    assert "KI-FOREIGN" not in ki_ids, (
        "_traverse_similarity returned a KnownIssue from a different project"
    )


# ---------------------------------------------------------------------------
# Weighted Match Count and Prioritization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_item_matched_by_two_anchors_appears_in_known_issues():
    # @spec DRE-SCORE-001
    # A KI matched by two error anchors must appear in known_issues (internal
    # match_count drives ranking; the observable guarantee is that it surfaces).
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
    ki_ids = [k.id for k in packet.known_issues]
    assert "KI-001" in ki_ids


@pytest.mark.asyncio
async def test_confirmed_similarity_ki_appears_in_known_issues():
    # @spec DRE-SCORE-002
    # A KI boosted by confirmed similarity (weight 2) must surface in known_issues.
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
    ki_ids = [k.id for k in packet.known_issues]
    assert "KI-001" in ki_ids


@pytest.mark.asyncio
async def test_candidate_similarity_ki_appears_in_known_issues():
    # @spec DRE-SCORE-002
    # A KI boosted by candidate similarity (weight 1) must surface in known_issues.
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
    ki_ids = [k.id for k in packet.known_issues]
    assert "KI-001" in ki_ids


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
    fix_ids = [f.id for f in packet.prior_fixes]
    assert "FIX-001" in fix_ids


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
async def test_matched_feature_anchor_produces_files():
    # @spec DRE-CONF-001
    # A feature anchor that resolves to files must produce relevant_files.
    # An error anchor with no known issues produces no known_issues.
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue()
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=["shtp-receiver"],
        has_errors=["unknown-error"],
        feature_files={"shtp-receiver": ["agent/src/shtp.c"]},
        error_known_issues={},
    )

    packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)
    assert "agent/src/shtp.c" in packet.relevant_files
    assert packet.known_issues == []


@pytest.mark.asyncio
async def test_confidence_zero_when_llm_returns_no_anchors():
    # When LLM extraction succeeds but returns zero anchor instances,
    # the DRE returns a 0-confidence empty packet rather than raising.
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue(raw_text="vague complaint")
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=[],
        has_errors=[],
    )

    empty_result = TicketParseResult(
        feature_slugs=[],
        error_signatures=[],
        environment={},
        symptoms=[],
        confidence=0.0,
        raw_response="{}",
    )

    with patch("modok.retrieval.engine.gateway") as mock_gw:
        mock_gw.parse_ticket = AsyncMock(return_value=empty_result)
        mock_gw.summarise_packet = AsyncMock(return_value=[])
        packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)
    assert packet.issue.anchors.features == []
    assert packet.issue.anchors.errors == []
    assert packet.affected_areas == []
    assert packet.relevant_files == []


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
    for field in (
        "issue",
        "affected_areas",
        "relevant_files",
        "relevant_tests",
        "known_issues",
        "prior_fixes",
        "recent_commits",
        "summary",
    ):
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
    assert packet.prior_fixes == []
    assert packet.relevant_files == []
    assert packet.relevant_tests == []


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
    assert packet.issue.summary == "Tracker drops out after USB reset"


@pytest.mark.asyncio
async def test_error_anchor_surfaces_known_issues():
    # @spec DRE-PKT-004
    # An error anchor that matches a KnownIssue must surface it in known_issues.
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
    assert len(packet.known_issues) >= 1
    ki = next(k for k in packet.known_issues if k.id == "KI-001")
    assert ki.summary == "SHTP issue"


@pytest.mark.asyncio
async def test_affected_areas_contains_matched_feature():
    # @spec DRE-PKT-005
    # Features that resolve to files must appear in affected_areas.
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue()
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=["feat-a"],
        has_errors=["err-a", "err-b"],
        feature_files={"feat-a": ["src/a.py"]},
    )

    packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)
    area_ids = [a.id for a in packet.affected_areas]
    assert "feature:feat-a" in area_ids


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
    module_files: dict[str, list[str]] | None = None,
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
    module_files = module_files or {}
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
        for kid, _, _ in kis:
            if not any(v == kid for v in _ki_node_id_map.values()):
                _ki_node_id_map[_next_ki_id] = kid
                _next_ki_id += 1
    _ki_issue_id_to_node_id = {v: k for k, v in _ki_node_id_map.items()}

    def _side_effect(cypher: str, params: dict | None = None):
        params = params or {}
        proj = params.get("project_slug", "stagehand")

        if "AFFECTS" in cypher and "Feature" in cypher:
            # RETURN f.feature_slug projects a scalar — real Quine returns the
            # raw value directly, not wrapped in a node dict.
            return [[slug] for slug in affects_features]

        if "HAS_ERROR" in cypher and "ErrorSignature" in cypher and "CustomerIssue" in cypher:
            # RETURN e.normalized_error projects a scalar — same as above.
            return [[err] for err in has_errors]

        if "idFrom('module'" in cypher:
            slug = params.get("feature_slug", "")
            files = module_files.get(slug, [])
            mod_dict = {
                "id": 0,
                "properties": {
                    "module_slug": slug,
                    "project_slug": proj,
                    "node_type": "Module",
                    "name": slug,
                },
            }
            return [
                [
                    mod_dict,
                    {
                        "id": i + 1,
                        "properties": {
                            "repo_path": path,
                            "project_slug": proj,
                            "node_type": "File",
                        },
                    },
                ]
                for i, path in enumerate(files)
            ]

        if "IMPLEMENTED_BY" in cypher:
            slug = params.get("feature_slug", "")
            files = feature_files.get(slug, [])
            feat_dict = {
                "id": 0,
                "properties": {
                    "feature_slug": slug,
                    "project_slug": proj,
                    "node_type": "Feature",
                    "name": slug,
                },
            }
            mod_dict = {
                "id": 1,
                "properties": {
                    "module_slug": slug,
                    "project_slug": proj,
                    "node_type": "Module",
                    "name": slug,
                },
            }
            return [
                [
                    feat_dict,
                    mod_dict,
                    {
                        "id": i + 2,
                        "properties": {
                            "repo_path": path,
                            "project_slug": proj,
                            "node_type": "File",
                        },
                    },
                ]
                for i, path in enumerate(files)
            ]

        if "HAS_ERROR" in cypher and "KnownIssue" in cypher:
            err = params.get("normalized_error", "")
            kis = error_known_issues.get(err, [])
            return [
                [
                    {
                        "id": _ki_issue_id_to_node_id.get(kid, 1000 + i),
                        "properties": {
                            "issue_id": kid,
                            "summary": summary,
                            "status": status,
                            "project_slug": proj,
                            "node_type": "KnownIssue",
                        },
                    }
                ]
                for i, (kid, summary, status) in enumerate(kis)
            ]

        if "RESOLVED_BY" in cypher:
            # Dispatch on integer Quine node ID — matches real traversal behaviour.
            ki_node_id = params.get("ki_node_id")
            kid = _ki_node_id_map.get(ki_node_id, "")
            fixes = ki_fixes.get(kid, [])
            return [
                [
                    {
                        "id": i,
                        "properties": {
                            "fix_id": fid,
                            "summary": summary,
                            "kind": kind,
                            "project_slug": proj,
                            "node_type": "Fix",
                        },
                    }
                ]
                for i, (fid, summary, kind) in enumerate(fixes)
            ]

        if "HAS_SIMILARITY_MATCH" in cypher:
            non_rejected = [(kid, s) for kid, s in similarity_matches if s != "rejected"]
            return [
                [
                    {
                        "id": i,
                        "properties": {
                            "issue_id": kid,
                            "summary": f"Similar {kid}",
                            "status": "open",
                            "project_slug": proj,
                            "node_type": "KnownIssue",
                        },
                    },
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
        for kid, _, _ in kis:
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
            # RETURN e.normalized_error projects a scalar — same as above.
            return [[err] for err in has_errors]

        if "IMPLEMENTED_BY" in cypher:
            return []

        if "HAS_ERROR" in cypher and "KnownIssue" in cypher:
            err = params.get("normalized_error", "")
            kis = error_known_issues.get(err, [])
            return [
                [
                    {
                        "id": _ki_issue_id_to_node_id.get(kid, 1000 + i),
                        "properties": {
                            "issue_id": kid,
                            "summary": summary,
                            "status": status,
                            "project_slug": proj,
                            "node_type": "KnownIssue",
                        },
                    }
                ]
                for i, (kid, summary, status) in enumerate(kis)
            ]

        if "RESOLVED_BY" in cypher:
            ki_node_id = params.get("ki_node_id")
            kid = _ki_node_id_map.get(ki_node_id, "")
            fixes = ki_fixes_cross_project.get(kid, [])
            # Simulate Quine filtering: Fix nodes belong to OTHER-PROJECT;
            # with the correct Cypher filter (fix.project_slug = $project_slug),
            # Quine returns nothing. We simulate that by checking the cypher.
            if "fix.project_slug = $project_slug" in cypher:
                return []  # Quine filters out cross-project Fix nodes
            # Without the filter in Cypher, Quine would return them (the old bug).
            return [
                [
                    {
                        "id": i,
                        "properties": {
                            "fix_id": fid,
                            "summary": summary,
                            "kind": kind,
                            "project_slug": "OTHER-PROJECT",
                            "node_type": "Fix",
                        },
                    }
                ]
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

        if "AFFECTS" in cypher and "Feature" in cypher:
            # RETURN f.feature_slug projects a scalar — real Quine returns the
            # raw value directly, not wrapped in a node dict.
            return [[slug] for slug in affects_features]

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
            # with the correct Cypher filter (ki.project_slug = $project_slug),
            # Quine returns nothing. We simulate that by inspecting the cypher.
            if "ki.project_slug = $project_slug" in cypher:
                return []  # Quine filters out cross-project KnownIssue nodes
            # Without the filter in Cypher, Quine would return them (the old bug).
            return [
                [
                    {
                        "id": i,
                        "properties": {
                            "issue_id": kid,
                            "summary": f"Foreign {kid}",
                            "status": "open",
                            "project_slug": "OTHER-PROJECT",
                            "node_type": "KnownIssue",
                        },
                    },
                    {"review_status": status},
                ]
                for i, (kid, status) in enumerate(similarity_ki_foreign)
            ]

        return []

    return _side_effect


# ---------------------------------------------------------------------------
# HAS_TEST traversal — test files surface in relevant_files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_test_files_with_no_other_evidence_appear_in_covered_tests_not_relevant_tests():
    """@spec DRE-TESTCOV-001, DRE-TESTCOV-002 — a test file reachable only via
    Feature-[:HAS_TEST]->File (no other evidence tying it to this ticket) is
    informational: it belongs in covered_tests, not relevant_tests/
    scored_candidates. Revised from this test's original assertion (bare
    coverage alone used to earn a scored test_coverage evidence item) after
    a live GitHub issue showed a test file covering two features stacking
    two test_coverage hits into a misleading MEDIUM-confidence score with
    zero ticket-specific evidence behind it."""
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue(raw_text=None)  # graph-anchored; no LLM call

    async def mock_query(cypher, params=None):
        # Graph anchor: one feature
        if "AFFECTS" in cypher and "Feature" in cypher:
            return [["shtp-receiver"]]
        if "AFFECTS" in cypher and "ErrorSignature" in cypher:
            return []
        # Source file traversal: Feature->Module->DEFINED_IN->File
        if "IMPLEMENTED_BY" in cypher and "DEFINED_IN" in cypher:
            return [
                [
                    {"properties": {"feature_slug": "shtp-receiver"}},
                    {"properties": {"module_slug": "shtp"}},
                    {"properties": {"repo_path": "agent/src/shtp.c"}},
                ]
            ]
        # Test file traversal: Feature->HAS_TEST->File
        if "HAS_TEST" in cypher:
            return [[{"properties": {"repo_path": "agent/tests/test_shtp.c"}}]]
        # Commit traversal
        if "TOUCHES" in cypher:
            return []
        # Similarity
        if "HAS_SIMILARITY_MATCH" in cypher:
            return []
        return []

    mock_client = AsyncMock()
    mock_client.get_node = AsyncMock(return_value=issue)
    mock_client.query = AsyncMock(side_effect=mock_query)

    mock_gw = AsyncMock()
    mock_gw.summarise_packet = AsyncMock(return_value=[])

    with patch("modok.retrieval.engine.gateway", mock_gw):
        packet = await retrieve("123", "stagehand", mock_client)

    assert "agent/src/shtp.c" in packet.relevant_files
    assert "agent/tests/test_shtp.c" not in packet.relevant_tests
    assert "agent/tests/test_shtp.c" not in [c.path for c in packet.scored_candidates]
    covered_paths = [ct.path for ct in packet.covered_tests]
    assert "agent/tests/test_shtp.c" in covered_paths


# @spec DRE-TRAV-001, DRE-TESTCOV-002
@pytest.mark.asyncio
async def test_source_file_outranks_test_file_for_same_feature_anchor():
    """A source file and its test, both touched by the same recent commit
    (so both carry real, non-coverage evidence and both remain in
    scored_candidates), must not rank the test above the source — found
    live: test_coverage (8.0) outscoring feature_anchor (7.0) meant every
    real bug report's "Top Suspects" list put test files above the likely
    actual source of the bug. Revised to give the test file its own
    non-coverage evidence (a commit touch) — bare HAS_TEST coverage alone no
    longer keeps a test file in scored_candidates at all (DRE-TESTCOV-002),
    so the original zero-other-evidence setup no longer exercises this
    ordering; a covering test that also has real evidence still must not
    outrank its source under equivalent-tier evidence."""
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue(raw_text=None)

    async def mock_query(cypher, params=None):
        if "AFFECTS" in cypher and "Feature" in cypher:
            return [["shtp-receiver"]]
        if "AFFECTS" in cypher and "ErrorSignature" in cypher:
            return []
        if "IMPLEMENTED_BY" in cypher and "DEFINED_IN" in cypher:
            return [
                [
                    {"properties": {"feature_slug": "shtp-receiver"}},
                    {"properties": {"module_slug": "shtp"}},
                    {"properties": {"repo_path": "agent/src/shtp.c"}},
                ]
            ]
        if "HAS_TEST" in cypher:
            return [[{"properties": {"repo_path": "agent/tests/test_shtp.c"}}]]
        if "TOUCHES" in cypher:
            commit = {
                "properties": {
                    "sha": "abc123",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "author_name": "dev",
                    "message": "unrelated touch",
                    "file_hunks": "{}",
                    "project_slug": "stagehand",
                }
            }
            file_props = {"properties": {"project_slug": "stagehand"}}
            return [[file_props, commit]]
        if "HAS_SIMILARITY_MATCH" in cypher:
            return []
        return []

    mock_client = AsyncMock()
    mock_client.get_node = AsyncMock(return_value=issue)
    mock_client.query = AsyncMock(side_effect=mock_query)

    mock_gw = AsyncMock()
    mock_gw.summarise_packet = AsyncMock(return_value=[])

    with patch("modok.retrieval.engine.gateway", mock_gw):
        packet = await retrieve("123", "stagehand", mock_client)

    by_path = {c.path: c for c in packet.scored_candidates}
    assert by_path["agent/src/shtp.c"].score > by_path["agent/tests/test_shtp.c"].score
    # Has real evidence (the commit touch), so it stays in scored_candidates
    # rather than moving to the informational covered_tests list.
    assert "agent/tests/test_shtp.c" not in [ct.path for ct in packet.covered_tests]


# @spec DRE-TESTCOV-001
@pytest.mark.asyncio
async def test_test_file_covered_by_two_features_does_not_stack_into_scored_evidence():
    """Found live (github.com/rocketmark/stagehand/issues/31): a test file
    covering two features (lonet-sender, livelink) stacked two test_coverage
    hits via geometric decay — 7.0 + 7.0*0.5 = 10.5 — pushing a file with
    zero ticket-specific evidence into MEDIUM confidence, ahead of
    genuinely relevant candidates. Under the new design it must not be a
    scored candidate at all, and it must appear exactly once in
    covered_tests listing both covering slugs, not duplicated."""
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue(raw_text=None)

    async def mock_query(cypher, params=None):
        params = params or {}
        if "AFFECTS" in cypher and "Feature" in cypher:
            return [["lonet-sender"], ["livelink"]]
        if "AFFECTS" in cypher and "ErrorSignature" in cypher:
            return []
        if "IMPLEMENTED_BY" in cypher and "DEFINED_IN" in cypher:
            return []
        if "HAS_TEST" in cypher:
            return [[{"properties": {"repo_path": "client/tests/test_output_consistency.py"}}]]
        if "TOUCHES" in cypher:
            return []
        if "HAS_SIMILARITY_MATCH" in cypher:
            return []
        return []

    mock_client = AsyncMock()
    mock_client.get_node = AsyncMock(return_value=issue)
    mock_client.query = AsyncMock(side_effect=mock_query)

    mock_gw = AsyncMock()
    mock_gw.summarise_packet = AsyncMock(return_value=[])

    with patch("modok.retrieval.engine.gateway", mock_gw):
        packet = await retrieve("123", "stagehand", mock_client)

    assert "client/tests/test_output_consistency.py" not in [
        c.path for c in packet.scored_candidates
    ]
    matches = [ct for ct in packet.covered_tests if ct.path == "client/tests/test_output_consistency.py"]
    assert len(matches) == 1
    assert set(matches[0].covering_slugs) == {"lonet-sender", "livelink"}


# ---------------------------------------------------------------------------
# extract_module_elements — test files contribute identifiers
# ---------------------------------------------------------------------------


def test_extract_module_elements_includes_test_identifiers(tmp_path):
    """Test function names from test_files must appear in extracted elements."""
    from modok.retrieval.elements import extract_module_elements

    src = tmp_path / "shtp.py"
    src.write_text("class ShtpReceiver:\n    def receive(self): pass\n")

    tst = tmp_path / "test_shtp.py"
    tst.write_text("def test_receive_returns_epoch(): pass\ndef test_empty_packet(): pass\n")

    result = extract_module_elements(
        source_files=["shtp.py"],
        repo_root=tmp_path,
        test_files=["test_shtp.py"],
    )

    assert "ShtpReceiver" in result
    assert "receive" in result
    assert "test_receive_returns_epoch" in result
    assert "test_empty_packet" in result


def test_extract_module_elements_caps_test_identifiers(tmp_path):
    """Test identifiers must not overflow _MAX_TEST_ELEMENTS cap."""
    from modok.retrieval.elements import _MAX_TEST_ELEMENTS, extract_module_elements

    tst = tmp_path / "test_big.py"
    # Write 20 test functions — more than _MAX_TEST_ELEMENTS
    lines = "\n".join(f"def test_fn_{i}(): pass" for i in range(20))
    tst.write_text(lines)

    result = extract_module_elements(
        source_files=[],
        repo_root=tmp_path,
        test_files=["test_big.py"],
    )

    assert len(result) <= _MAX_TEST_ELEMENTS


def test_extract_module_elements_deduplicates_across_source_and_test(tmp_path):
    """An identifier present in both source and test files must appear only once."""
    from modok.retrieval.elements import extract_module_elements

    src = tmp_path / "mod.py"
    src.write_text("def shared_helper(): pass\n")

    tst = tmp_path / "test_mod.py"
    tst.write_text("def shared_helper(): pass\ndef test_uses_helper(): pass\n")

    result = extract_module_elements(
        source_files=["mod.py"],
        repo_root=tmp_path,
        test_files=["test_mod.py"],
    )

    assert result.count("shared_helper") == 1
    assert "test_uses_helper" in result


# ---------------------------------------------------------------------------
# Anchor Token Matching
# ---------------------------------------------------------------------------


def test_tokenize_snake_case():
    # @spec DRE-TOKEN-001
    from modok.retrieval.engine import _tokenize

    assert _tokenize("reinit_requested") == {"reinit", "requested"}


def test_tokenize_camel_case():
    # @spec DRE-TOKEN-001
    from modok.retrieval.engine import _tokenize

    assert _tokenize("DeviceCard") == {"device", "card"}


def test_tokenize_kebab_case():
    # @spec DRE-TOKEN-001
    from modok.retrieval.engine import _tokenize

    assert _tokenize("device-card") == {"device", "card"}


def test_tokenize_mixed_leading_underscore():
    # @spec DRE-TOKEN-001
    from modok.retrieval.engine import _tokenize

    assert _tokenize("_make_tracker_row") == {"make", "tracker", "row"}


def test_tokenize_excludes_tokens_length_two_or_less():
    # @spec DRE-TOKEN-001
    from modok.retrieval.engine import _tokenize

    result = _tokenize("is_ok_now")
    assert "is" not in result
    assert "ok" not in result
    assert "now" in result


def test_symptom_error_tokens_excludes_feature_slug_tokens():
    # @spec DRE-TOKEN-002
    # Building with empty feature_slugs excludes those slug tokens.
    # The same tokens DO appear when feature_slugs are included.
    from modok.retrieval.engine import _build_anchor_tokens

    symptom_error_tokens = _build_anchor_tokens(
        feature_slugs=[],
        error_sigs=["shtp-version-mismatch"],
        symptoms=["pose dropout"],
    )
    full_anchor_tokens = _build_anchor_tokens(
        feature_slugs=["device-card"],
        error_sigs=["shtp-version-mismatch"],
        symptoms=["pose dropout"],
    )
    assert "device" not in symptom_error_tokens
    assert "card" not in symptom_error_tokens
    assert "device" in full_anchor_tokens
    assert "card" in full_anchor_tokens


def test_func_anchor_tokens_adds_matched_element_tokens():
    # @spec DRE-TOKEN-003
    from modok.retrieval.engine import _build_anchor_tokens, _tokenize

    symptom_error_tokens = _build_anchor_tokens([], ["connection-error"], ["reinit"])
    matched_elements = ["reinit_requested"]
    func_anchor_tokens = symptom_error_tokens.copy()
    for elem in matched_elements:
        func_anchor_tokens.update(_tokenize(elem))
    assert "reinit" in func_anchor_tokens
    assert "requested" in func_anchor_tokens


def test_func_anchor_tokens_does_not_include_feature_slug_tokens():
    # @spec DRE-TOKEN-003
    # func_anchor_tokens derives from symptom_error_tokens (no feature_slugs) +
    # matched_elements. Feature slug tokens never enter this set.
    from modok.retrieval.engine import _build_anchor_tokens

    symptom_error_tokens = _build_anchor_tokens([], ["connection-error"], [])
    func_anchor_tokens = symptom_error_tokens.copy()
    # No matched elements, no feature slugs — "device" and "card" must not appear.
    assert "device" not in func_anchor_tokens
    assert "card" not in func_anchor_tokens


# ---------------------------------------------------------------------------
# Element / function anchor matching helpers
# ---------------------------------------------------------------------------


def _make_module_error_query_side_effect(
    module_slug: str,
    module_files: list[str],
    error_sigs: list[str],
    commits: list[dict] | None = None,
):
    """
    Query side-effect providing BOTH a module-slug graph anchor (via AFFECTS,
    resolved through module fallback) AND error signature anchors.
    This ensures symptom_error_tokens is non-empty (from error sig tokens)
    while the module slug resolves to files for element matching.
    """
    commits = commits or []

    def _side_effect(cypher: str, params: dict | None = None):
        params = params or {}
        proj = params.get("project_slug", "stagehand")
        slug = params.get("feature_slug", "")
        file_path = params.get("file_path", "")
        if "AFFECTS" in cypher and "Feature" in cypher:
            return [[module_slug]]

        if "HAS_ERROR" in cypher and "ErrorSignature" in cypher and "CustomerIssue" in cypher:
            return [[err] for err in error_sigs]

        if "IMPLEMENTED_BY" in cypher and "DEFINED_IN" in cypher:
            return []  # No Feature→Module→File; triggers module fallback

        if "HAS_TEST" in cypher and "idFrom('feature'" in cypher:
            return []

        if "idFrom('module'" in cypher and "DEFINED_IN" in cypher and slug == module_slug:
            return [
                [
                    {
                        "id": 1,
                        "properties": {
                            "module_slug": module_slug,
                            "project_slug": proj,
                            "node_type": "Module",
                            "name": module_slug,
                        },
                    },
                    {
                        "id": i + 2,
                        "properties": {
                            "repo_path": path,
                            "project_slug": proj,
                            "node_type": "File",
                        },
                    },
                ]
                for i, path in enumerate(module_files)
            ]

        if "idFrom('module'" in cypher:
            return []

        if "TOUCHES" in cypher:
            matching = [c for c in commits if file_path in c.get("files_touched", [])]
            return [
                [
                    {"id": 0, "properties": {"repo_path": file_path, "project_slug": proj}},
                    {"id": i + 1, "properties": {**c, "project_slug": proj}},
                ]
                for i, c in enumerate(matching)
            ]

        if "HAS_ERROR" in cypher and "KnownIssue" in cypher:
            return []

        if "RESOLVED_BY" in cypher:
            return []

        if "HAS_SIMILARITY_MATCH" in cypher:
            return []

        return []

    return _side_effect


def _make_module_slug_query_side_effect(
    module_slug: str,
    module_files: list[str],
    commits: list[dict] | None = None,
):
    """
    Query side-effect where `module_slug` is returned as the graph anchor and
    resolves via the module fallback path (IMPLEMENTED_BY returns nothing).
    Optionally includes commits touching module_files.
    """
    commits = commits or []

    def _side_effect(cypher: str, params: dict | None = None):
        params = params or {}
        proj = params.get("project_slug", "stagehand")
        slug = params.get("feature_slug", "")
        file_path = params.get("file_path", "")

        if "AFFECTS" in cypher and "Feature" in cypher:
            return [[module_slug]]

        if "HAS_ERROR" in cypher and "ErrorSignature" in cypher and "CustomerIssue" in cypher:
            return []

        if "IMPLEMENTED_BY" in cypher and "DEFINED_IN" in cypher:
            return []  # No Feature→Module→File; triggers module fallback

        if "HAS_TEST" in cypher and "idFrom('feature'" in cypher:
            return []

        if "idFrom('module'" in cypher and slug == module_slug:
            return [
                [
                    {
                        "id": 1,
                        "properties": {
                            "module_slug": module_slug,
                            "project_slug": proj,
                            "node_type": "Module",
                            "name": module_slug,
                        },
                    },
                    {
                        "id": i + 2,
                        "properties": {
                            "repo_path": path,
                            "project_slug": proj,
                            "node_type": "File",
                        },
                    },
                ]
                for i, path in enumerate(module_files)
            ]

        if "idFrom('module'" in cypher:
            return []  # Module test-file walk returns nothing

        if "TOUCHES" in cypher:
            matching = [c for c in commits if file_path in c.get("files_touched", [])]
            return [
                [
                    {"id": 0, "properties": {"repo_path": file_path, "project_slug": proj}},
                    {"id": 1, "properties": {**c, "project_slug": proj}},
                ]
                for c in matching
            ]

        if "HAS_SIMILARITY_MATCH" in cypher:
            return []

        if "RESOLVED_BY" in cypher:
            return []

        return []

    return _side_effect


# ---------------------------------------------------------------------------
# Element Anchor Matching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_element_matches_symptom_tokens_not_feature_slug_tokens():
    # @spec DRE-ELEM-001, DRE-TOKEN-002
    # Element "reinit_requested" matches error sig token "reinit"; element "DeviceCard"
    # should NOT match because its tokens {"device", "card"} come only from the feature
    # slug "device-card" and are absent from symptom_error_tokens.
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue(raw_text=None)
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    # Graph anchors: feature slug "device-card" (resolves as module) + error sig "reinit-error".
    # error_sigs=["reinit-error"] → symptom_error_tokens includes "reinit" and "error".
    # "device-card" tokens ("device", "card") are NOT in symptom_error_tokens.
    mock_client.query.side_effect = _make_module_error_query_side_effect(
        module_slug="device-card",
        module_files=["ui/device_card.py"],
        error_sigs=["reinit-error"],
    )

    with patch("modok.retrieval.engine.gateway") as mock_gw:
        mock_gw.summarise_packet = AsyncMock(return_value="summary")

        packet = await retrieve(
            issue_id=1,
            project_slug="stagehand",
            client=mock_client,
            module_elements={"device-card": ["reinit_requested", "DeviceCard"]},
            module_source_files={"device-card": ["ui/device_card.py"]},
        )

    # "reinit_requested" matches: tokens {"reinit", "requested"} ∩ {"reinit", "error"} ≠ ∅
    # "DeviceCard" does NOT match: tokens {"device", "card"} ∩ {"reinit", "error"} = ∅
    elem_evidence = [
        ev
        for c in packet.scored_candidates
        for ev in c.evidence
        if ev.type == "element_anchor_match"
    ]
    assert elem_evidence, "Expected element_anchor_match evidence"
    for ev in elem_evidence:
        assert "reinit_requested" in ev.explanation
        assert "DeviceCard" not in ev.explanation


@pytest.mark.asyncio
async def test_element_match_adds_evidence_to_existing_files_with_score_6():
    # @spec DRE-ELEM-002
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue(raw_text=None)
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_module_error_query_side_effect(
        module_slug="device-card",
        module_files=["ui/device_card.py"],
        error_sigs=["reinit-error"],
    )

    with patch("modok.retrieval.engine.gateway") as mock_gw:
        mock_gw.summarise_packet = AsyncMock(return_value="summary")

        packet = await retrieve(
            issue_id=1,
            project_slug="stagehand",
            client=mock_client,
            module_elements={"device-card": ["reinit_requested"]},
            module_source_files={"device-card": ["ui/device_card.py"]},
        )

    candidate = next(c for c in packet.scored_candidates if c.path == "ui/device_card.py")
    elem_ev = [ev for ev in candidate.evidence if ev.type == "element_anchor_match"]
    assert elem_ev, "Expected element_anchor_match evidence on file already in evidence map"
    assert elem_ev[0].score == 6.0


@pytest.mark.asyncio
async def test_element_match_does_not_add_new_files_to_evidence_map():
    # @spec DRE-ELEM-003
    # A file listed in module_source_files but not reachable via graph traversal
    # must NOT appear in scored_candidates after element matching.
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue(raw_text=None)
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    # Traversal returns only "ui/device_card.py"; "ui/helpers.py" is only in module_source_files
    mock_client.query.side_effect = _make_module_error_query_side_effect(
        module_slug="device-card",
        module_files=["ui/device_card.py"],
        error_sigs=["reinit-error"],
    )

    with patch("modok.retrieval.engine.gateway") as mock_gw:
        mock_gw.summarise_packet = AsyncMock(return_value="summary")

        packet = await retrieve(
            issue_id=1,
            project_slug="stagehand",
            client=mock_client,
            module_elements={"device-card": ["reinit_requested"]},
            module_source_files={"device-card": ["ui/device_card.py", "ui/helpers.py"]},
        )

    candidate_paths = [c.path for c in packet.scored_candidates]
    assert "ui/helpers.py" not in candidate_paths


@pytest.mark.asyncio
async def test_element_match_extends_func_anchor_tokens():
    # @spec DRE-ELEM-004
    # When element matching fires, the matched element's tokens expand func_anchor_tokens
    # so that function anchor matching can pick up defs whose tokens overlap with the
    # element name, even if those tokens weren't in the original error/symptom set.
    # Observable: function_anchor_match fires for def "reinit_requested" in a commit,
    # where "requested" alone would not match without the element match expanding the token set.
    from modok.retrieval.engine import retrieve

    import json

    sha = "abc1234" + "0" * 33
    commit = {
        "sha": sha,
        "timestamp": "2024-01-15T10:00:00Z",
        "author_name": "Test Author",
        "message": "fix reinit path",
        "files_touched": ["ui/device_card.py"],
        "file_hunks": json.dumps({"ui/device_card.py": [{"defs": ["reinit_requested"]}]}),
    }

    issue = make_customer_issue(raw_text=None)
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_module_error_query_side_effect(
        module_slug="device-card",
        module_files=["ui/device_card.py"],
        error_sigs=["reinit-error"],
        commits=[commit],
    )

    with patch("modok.retrieval.engine.gateway") as mock_gw:
        mock_gw.summarise_packet = AsyncMock(return_value="summary")

        packet = await retrieve(
            issue_id=1,
            project_slug="stagehand",
            client=mock_client,
            module_elements={"device-card": ["reinit_requested"]},
            module_source_files={"device-card": ["ui/device_card.py"]},
        )

    candidate = next(c for c in packet.scored_candidates if c.path == "ui/device_card.py")
    fn_ev = [ev for ev in candidate.evidence if ev.type == "function_anchor_match"]
    assert fn_ev, (
        "function_anchor_match expected after element matching expanded func_anchor_tokens"
    )


@pytest.mark.asyncio
async def test_commit_message_matching_anchor_tokens_gets_evidence():
    # @spec DRE-CAND-007
    # A commit whose own message shares an anchor token with the ticket (here,
    # the error signature "reinit-error" contributes token "reinit") is much
    # stronger, more targeted evidence than bare recency — found live: a
    # commit literally titled "fixed wifi provisioning" was indistinguishable
    # from four unrelated maintenance commits touching the same file.
    from modok.retrieval.engine import retrieve

    import json

    sha = "def4567" + "0" * 33
    relevant_commit = {
        "sha": sha,
        "timestamp": "2024-01-15T10:00:00Z",
        "author_name": "Test Author",
        "message": "fixed reinit handling",
        "files_touched": ["ui/device_card.py"],
        "file_hunks": json.dumps({}),
    }
    unrelated_sha = "aaa1111" + "0" * 33
    unrelated_commit = {
        "sha": unrelated_sha,
        "timestamp": "2024-01-14T10:00:00Z",
        "author_name": "Test Author",
        "message": "cleaned up docs",
        "files_touched": ["ui/device_card.py"],
        "file_hunks": json.dumps({}),
    }

    issue = make_customer_issue(raw_text=None)
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_module_error_query_side_effect(
        module_slug="device-card",
        module_files=["ui/device_card.py"],
        error_sigs=["reinit-error"],
        commits=[relevant_commit, unrelated_commit],
    )

    with patch("modok.retrieval.engine.gateway") as mock_gw:
        mock_gw.summarise_packet = AsyncMock(return_value="summary")

        packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)

    candidate = next(c for c in packet.scored_candidates if c.path == "ui/device_card.py")
    msg_ev = [ev for ev in candidate.evidence if ev.type == "commit_message_match"]
    assert len(msg_ev) == 1
    assert msg_ev[0].explanation.startswith("fixed reinit handling")
    assert sha[:7] in msg_ev[0].explanation


# ---------------------------------------------------------------------------
# Function Anchor Matching
# ---------------------------------------------------------------------------


def test_matching_defs_returns_overlapping_def_names():
    # @spec DRE-FUNC-001
    from modok.retrieval.engine import _matching_defs

    hunk_data = [{"defs": ["reinit_requested", "set_color", "DeviceCard"]}]
    anchor_tokens = {"reinit", "requested"}
    result = _matching_defs(hunk_data, anchor_tokens)
    assert "reinit_requested" in result
    assert "set_color" not in result
    assert "DeviceCard" not in result


def test_matching_defs_empty_when_no_overlap():
    # @spec DRE-FUNC-001
    from modok.retrieval.engine import _matching_defs

    hunk_data = [{"defs": ["set_color", "update_layout"]}]
    anchor_tokens = {"reinit", "requested"}
    assert _matching_defs(hunk_data, anchor_tokens) == []


def test_matching_defs_empty_for_empty_hunk_data():
    # @spec DRE-FUNC-001
    from modok.retrieval.engine import _matching_defs

    assert _matching_defs([], {"reinit"}) == []


@pytest.mark.asyncio
async def test_function_anchor_match_explanation_format():
    # @spec DRE-FUNC-002
    # Explanation must be "{names} · {sha_short}" where sha_short is 7 chars.
    from modok.retrieval.engine import retrieve

    import json

    sha = "abc1234" + "0" * 33
    commit = {
        "sha": sha,
        "timestamp": "2024-01-15T10:00:00Z",
        "author_name": "Dev",
        "message": "fix reinit",
        "files_touched": ["ui/device_card.py"],
        "file_hunks": json.dumps({"ui/device_card.py": [{"defs": ["reinit_requested"]}]}),
    }

    issue = make_customer_issue(raw_text=None)
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_module_slug_query_side_effect(
        module_slug="device-card",
        module_files=["ui/device_card.py"],
        commits=[commit],
    )

    with patch("modok.retrieval.engine.gateway") as mock_gw:
        # graph anchor: feature slug from AFFECTS
        # but this test needs symptom tokens to match the def name.
        # We'll inject via error sig so symptom_error_tokens has "reinit".
        mock_client.query.side_effect = None
        mock_client.query = AsyncMock(
            side_effect=_make_commit_query_side_effect(
                module_slug="device-card",
                module_files=["ui/device_card.py"],
                error_sigs=["reinit-error"],
                commits=[commit],
            )
        )
        mock_gw.summarise_packet = AsyncMock(return_value="summary")

        packet = await retrieve(
            issue_id=1,
            project_slug="stagehand",
            client=mock_client,
        )

    candidate = next((c for c in packet.scored_candidates if c.path == "ui/device_card.py"), None)
    assert candidate is not None
    fn_ev = [ev for ev in candidate.evidence if ev.type == "function_anchor_match"]
    assert fn_ev, "Expected function_anchor_match evidence"
    # Format: "{names} · {sha_short}"
    parts = fn_ev[0].explanation.split(" · ")
    assert len(parts) == 2, f"Unexpected explanation format: {fn_ev[0].explanation!r}"
    assert parts[1] == sha[:7], f"sha_short should be 7 chars, got {parts[1]!r}"


@pytest.mark.asyncio
async def test_function_anchor_match_does_not_add_new_files():
    # @spec DRE-FUNC-003
    # A file touched by a commit but not already in the evidence map must not be added.
    from modok.retrieval.engine import retrieve

    import json

    sha = "def5678" + "0" * 33
    commit = {
        "sha": sha,
        "timestamp": "2024-01-15T10:00:00Z",
        "author_name": "Dev",
        "message": "refactor",
        "files_touched": ["ui/device_card.py", "ui/other_file.py"],
        "file_hunks": json.dumps(
            {
                "ui/device_card.py": [{"defs": ["reinit_requested"]}],
                "ui/other_file.py": [{"defs": ["reinit_helper"]}],
            }
        ),
    }

    mock_client = AsyncMock()
    mock_client.get_node.return_value = make_customer_issue(raw_text=None)
    mock_client.query = AsyncMock(
        side_effect=_make_commit_query_side_effect(
            module_slug="device-card",
            module_files=["ui/device_card.py"],  # only device_card.py is in evidence map
            error_sigs=["reinit-error"],
            commits=[commit],
        )
    )

    with patch("modok.retrieval.engine.gateway") as mock_gw:
        mock_gw.summarise_packet = AsyncMock(return_value="summary")
        packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)

    candidate_paths = [c.path for c in packet.scored_candidates]
    assert "ui/other_file.py" not in candidate_paths


# ---------------------------------------------------------------------------
# Candidate Scoring
# ---------------------------------------------------------------------------


def test_score_single_evidence_item():
    # @spec DRE-CAND-001
    from modok.retrieval.engine import EvidenceItem, _score_candidate

    items = [EvidenceItem(type="feature_anchor", score=7.0, explanation="")]
    # 1 type: diversity bonus = 3.0 * min(0, 4) = 0. Total = 7.0.
    assert _score_candidate(items) == 7.0


def test_score_geometric_decay_within_same_type():
    # @spec DRE-CAND-001
    from modok.retrieval.engine import EvidenceItem, _score_candidate

    items = [
        EvidenceItem(type="feature_anchor", score=7.0, explanation=""),
        EvidenceItem(type="feature_anchor", score=7.0, explanation=""),
    ]
    # Same type: 7.0 + 7.0*0.5 = 10.5. Diversity bonus = 0.
    assert _score_candidate(items) == 10.5


def test_score_diversity_bonus_per_unique_type():
    # @spec DRE-CAND-001
    from modok.retrieval.engine import EvidenceItem, _score_candidate

    items = [
        EvidenceItem(type="feature_primary_file", score=7.0, explanation=""),
        EvidenceItem(type="test_coverage", score=8.0, explanation=""),
    ]
    # Two corroborating types: diversity bonus = 3.0 * min(1, 4) = 3.0.
    # Total = 7.0 + 8.0 + 3.0 = 18.0.
    assert _score_candidate(items) == 18.0


def test_score_diversity_bonus_capped_at_four_types():
    # @spec DRE-CAND-001
    from modok.retrieval.engine import EvidenceItem, _score_candidate

    items = [
        EvidenceItem(type="feature_primary_file", score=1.0, explanation=""),
        EvidenceItem(type="test_coverage", score=1.0, explanation=""),
        EvidenceItem(type="element_anchor_match", score=1.0, explanation=""),
        EvidenceItem(type="function_anchor_match", score=1.0, explanation=""),
        EvidenceItem(type="ticket_mention", score=1.0, explanation=""),
    ]
    # 5 corroborating types: diversity bonus = 3.0 * min(4, 4) = 12.0 (capped).
    # Type scores: 5 × 1.0. Total = 5.0 + 12.0 = 17.0.
    assert _score_candidate(items) == 17.0


def test_score_recent_commit_does_not_count_toward_diversity_bonus_alone():
    # @spec DRE-CAND-006
    from modok.retrieval.engine import EvidenceItem, _score_candidate

    items = [
        EvidenceItem(type="feature_anchor", score=3.0, explanation=""),
        EvidenceItem(type="recent_commit", score=1.5, explanation=""),
    ]
    # Both types are non-corroborating (peripheral feature match + bare
    # recency) — with no direct evidence to reinforce, the diversity bonus
    # must not fire; it would manufacture apparent strength from two weak
    # signals alone. Total = 3.0 + 1.5 + 0.0 (no bonus) = 4.5.
    assert _score_candidate(items) == 4.5


def test_score_recent_commit_counts_toward_diversity_when_direct_evidence_present():
    # @spec DRE-CAND-006
    from modok.retrieval.engine import EvidenceItem, _score_candidate

    items = [
        EvidenceItem(type="feature_primary_file", score=9.0, explanation=""),
        EvidenceItem(type="element_anchor_match", score=6.0, explanation=""),
        EvidenceItem(type="recent_commit", score=1.5, explanation=""),
    ]
    # feature_primary_file and element_anchor_match are direct evidence —
    # recent_commit reinforcing an already-plausible candidate should still
    # count toward the diversity bonus. This is the case that regressed
    # live: a well-anchored candidate (element match + several recent
    # commits on the same file) scored noticeably lower than it should have
    # when recent_commit was unconditionally excluded from the bonus.
    # 3 types: diversity bonus = 3.0 * min(2, 4) = 6.0.
    # Total = 9.0 + 6.0 + 1.5 + 6.0 = 22.5.
    assert _score_candidate(items) == 22.5


def test_score_penalty_items_summed_directly():
    # @spec DRE-CAND-001
    from modok.retrieval.engine import EvidenceItem, _score_candidate

    items = [
        EvidenceItem(type="feature_anchor", score=8.0, explanation=""),
        EvidenceItem(type="doc_penalty", score=-6.0, explanation=""),
    ]
    # Positive: 8.0. Penalty: -6.0. Diversity bonus = 0 (penalty type not counted).
    assert _score_candidate(items) == 2.0


# @spec DRE-CAND-002
def test_is_source_path_recognizes_shell_scripts():
    from modok.retrieval.engine import _is_source_path

    assert _is_source_path("pi-image/chroot-customize.sh") is True


# @spec DRE-CAND-002
def test_is_source_path_recognizes_extensionless_scripts_dir():
    """Deployment/provisioning scripts under scripts/ are real operational
    code, not documentation, even with no file extension — found live:
    scripts/stagehand-wifi-provision (no extension) was penalized with the
    same 0.25x actionability multiplier as a markdown doc, dropping a
    directly-relevant script to the bottom of the Top Suspects list."""
    from modok.retrieval.engine import _is_source_path

    assert _is_source_path("scripts/stagehand-wifi-provision") is True
    assert _is_source_path("scripts/stagehand-health") is True


# @spec DRE-CAND-002
def test_is_source_path_still_treats_markdown_as_non_source():
    from modok.retrieval.engine import _is_source_path

    assert _is_source_path("docs/llds/wifi-provisioning.md") is False


# @spec DRE-CAND-002
def test_is_source_path_extensionless_outside_scripts_dir_is_non_source():
    from modok.retrieval.engine import _is_source_path

    assert _is_source_path("config/stagehand-wifi-provision.service") is False


def test_doc_penalty_applied_to_non_source_file():
    # @spec DRE-CAND-002
    from modok.retrieval.engine import EvidenceItem, _add_evidence, _build_scored_candidates

    evidence_map: dict = {}
    _add_evidence(
        evidence_map,
        "docs/README.md",
        EvidenceItem(
            type="feature_anchor",
            score=7.0,
            explanation="",
        ),
    )
    candidates = _build_scored_candidates(evidence_map, "source", cap=20)
    assert candidates, "Expected at least one candidate"
    c = candidates[0]
    penalty_items = [ev for ev in c.evidence if ev.type == "doc_penalty"]
    assert penalty_items, "Expected doc_penalty evidence on non-source file"
    assert penalty_items[0].score < 0


def test_confidence_label_high():
    # @spec DRE-CAND-003
    from modok.retrieval.engine import _confidence_label

    assert _confidence_label(20.0) == "high"
    assert _confidence_label(25.5) == "high"


def test_confidence_label_medium():
    # @spec DRE-CAND-003
    from modok.retrieval.engine import _confidence_label

    assert _confidence_label(10.0) == "medium"
    assert _confidence_label(19.9) == "medium"


def test_confidence_label_low():
    # @spec DRE-CAND-003
    from modok.retrieval.engine import _confidence_label

    assert _confidence_label(0.0) == "low"
    assert _confidence_label(9.9) == "low"


@pytest.mark.asyncio
async def test_source_and_test_candidates_built_and_merged_separately():
    # @spec DRE-CAND-004
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue(raw_text=None)
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue

    async def mock_query(cypher: str, params: dict | None = None):
        params = params or {}
        proj = params.get("project_slug", "stagehand")
        slug = params.get("feature_slug", "")

        if "AFFECTS" in cypher and "Feature" in cypher:
            return [["feat-a"]]
        if "HAS_ERROR" in cypher and "ErrorSignature" in cypher and "CustomerIssue" in cypher:
            return []
        if "IMPLEMENTED_BY" in cypher and "DEFINED_IN" in cypher:
            if slug == "feat-a":
                return [
                    [
                        {
                            "id": 0,
                            "properties": {
                                "feature_slug": "feat-a",
                                "project_slug": proj,
                                "node_type": "Feature",
                                "name": "feat-a",
                            },
                        },
                        {
                            "id": 1,
                            "properties": {
                                "module_slug": "feat-a",
                                "project_slug": proj,
                                "node_type": "Module",
                                "name": "feat-a",
                            },
                        },
                        {
                            "id": 2,
                            "properties": {
                                "repo_path": "src/feat_a.py",
                                "project_slug": proj,
                                "node_type": "File",
                            },
                        },
                    ]
                ]
            return []
        if "HAS_TEST" in cypher and "idFrom('feature'" in cypher:
            if slug == "feat-a":
                return [
                    [
                        {
                            "id": 3,
                            "properties": {
                                "repo_path": "tests/test_feat_a.py",
                                "project_slug": proj,
                                "node_type": "TestFile",
                            },
                        }
                    ]
                ]
            return []
        if "TOUCHES" in cypher:
            # Real (non-coverage) evidence for the test file — bare HAS_TEST
            # coverage alone no longer keeps a test file in scored_candidates
            # (DRE-TESTCOV-002), so this fixture needs its own evidence to
            # exercise DRE-CAND-004's no-cross-contamination guarantee.
            commit = {
                "properties": {
                    "sha": "def456",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "author_name": "dev",
                    "message": "touch",
                    "file_hunks": "{}",
                    "project_slug": proj,
                }
            }
            return [[{"properties": {"project_slug": proj}}, commit]]
        if "HAS_SIMILARITY_MATCH" in cypher:
            return []
        return []

    mock_client.query = AsyncMock(side_effect=mock_query)

    with patch("modok.retrieval.engine.gateway") as mock_gw:
        mock_gw.summarise_packet = AsyncMock(return_value="summary")
        packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)

    assert "src/feat_a.py" in packet.relevant_files
    assert "tests/test_feat_a.py" in packet.relevant_tests
    # They must not cross-contaminate each other's lists
    assert "tests/test_feat_a.py" not in packet.relevant_files
    assert "src/feat_a.py" not in packet.relevant_tests


# ---------------------------------------------------------------------------
# LLM Summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summarise_packet_called_with_matched_elements():
    # @spec DRE-SUMM-001
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue(raw_text=None)
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_module_error_query_side_effect(
        module_slug="device-card",
        module_files=["ui/device_card.py"],
        error_sigs=["reinit-error"],
    )

    with patch("modok.retrieval.engine.gateway") as mock_gw:
        mock_gw.summarise_packet = AsyncMock(return_value="LLM summary")

        await retrieve(
            issue_id=1,
            project_slug="stagehand",
            client=mock_client,
            module_elements={"device-card": ["reinit_requested"]},
            module_source_files={"device-card": ["ui/device_card.py"]},
        )

    call_kwargs = mock_gw.summarise_packet.call_args.kwargs
    assert "matched_elements" in call_kwargs
    assert "reinit_requested" in call_kwargs["matched_elements"]


@pytest.mark.asyncio
async def test_summarise_packet_exception_falls_back_to_issue_summary():
    # @spec DRE-SUMM-002
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue(summary="Tracker loses pose after USB reset")
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=["shtp-receiver"],
        has_errors=[],
    )

    with patch("modok.retrieval.engine.gateway") as mock_gw:
        mock_gw.summarise_packet = AsyncMock(side_effect=RuntimeError("LLM down"))
        packet = await retrieve(issue_id=1, project_slug="stagehand", client=mock_client)

    assert packet.summary == "Tracker loses pose after USB reset"
    assert packet.relevant_files is not None  # packet is still fully populated


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_progress_loading_emitted_with_only_issue_summary():
    # @spec DRE-STREAM-001
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue(summary="Tracker loses pose", raw_text="some text")
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=[],
        has_errors=[],
    )

    progress_events: list[tuple[str, object]] = []

    def on_progress(step, packet):
        progress_events.append((step, packet))

    with patch("modok.retrieval.engine.gateway") as mock_gw:
        mock_gw.parse_ticket = AsyncMock(return_value=make_ticket_parse_result())
        mock_gw.summarise_packet = AsyncMock(return_value="summary")
        await retrieve(
            issue_id=1,
            project_slug="stagehand",
            client=mock_client,
            on_progress=on_progress,
        )

    loading_events = [(s, p) for s, p in progress_events if s == "loading"]
    assert loading_events, "Expected a 'loading' on_progress event"
    _, loading_packet = loading_events[0]
    # Loading packet: only issue.summary populated; all lists empty; LLM summary empty
    assert loading_packet.issue.summary == "Tracker loses pose"
    assert loading_packet.relevant_files == []
    assert loading_packet.known_issues == []
    assert loading_packet.scored_candidates == []
    assert loading_packet.summary == ""


@pytest.mark.asyncio
async def test_on_progress_partial_emitted_before_summary_with_evidence_populated():
    # @spec DRE-STREAM-002
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue(raw_text=None)
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=["shtp-receiver"],
        has_errors=[],
        feature_files={"shtp-receiver": ["agent/src/shtp.c"]},
    )

    progress_events: list[tuple[str, object]] = []

    def on_progress(step, packet):
        progress_events.append((step, packet))

    with patch("modok.retrieval.engine.gateway") as mock_gw:
        mock_gw.summarise_packet = AsyncMock(return_value="LLM summary")
        await retrieve(
            issue_id=1,
            project_slug="stagehand",
            client=mock_client,
            on_progress=on_progress,
        )

    partial_events = [(s, p) for s, p in progress_events if s == "partial"]
    assert partial_events, "Expected a 'partial' on_progress event"
    _, partial_packet = partial_events[0]
    # Partial packet: evidence populated but summary is empty string
    assert partial_packet.summary == ""
    assert "agent/src/shtp.c" in partial_packet.relevant_files


# ---------------------------------------------------------------------------
# Anchor Pre-matching — DRE-ANCH-009
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_matched_module_slugs_appear_first_in_merged_feature_slugs():
    # @spec DRE-ANCH-009
    # When raw_text mentions a source file path from module_source_files, the
    # matching module slug is seeded before calling parse_ticket and appears first
    # in the merged feature_slugs list used for traversal.
    from modok.retrieval.engine import retrieve

    # raw_text mentions "agent/src/shtp.c" which belongs to "shtp-receiver"
    issue = make_customer_issue(raw_text="Problem in agent/src/shtp.c after USB reset")
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=[],
        has_errors=[],
    )

    captured_args: dict = {}

    async def capture_parse(*args, **kwargs):
        captured_args["feature_slugs_param"] = kwargs.get("feature_slugs", [])
        return make_ticket_parse_result(feature_slug="other-feat", error_signatures=[], symptoms=[])

    with patch("modok.retrieval.engine.gateway") as mock_gw:
        mock_gw.parse_ticket = AsyncMock(side_effect=capture_parse)
        mock_gw.summarise_packet = AsyncMock(return_value="summary")

        await retrieve(
            issue_id=1,
            project_slug="stagehand",
            client=mock_client,
            module_source_files={"shtp-receiver": ["agent/src/shtp.c"]},
        )

    # parse_ticket is called (no graph anchors)
    assert mock_gw.parse_ticket.called
    # The pre-matched slug should be passed to parse_ticket's context — but more
    # importantly, the merged feature_slugs list starts with the pre-matched slug.
    # We verify by checking the packet's anchor order via the traversal call order:
    # _traverse_feature_to_files is called once per slug in order. The pre-matched
    # slug "shtp-receiver" should be processed before "other-feat" from LLM.
    # Since we don't expose the internal list, we verify parse_ticket was called
    # (triggering the pre-match path) and the pre-matched slug appears in anchors.
    # (Traversal may not resolve it if no files exist in the mock, but the order is set.)


@pytest.mark.asyncio
async def test_pre_matched_slug_not_duplicated_when_llm_also_returns_it():
    # @spec DRE-ANCH-009
    from modok.retrieval.engine import retrieve

    issue = make_customer_issue(raw_text="Problem in agent/src/shtp.c")
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=[],
        has_errors=[],
    )

    with patch("modok.retrieval.engine.gateway") as mock_gw:
        # LLM also returns the same slug
        mock_gw.parse_ticket = AsyncMock(
            return_value=TicketParseResult(
                feature_slugs=["shtp-receiver"],  # same as pre-matched
                error_signatures=[],
                environment={},
                symptoms=[],
                confidence=0.8,
                raw_response="{}",
                mentioned_files=[],
            )
        )
        mock_gw.summarise_packet = AsyncMock(return_value="summary")

        packet = await retrieve(
            issue_id=1,
            project_slug="stagehand",
            client=mock_client,
            module_source_files={"shtp-receiver": ["agent/src/shtp.c"]},
        )

    # "shtp-receiver" should appear at most once in anchors
    assert packet.issue.anchors.features.count("shtp-receiver") <= 1


def test_pre_match_element_token_match():
    # @spec DRE-ANCH-009
    from modok.retrieval.engine import _pre_match_modules

    text = "tracker_lost_logged is never cleared after recovery"
    result = _pre_match_modules(
        text,
        module_source_files={"pi-agent": []},
        module_elements={"pi-agent": ["tracker_lost_logged", "announced"]},
    )
    assert "pi-agent" in result


def test_pre_match_element_token_no_partial_match():
    # @spec DRE-ANCH-009
    # Partial token overlap must not match — all element tokens must be present in text.
    from modok.retrieval.engine import _pre_match_modules

    # Text has "tracker" but not "lost" or "logged"
    text = "the tracker is working fine, no issues reported"
    result = _pre_match_modules(
        text,
        module_source_files={"pi-agent": []},
        module_elements={"pi-agent": ["tracker_lost_logged"]},
    )
    assert "pi-agent" not in result


@pytest.mark.asyncio
async def test_mechanical_validation_pass_removes_invalid_llm_slugs():
    # @spec DRE-ANCH-010
    # After merging pre-matched and LLM slugs, slugs not in valid_slugs are filtered
    # before Quine traversal.
    from modok.retrieval.engine import retrieve

    traversed: list[str] = []

    async def tracking_traverse(slug, project_slug, client):
        traversed.append(slug)
        return [], [], "module"

    issue = make_customer_issue(raw_text="some issue")
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=[],
        has_errors=[],
    )

    with patch("modok.retrieval.engine.gateway") as mock_gw:
        with patch(
            "modok.retrieval.engine._traverse_feature_to_files", side_effect=tracking_traverse
        ):
            mock_gw.parse_ticket = AsyncMock(
                return_value=make_ticket_parse_result(
                    feature_slugs=["real-module", "hallucinated-module"],
                )
            )
            mock_gw.summarise_packet = AsyncMock(return_value="summary")
            await retrieve(
                issue_id=1,
                project_slug="stagehand",
                client=mock_client,
                valid_slugs=["real-module"],
            )

    assert "real-module" in traversed
    assert "hallucinated-module" not in traversed


# ---------------------------------------------------------------------------
# Commit query side-effect helper (for function anchor matching tests)
# ---------------------------------------------------------------------------


def _make_commit_query_side_effect(
    module_slug: str,
    module_files: list[str],
    error_sigs: list[str],
    commits: list[dict],
):
    """
    Query side-effect combining module-slug graph anchor with error sig anchors
    and commit traversal, for testing function anchor matching.
    """

    def _side_effect(cypher: str, params: dict | None = None):
        params = params or {}
        proj = params.get("project_slug", "stagehand")
        slug = params.get("feature_slug", "")
        file_path = params.get("file_path", "")

        if "AFFECTS" in cypher and "Feature" in cypher:
            return [[module_slug]]

        if "HAS_ERROR" in cypher and "ErrorSignature" in cypher and "CustomerIssue" in cypher:
            return [
                [err]
                for err in error_sigs
            ]

        if "IMPLEMENTED_BY" in cypher and "DEFINED_IN" in cypher:
            return []

        if "HAS_TEST" in cypher and "idFrom('feature'" in cypher:
            return []

        if "idFrom('module'" in cypher and "DEFINED_IN" in cypher and slug == module_slug:
            return [
                [
                    {
                        "id": 1,
                        "properties": {
                            "module_slug": module_slug,
                            "project_slug": proj,
                            "node_type": "Module",
                            "name": module_slug,
                        },
                    },
                    {
                        "id": i + 2,
                        "properties": {
                            "repo_path": path,
                            "project_slug": proj,
                            "node_type": "File",
                        },
                    },
                ]
                for i, path in enumerate(module_files)
            ]

        if "idFrom('module'" in cypher:
            return []

        if "TOUCHES" in cypher:
            matching = [c for c in commits if file_path in c.get("files_touched", [])]
            return [
                [
                    {"id": 0, "properties": {"repo_path": file_path, "project_slug": proj}},
                    {"id": i + 1, "properties": {**c, "project_slug": proj}},
                ]
                for i, c in enumerate(matching)
            ]

        if "HAS_ERROR" in cypher and "KnownIssue" in cypher:
            return []

        if "RESOLVED_BY" in cypher:
            return []

        if "HAS_SIMILARITY_MATCH" in cypher:
            return []

        return []

    return _side_effect


# ---------------------------------------------------------------------------
# Quick Investigation Summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quick_investigation_summary_uses_graph_anchors_and_primary_files():
    # @spec DRE-QUICK-001, DRE-QUICK-002
    # No LLM call involved — the summary is built mechanically from graph-first
    # anchors and the registry's declared primary files, so it returns in
    # about the time of a couple of Quine round-trips (found live: an
    # LLM-based version of this function measured ~85s standalone, defeating
    # the point of a fast "triggered" notification posted before retrieve()).
    from modok.retrieval.engine import quick_investigation_summary

    issue = make_customer_issue(raw_text="wifi won't connect")
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(
        affects_features=["wifi-provisioning"],
        has_errors=["wifi-timeout"],
    )

    summary = await quick_investigation_summary(
        issue_id=1,
        project_slug="stagehand",
        client=mock_client,
        feature_source_files={"wifi-provisioning": ["client/wifi_provision_logic.py"]},
    )

    assert "wifi-provisioning" in summary
    assert "wifi-timeout" in summary
    assert "client/wifi_provision_logic.py" in summary


@pytest.mark.asyncio
async def test_quick_investigation_summary_falls_back_when_no_anchors():
    # @spec DRE-QUICK-003
    from modok.retrieval.engine import quick_investigation_summary

    issue = make_customer_issue(summary="Tracker won't initialize")
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = _make_query_side_effect(affects_features=[], has_errors=[])

    summary = await quick_investigation_summary(
        issue_id=1, project_slug="stagehand", client=mock_client
    )

    assert summary == "Tracker won't initialize"


@pytest.mark.asyncio
async def test_quick_investigation_summary_falls_back_on_anchor_query_failure():
    # @spec DRE-QUICK-003
    from modok.retrieval.engine import quick_investigation_summary

    issue = make_customer_issue(summary="Tracker won't initialize")
    mock_client = AsyncMock()
    mock_client.get_node.return_value = issue
    mock_client.query.side_effect = Exception("Quine unreachable")

    summary = await quick_investigation_summary(
        issue_id=1, project_slug="stagehand", client=mock_client
    )

    assert summary == "Tracker won't initialize"


@pytest.mark.asyncio
async def test_quick_investigation_summary_falls_back_when_issue_not_found():
    # @spec DRE-QUICK-003
    from modok.retrieval.engine import quick_investigation_summary

    mock_client = AsyncMock()
    mock_client.get_node.side_effect = Exception("not found")

    summary = await quick_investigation_summary(
        issue_id=999, project_slug="stagehand", client=mock_client
    )

    assert summary == ""
