"""
Tests for the File Escalation Pattern: the `CustomerIssue -[:FLAGS]-> File`
write-back, the `file-escalation-pattern` standing query, the shared
`_process_file_escalation` function (used by both the `run_ingest_event`
branch and the reconciliation sweep), GitHub issue creation, and failure/
scope handling (docs/llds/file-escalation-pattern.md). Written before
implementation (Phase 5) — every function targeted here does not exist yet,
so every test fails with ImportError/AttributeError until Phase 6.

Interface assumptions (Phase 6 may adjust; the behavioral requirements
FESC-* do not depend on exact names):
  - src/modok/quine/models.py gains `FileEscalation` and `CustomerIssue.created_at`
  - src/modok/quine/standing_queries/file_escalation_pattern.yaml (loadable
    via the existing `load_definition`/`all_definitions`)
  - src/modok/webhook/models.py gains `FileEscalationData`
  - src/modok/webhook/server.py gains `_process_file_escalation(client,
    project_slug, file_path, since_commit) -> int`
  - src/modok/ingestion/github.py gains `create_issue(github_repo, token,
    title, body) -> str | None`
  - src/modok/ingestion/ci_ingestion.py gains `reconcile_file_escalations
    (client, project_slug) -> None`
  - `_maybe_notify_github` (src/modok/webhook/server.py) gains a FLAGS
    write-back step after its `retrieve()` call

Specs verified: FESC-NODE-001/002, FESC-EDGE-001/002, FESC-FLAGS-001
through 005, FESC-SQ-001/005/006, FESC-PROC-001 through 007 (incl. 001a),
FESC-GH-001 through 004, FESC-POLL-001 through 004, FESC-ERR-001 through
003, FESC-SCOPE-001 through 004.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


def _mock_client(query_return=None) -> MagicMock:
    client = MagicMock()
    client.upsert_node = AsyncMock()
    client.write_edge_by_parts = AsyncMock()
    client.replace_edges_by_parts = AsyncMock()
    client.node_exists_by_parts = AsyncMock(return_value=False)
    client.query = AsyncMock(return_value=query_return if query_return is not None else [])
    return client


def _issue_row(source_system="github", ticket_id="1", summary="s"):
    return [source_system, ticket_id, summary]


# ---------------------------------------------------------------------------
# FESC-NODE-001/002, FESC-EDGE-001/002 — graph model
# ---------------------------------------------------------------------------


# @spec FESC-NODE-001
def test_file_escalation_node_has_expected_fields():
    from modok.quine.models import FileEscalation

    node = FileEscalation(
        node_type="FileEscalation",
        project_slug="stagehand",
        file_path="agent/src/shtp.c",
        since_commit="abc1234",
        github_issue_number="",
        status="open",
        created_at="2026-07-16T00:00:00Z",
        standing_query_name="file-escalation-pattern",
    )
    assert node.file_path == "agent/src/shtp.c"
    assert node.since_commit == "abc1234"


# @spec FESC-NODE-002
def test_customer_issue_has_created_at_field():
    from modok.quine.models import CustomerIssue

    node = CustomerIssue(
        node_type="CustomerIssue",
        project_slug="stagehand",
        source_system="github",
        ticket_id="42",
        summary="s",
        status="open",
        created_at="2026-07-16T00:00:00Z",
    )
    assert node.created_at == "2026-07-16T00:00:00Z"


# @spec FESC-NODE-002
def test_customer_issue_created_at_set_at_ingestion_call_sites():
    from modok.webhook.models import CustomerIssueData, IngestEvent
    from modok.webhook.pipeline import run_ingest_event

    client = _mock_client()
    event = IngestEvent(
        kind="customer_issue",
        project_slug="stagehand",
        data=CustomerIssueData(
            source_system="github", ticket_id="1", summary="s", raw_text=None,
            status="open", ticket_kind="bug",
        ),
    )
    with patch("modok.webhook.server._link_anchors_resilient", new=AsyncMock()):
        run_ingest_event(event, client)
    written_node = client.upsert_node.call_args.args[0]
    assert written_node.created_at


# ---------------------------------------------------------------------------
# FESC-SQ-001/005/006 — standing query definition (unit-testable subset)
# ---------------------------------------------------------------------------


# @spec FESC-SQ-001
def test_file_escalation_pattern_is_loadable_and_distinct_id():
    from modok.quine.standing_queries.loader import load_definition

    definition = load_definition("file-escalation-pattern")
    assert definition.mode == "DistinctId"


# @spec FESC-SQ-001
def test_file_escalation_pattern_keys_on_customer_issue_not_file():
    from modok.quine.standing_queries.loader import load_definition

    definition = load_definition("file-escalation-pattern")
    assert "id(ci) AS id" in definition.pattern
    assert "id(f) AS id" not in definition.pattern


# @spec FESC-SQ-005
def test_file_escalation_pattern_does_not_bind_a_relationship_variable():
    from modok.quine.standing_queries.loader import load_definition

    definition = load_definition("file-escalation-pattern")
    # Quine live-rejects `[r:FLAGS]`-style relationship variable binding in a
    # standing-query pattern; this pattern must never reintroduce that shape.
    assert "[r:" not in definition.pattern


# @spec FESC-SQ-002
def test_file_escalation_pattern_enrichment_uses_aggregation_and_ordering():
    from modok.quine.standing_queries.loader import load_definition

    definition = load_definition("file-escalation-pattern")
    assert "count(distinct" in definition.enrichment_query
    assert "ORDER BY" in definition.enrichment_query
    assert "LIMIT 1" in definition.enrichment_query
    assert "n >= 3" in definition.enrichment_query


# @spec FESC-SQ-001
def test_all_definitions_includes_file_escalation_pattern():
    from modok.quine.standing_queries.loader import all_definitions

    names = [d.name for d in all_definitions()]
    assert "file-escalation-pattern" in names


# @spec FESC-SQ-006
def test_route_dispatches_since_commit_row_to_file_escalation_data():
    from modok.webhook.server import _standing_query_row_to_event_data

    row = {
        "project_slug": "stagehand",
        "file_path": "agent/src/shtp.c",
        "since_commit": "abc1234",
        "standing_query_name": "file-escalation-pattern",
    }
    data = _standing_query_row_to_event_data(row)
    from modok.webhook.models import FileEscalationData

    assert isinstance(data, FileEscalationData)


# @spec FESC-SQ-006
def test_route_dispatches_milestone_kind_row_unchanged():
    from modok.webhook.server import _standing_query_row_to_event_data
    from modok.webhook.models import MilestoneData

    row = {
        "project_slug": "stagehand", "source_system": "github", "ticket_id": "1",
        "milestone_kind": "ci_corroboration", "test_failure_id": "t1",
        "error_signature": "e1", "workflow_run_id": "w1",
        "standing_query_name": "ci-corroboration-pattern",
    }
    data = _standing_query_row_to_event_data(row)
    assert isinstance(data, MilestoneData)


# ---------------------------------------------------------------------------
# FESC-FLAGS-001..005 — CustomerIssue -[:FLAGS]-> File write-back
# ---------------------------------------------------------------------------


def make_packet(scored_candidates):
    from modok.retrieval.models import DebugPacket, IssueAnchors, IssueSummary

    return DebugPacket(
        issue=IssueSummary(summary="s", anchors=IssueAnchors(features=[], errors=[], symptoms=[])),
        affected_areas=[],
        relevant_files=[],
        relevant_tests=[],
        known_issues=[],
        prior_fixes=[],
        scored_candidates=scored_candidates,
        summary="s",
    )


def _candidate(path, kind="source", confidence="high"):
    from modok.retrieval.models import ScoredCandidate

    return ScoredCandidate(path=path, kind=kind, score=20.0, confidence=confidence, evidence=[])


async def _run_maybe_notify_github(client, packet):
    from modok.webhook.server import _maybe_notify_github

    fake_project = type("P", (), {"slug": "stagehand", "github_repo": "acme/stagehand"})()
    fake_config = type("C", (), {"projects": [fake_project]})()
    with patch("modok.cli.config.ModokConfig.load", return_value=fake_config), \
         patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}), \
         patch("modok.retrieval.engine.quick_investigation_summary", new=AsyncMock(return_value="")), \
         patch("modok.retrieval.engine.retrieve", new=AsyncMock(return_value=packet)), \
         patch("modok.ingestion.github.post_issue_comment", new=AsyncMock()):
        client.query = AsyncMock(return_value=[["node-id"]])
        await _maybe_notify_github(
            client=client, project_slug="stagehand", source_system="github",
            ticket_id="42", investigation_id="inv-42", standing_query_name="new-bug-report-pattern",
        )


# @spec FESC-FLAGS-001, FESC-FLAGS-002, FESC-SCOPE-001
@pytest.mark.asyncio
async def test_flags_written_for_high_confidence_source_candidates_only():
    client = _mock_client()
    packet = make_packet([
        _candidate("agent/src/shtp.c", kind="source", confidence="high"),
        _candidate("agent/src/other.c", kind="source", confidence="medium"),
        _candidate("agent/tests/test_shtp.c", kind="test", confidence="high"),
    ])
    await _run_maybe_notify_github(client, packet)

    call = client.replace_edges_by_parts.call_args
    assert call.args[0] == ("customer-issue", "stagehand", "github", "42")
    assert call.args[1] == "FLAGS"
    targets = call.args[2]
    assert ("file", "stagehand", "agent/src/shtp.c") in targets
    assert len(targets) == 1


# @spec FESC-FLAGS-002
@pytest.mark.asyncio
async def test_flags_reconciled_to_empty_set_when_no_high_confidence_candidates():
    client = _mock_client()
    packet = make_packet([_candidate("agent/src/other.c", kind="source", confidence="medium")])
    await _run_maybe_notify_github(client, packet)

    call = client.replace_edges_by_parts.call_args
    assert call.args[1] == "FLAGS"
    assert call.args[2] == []


# @spec FESC-FLAGS-004
@pytest.mark.asyncio
async def test_flags_not_written_for_non_github_source():
    from modok.webhook.server import _maybe_notify_github

    client = _mock_client()
    await _maybe_notify_github(
        client=client, project_slug="stagehand", source_system="jira",
        ticket_id="42", investigation_id="inv-42", standing_query_name="new-bug-report-pattern",
    )
    client.replace_edges_by_parts.assert_not_called()


# @spec FESC-FLAGS-005
def test_process_milestone_does_not_write_flags():
    import inspect
    from modok.webhook import server

    source = inspect.getsource(server._process_milestone)
    assert "FLAGS" not in source
    assert "replace_edges_by_parts" not in source


# ---------------------------------------------------------------------------
# FESC-PROC-* — _process_file_escalation
# ---------------------------------------------------------------------------


# @spec FESC-PROC-002
@pytest.mark.asyncio
async def test_process_file_escalation_noop_when_fewer_than_three_qualify():
    from modok.webhook.server import _process_file_escalation

    client = _mock_client(query_return=[_issue_row(), _issue_row()])
    await _process_file_escalation(client, "stagehand", "agent/src/shtp.c", "abc1234")
    client.upsert_node.assert_not_called()


# @spec FESC-PROC-003, FESC-EDGE-001, FESC-EDGE-002
@pytest.mark.asyncio
async def test_process_file_escalation_creates_node_edges_then_issue():
    from modok.webhook.server import _process_file_escalation

    client = _mock_client(query_return=[_issue_row(ticket_id=str(i)) for i in range(1, 4)])
    client.node_exists_by_parts = AsyncMock(return_value=False)
    fake_project = type("P", (), {"slug": "stagehand", "github_repo": "acme/stagehand"})()
    fake_config = type("C", (), {"projects": [fake_project]})()

    call_order = []
    client.upsert_node.side_effect = lambda *a, **k: call_order.append("upsert_node")
    client.write_edge_by_parts.side_effect = lambda *a, **k: call_order.append("write_edge_by_parts")

    with patch("modok.cli.config.ModokConfig.load", return_value=fake_config), \
         patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}), \
         patch("modok.ingestion.github.create_issue", new=AsyncMock(return_value="99")) as mock_create:
        await _process_file_escalation(client, "stagehand", "agent/src/shtp.c", "abc1234")

    assert call_order[0] == "upsert_node"
    assert "write_edge_by_parts" in call_order
    mock_create.assert_called_once()
    # ESCALATES to the File, INCLUDES to each of the 3 issues = 4 edge writes
    assert client.write_edge_by_parts.call_count == 4


# @spec FESC-PROC-003, FESC-GH-004
@pytest.mark.asyncio
async def test_process_file_escalation_leaves_issue_number_empty_on_create_failure():
    from modok.webhook.server import _process_file_escalation

    client = _mock_client(query_return=[_issue_row(ticket_id=str(i)) for i in range(1, 4)])
    client.node_exists_by_parts = AsyncMock(return_value=False)
    fake_project = type("P", (), {"slug": "stagehand", "github_repo": "acme/stagehand"})()
    fake_config = type("C", (), {"projects": [fake_project]})()

    with patch("modok.cli.config.ModokConfig.load", return_value=fake_config), \
         patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}), \
         patch("modok.ingestion.github.create_issue", new=AsyncMock(return_value=None)):
        await _process_file_escalation(client, "stagehand", "agent/src/shtp.c", "abc1234")

    written_node = client.upsert_node.call_args.args[0]
    assert written_node.github_issue_number == ""


# @spec FESC-PROC-004
@pytest.mark.asyncio
async def test_process_file_escalation_retries_creation_when_issue_number_empty():
    from modok.webhook.server import _process_file_escalation

    client = _mock_client()
    client.node_exists_by_parts = AsyncMock(return_value=True)
    client.query = AsyncMock(side_effect=[
        [_issue_row(ticket_id=str(i)) for i in range(1, 4)],  # qualifying
        [[""]],  # github_issue_number fetch -> still empty, pending/failed
        None,  # SET github_issue_number after successful (re)creation
    ])
    fake_project = type("P", (), {"slug": "stagehand", "github_repo": "acme/stagehand"})()
    fake_config = type("C", (), {"projects": [fake_project]})()

    with patch("modok.cli.config.ModokConfig.load", return_value=fake_config), \
         patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}), \
         patch("modok.ingestion.github.create_issue", new=AsyncMock(return_value="100")) as mock_create, \
         patch("modok.ingestion.github.post_issue_comment", new=AsyncMock()) as mock_comment:
        await _process_file_escalation(client, "stagehand", "agent/src/shtp.c", "abc1234")

    mock_create.assert_called_once()
    mock_comment.assert_not_called()


# @spec FESC-PROC-005
@pytest.mark.asyncio
async def test_process_file_escalation_posts_update_comment_for_new_issue():
    from modok.webhook.server import _process_file_escalation

    client = _mock_client()
    client.node_exists_by_parts = AsyncMock(return_value=True)
    # Existing INCLUDES targets are tickets 1-3; ticket 4 is new.
    client.query = AsyncMock(side_effect=[
        [_issue_row(ticket_id=str(i)) for i in range(1, 5)],  # qualifying
        [["99"]],  # github_issue_number fetch
        [["1"], ["2"], ["3"]],  # existing INCLUDES targets
    ])
    fake_project = type("P", (), {"slug": "stagehand", "github_repo": "acme/stagehand"})()
    fake_config = type("C", (), {"projects": [fake_project]})()

    with patch("modok.cli.config.ModokConfig.load", return_value=fake_config), \
         patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}), \
         patch("modok.ingestion.github.post_issue_comment", new=AsyncMock()) as mock_comment:
        await _process_file_escalation(client, "stagehand", "agent/src/shtp.c", "abc1234")

    mock_comment.assert_called_once()
    assert mock_comment.call_args.args[2] == "99"


# @spec FESC-PROC-005
@pytest.mark.asyncio
async def test_process_file_escalation_noop_when_no_new_issues():
    from modok.webhook.server import _process_file_escalation

    client = _mock_client()
    client.node_exists_by_parts = AsyncMock(return_value=True)
    client.query = AsyncMock(side_effect=[
        [_issue_row(ticket_id=str(i)) for i in range(1, 4)],  # qualifying (1, 2, 3)
        [["99"]],  # github_issue_number fetch
        [["1"], ["2"], ["3"]],  # existing INCLUDES targets == exactly the qualifying set
    ])

    with patch("modok.ingestion.github.post_issue_comment", new=AsyncMock()) as mock_comment:
        await _process_file_escalation(client, "stagehand", "agent/src/shtp.c", "abc1234")

    mock_comment.assert_not_called()
    client.write_edge_by_parts.assert_not_called()


# @spec FESC-PROC-006
@pytest.mark.asyncio
async def test_process_file_escalation_does_not_filter_by_ticket_status():
    import inspect
    from modok.webhook import server

    source = inspect.getsource(server._process_file_escalation)
    assert "ticket_kind" not in source
    assert ".status" not in source.replace("status=\"open\"", "")


# @spec FESC-PROC-007, FESC-ERR-003
@pytest.mark.asyncio
async def test_process_file_escalation_swallows_exceptions():
    from modok.webhook.server import _process_file_escalation

    client = _mock_client()
    client.query = AsyncMock(side_effect=Exception("boom"))
    await _process_file_escalation(client, "stagehand", "agent/src/shtp.c", "abc1234")  # must not raise


# ---------------------------------------------------------------------------
# FESC-GH-001..004 — GitHub issue creation
# ---------------------------------------------------------------------------


# @spec FESC-GH-001
@pytest.mark.asyncio
async def test_create_issue_returns_number_on_success():
    from modok.ingestion.github import create_issue

    with patch("httpx.AsyncClient") as mock_cls:
        mock_instance = mock_cls.return_value.__aenter__.return_value
        mock_instance.post = AsyncMock(return_value=httpx.Response(201, json={"number": 123}))
        number = await create_issue("acme/stagehand", "tok", "title", "body")
    assert number == "123"


# @spec FESC-GH-001
@pytest.mark.asyncio
async def test_create_issue_returns_none_on_failure():
    from modok.ingestion.github import create_issue

    with patch("httpx.AsyncClient") as mock_cls:
        mock_instance = mock_cls.return_value.__aenter__.return_value
        mock_instance.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        number = await create_issue("acme/stagehand", "tok", "title", "body")  # must not raise
    assert number is None


# @spec FESC-GH-002
@pytest.mark.asyncio
async def test_process_file_escalation_skips_creation_when_token_missing():
    from modok.webhook.server import _process_file_escalation

    client = _mock_client(query_return=[_issue_row(ticket_id=str(i)) for i in range(1, 4)])
    client.node_exists_by_parts = AsyncMock(return_value=False)
    fake_project = type("P", (), {"slug": "stagehand", "github_repo": "acme/stagehand"})()
    fake_config = type("C", (), {"projects": [fake_project]})()

    with patch("modok.cli.config.ModokConfig.load", return_value=fake_config), \
         patch.dict("os.environ", {}, clear=True), \
         patch("modok.ingestion.github.create_issue", new=AsyncMock()) as mock_create:
        await _process_file_escalation(client, "stagehand", "agent/src/shtp.c", "abc1234")

    mock_create.assert_not_called()


# @spec FESC-GH-003
def test_escalation_title_format():
    from modok.retrieval.formatting import format_file_escalation_title

    title = format_file_escalation_title("agent/src/shtp.c", 3, "abc1234567")
    assert title == "MODOK: agent/src/shtp.c flagged by 3 tickets since abc1234"


# ---------------------------------------------------------------------------
# FESC-POLL-001..004 — reconciliation sweep
# ---------------------------------------------------------------------------


# @spec FESC-POLL-001
@pytest.mark.asyncio
async def test_reconcile_file_escalations_calls_shared_processor():
    from modok.ingestion.ci_ingestion import reconcile_file_escalations

    client = _mock_client(query_return=[["agent/src/shtp.c", "abc1234"], ["agent/src/other.c", "def5678"]])
    with patch("modok.webhook.server._process_file_escalation", new=AsyncMock()) as mock_process:
        await reconcile_file_escalations(client, "stagehand")

    assert mock_process.call_count == 2
    mock_process.assert_any_call(client, "stagehand", "agent/src/shtp.c", "abc1234")


# @spec FESC-PROC-001a
def test_reconcile_file_escalations_uses_lazy_import():
    import inspect
    from modok.ingestion import ci_ingestion

    source = inspect.getsource(ci_ingestion.reconcile_file_escalations)
    assert "from modok.webhook.server import _process_file_escalation" in source


# @spec FESC-POLL-002
def test_reconcile_file_escalations_has_no_sweep_specific_dedup_logic():
    import inspect
    from modok.ingestion import ci_ingestion

    source = inspect.getsource(ci_ingestion.reconcile_file_escalations)
    # Idempotency must come entirely from _process_file_escalation's own
    # node_exists_by_parts check — the sweep itself does no filtering beyond
    # the threshold query and delegating to the shared function.
    assert "node_exists_by_parts" not in source
    assert "github_issue_number" not in source


# @spec FESC-POLL-004
@pytest.mark.asyncio
async def test_sweep_reprocessing_a_fully_handled_file_is_a_noop():
    from modok.ingestion.ci_ingestion import reconcile_file_escalations

    client = _mock_client(query_return=[["agent/src/shtp.c", "abc1234"]])
    # _process_file_escalation itself is exercised directly elsewhere
    # (test_process_file_escalation_noop_when_no_new_issues); here we only
    # need to confirm the sweep doesn't add its own duplicate-issue risk on
    # top of that already-verified idempotency.
    with patch("modok.webhook.server._process_file_escalation", new=AsyncMock(return_value=0)) as mock_process:
        await reconcile_file_escalations(client, "stagehand")
    mock_process.assert_called_once_with(client, "stagehand", "agent/src/shtp.c", "abc1234")


# @spec FESC-POLL-003
@pytest.mark.asyncio
async def test_ci_ingestion_cycle_isolates_file_escalation_sweep_failure():
    from modok.webhook.adapters.github_poll import _run_ci_ingestion_cycle

    project = type("P", (), {
        "slug": "stagehand", "github_repo": "acme/stagehand",
        "last_workflow_sync": None, "ci_artifact_pattern": None,
    })()
    client = _mock_client()
    with patch("modok.webhook.adapters.github_poll.discover_workflow_runs", new=AsyncMock(return_value=[])), \
         patch("modok.webhook.adapters.github_poll.find_expansion_backlog", new=AsyncMock(return_value=[])), \
         patch("modok.webhook.adapters.github_poll.reconcile_commit_edges", new=AsyncMock()), \
         patch("modok.webhook.adapters.github_poll.reconcile_test_execution_links", new=AsyncMock()), \
         patch("modok.webhook.adapters.github_poll.reconcile_file_escalations",
               new=AsyncMock(side_effect=Exception("boom"))), \
         patch("modok.webhook.adapters.github_poll.save_last_workflow_sync"):
        await _run_ci_ingestion_cycle(client, project, "tok")  # must not raise


# ---------------------------------------------------------------------------
# FESC-ERR-001/002 — failure handling
# ---------------------------------------------------------------------------


# @spec FESC-ERR-001, FESC-ERR-002
def test_enrichment_query_requires_touches_edge_and_strict_recency():
    from modok.quine.standing_queries.loader import load_definition

    definition = load_definition("file-escalation-pattern")
    assert "TOUCHES" in definition.enrichment_query
    assert "ci2.created_at > c.timestamp" in definition.enrichment_query


# ---------------------------------------------------------------------------
# FESC-SCOPE-001..004
# ---------------------------------------------------------------------------


# @spec FESC-SCOPE-001
@pytest.mark.asyncio
async def test_test_kind_candidates_excluded_from_flags():
    client = _mock_client()
    packet = make_packet([_candidate("agent/tests/test_shtp.c", kind="test", confidence="high")])
    await _run_maybe_notify_github(client, packet)

    call = client.replace_edges_by_parts.call_args
    assert call.args[2] == []


# @spec FESC-SCOPE-002
def test_file_escalation_status_field_only_ever_open_in_source():
    import inspect
    from modok.webhook import server

    source = inspect.getsource(server._process_file_escalation)
    assert 'status="resolved"' not in source
    assert 'status="stale"' not in source


# @spec FESC-SCOPE-003
def test_no_retroactive_edit_close_or_comment_on_flags_removal():
    import inspect
    from modok.webhook import server

    # Nothing in this component's source watches for a removed FLAGS edge or
    # closes/edits an already-created escalation issue in response to one —
    # replace_edges_by_parts (FLAGS reconciliation) and _process_file_escalation
    # are entirely independent code paths with no cross-reference.
    flags_source = inspect.getsource(server._maybe_notify_github)
    process_source = inspect.getsource(server._process_file_escalation)
    for source in (flags_source, process_source):
        assert "close" not in source.lower()


# @spec FESC-SCOPE-004
def test_no_repo_path_rename_migration_logic():
    import inspect
    from modok.webhook import server

    source = inspect.getsource(server._process_file_escalation)
    # The re-derivation query is keyed on the file_path string parameter as
    # given — no lookup for a File node's prior/alternate repo_path, no
    # rename detection, no merge of one FileEscalation into another.
    assert "rename" not in source.lower()
    assert "old_" not in source.lower()
