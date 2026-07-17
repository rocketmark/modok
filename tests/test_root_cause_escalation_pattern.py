"""
Tests for the Root-Cause Escalation Pattern: grouping tickets by shared
Feature via the pre-existing AFFECTS edge, the root-cause-escalation-pattern
standing query, the shared _process_root_cause_escalation function, GitHub
issue-state polling as the reset signal, and scope/failure handling
(docs/llds/root-cause-escalation-pattern.md). Written before implementation
(Phase 5) — every targeted function does not exist yet, so every test fails
with ImportError/AttributeError until Phase 6.

Interface assumptions (Phase 6 may adjust; the behavioral requirements
RCESC-* do not depend on exact names):
  - src/modok/quine/models.py gains RootCauseEscalation
  - src/modok/webhook/models.py gains RootCauseEscalationData
  - src/modok/webhook/server.py gains _process_root_cause_escalation(client,
    project_slug, feature_slug) -> int and _create_or_retry_root_cause_escalation
  - src/modok/ingestion/github.py gains get_issue_state(github_repo, token,
    issue_number) -> str | None
  - src/modok/ingestion/ci_ingestion.py gains reconcile_root_cause_escalations
  - _standing_query_row_to_event_data / _required_fields_for_match
    (src/modok/webhook/server.py) gain a feature_slug-based branch

Specs verified: RCESC-NODE-001/002, RCESC-EDGE-001/002, RCESC-PROC-001
through 013, RCESC-CREATE-001 through 003, RCESC-GH-001 through 005,
RCESC-SQ-001 through 004, RCESC-POLL-001 through 003, RCESC-SCOPE-001
through 004.
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
# RCESC-NODE-001/002, RCESC-EDGE-001/002 — graph model
# ---------------------------------------------------------------------------


# @spec RCESC-NODE-001
def test_root_cause_escalation_node_has_expected_fields():
    from modok.quine.models import RootCauseEscalation

    node = RootCauseEscalation(
        node_type="RootCauseEscalation",
        project_slug="stagehand",
        feature_slug="wifi-provisioning",
        sequence=1,
        github_issue_number="",
        status="open",
        created_at="2026-07-16T00:00:00Z",
        standing_query_name="root-cause-escalation-pattern",
    )
    assert node.feature_slug == "wifi-provisioning"
    assert node.sequence == 1


# @spec RCESC-NODE-002
def test_status_never_referenced_in_decision_logic():
    import inspect
    from modok.webhook import server

    source = inspect.getsource(server._process_root_cause_escalation)
    assert ".status ==" not in source.replace('ci.status ==', '')
    assert "rce.status" not in source
    assert "node.status ==" not in source


# ---------------------------------------------------------------------------
# RCESC-SQ-001..004 — standing query definition
# ---------------------------------------------------------------------------


# @spec RCESC-SQ-001
def test_root_cause_pattern_is_loadable_distinct_id_no_aggregation():
    from modok.quine.standing_queries.loader import load_definition

    definition = load_definition("root-cause-escalation-pattern")
    assert definition.mode == "DistinctId"
    assert "WITH" not in definition.pattern
    assert "count(" not in definition.pattern


# @spec RCESC-SQ-002
def test_root_cause_pattern_keys_on_customer_issue():
    from modok.quine.standing_queries.loader import load_definition

    definition = load_definition("root-cause-escalation-pattern")
    assert "id(ci) AS id" in definition.pattern
    assert "id(feat) AS id" not in definition.pattern


# @spec RCESC-SQ-003
def test_root_cause_pattern_enrichment_has_no_aggregation():
    from modok.quine.standing_queries.loader import load_definition

    definition = load_definition("root-cause-escalation-pattern")
    assert "count(" not in definition.enrichment_query
    assert "feature_slug" in definition.enrichment_query


# @spec RCESC-SQ-001
def test_all_definitions_includes_root_cause_escalation_pattern():
    from modok.quine.standing_queries.loader import all_definitions

    names = [d.name for d in all_definitions()]
    assert "root-cause-escalation-pattern" in names


# @spec RCESC-SQ-004
def test_route_dispatches_feature_slug_row_to_root_cause_data():
    from modok.webhook.server import _standing_query_row_to_event_data
    from modok.webhook.models import RootCauseEscalationData

    row = {
        "project_slug": "stagehand",
        "feature_slug": "wifi-provisioning",
        "standing_query_name": "root-cause-escalation-pattern",
    }
    data = _standing_query_row_to_event_data(row)
    assert isinstance(data, RootCauseEscalationData)


# @spec RCESC-SQ-004
def test_route_still_dispatches_since_commit_row_to_file_escalation_data():
    from modok.webhook.server import _standing_query_row_to_event_data
    from modok.webhook.models import FileEscalationData

    row = {
        "project_slug": "stagehand", "file_path": "agent/src/shtp.c",
        "since_commit": "abc1234", "standing_query_name": "file-escalation-pattern",
    }
    data = _standing_query_row_to_event_data(row)
    assert isinstance(data, FileEscalationData)


# ---------------------------------------------------------------------------
# RCESC-PROC-* — _process_root_cause_escalation
# ---------------------------------------------------------------------------


# @spec RCESC-PROC-001, RCESC-PROC-002
@pytest.mark.asyncio
async def test_noop_when_fewer_than_three_qualify():
    from modok.webhook.server import _process_root_cause_escalation

    client = _mock_client()
    client.query = AsyncMock(side_effect=[
        [_issue_row(ticket_id=str(i)) for i in range(1, 3)],  # only 2 open
        [],  # nothing already linked
    ])
    result = await _process_root_cause_escalation(client, "stagehand", "wifi-provisioning")
    assert result == 0
    client.upsert_node.assert_not_called()


# @spec RCESC-PROC-001, RCESC-PROC-003
@pytest.mark.asyncio
async def test_pending_escalation_tickets_not_excluded_from_qualifying():
    from modok.webhook.server import _process_root_cause_escalation

    client = _mock_client()
    # 4 open tickets; 1 already linked to a PENDING (no issue number yet)
    # escalation for this feature — the exclusion query itself (per
    # RCESC-PROC-003) only returns rows for escalations with a real issue
    # number, so it must return nothing here for ticket "1" to still count.
    client.query = AsyncMock(side_effect=[
        [_issue_row(ticket_id=str(i)) for i in range(1, 5)],
        [],  # exclusion query returns nothing (pending escalation excluded by its own WHERE clause)
        [],  # latest-escalation lookup: none found in this simplified path... (see next test for full flow)
    ])
    with patch("modok.webhook.server._create_or_retry_root_cause_escalation", new=AsyncMock()) as mock_create:
        result = await _process_root_cause_escalation(client, "stagehand", "wifi-provisioning")
    assert result == 1
    mock_create.assert_called_once()
    called_qualifying = mock_create.call_args.args[-1]
    assert len(called_qualifying) == 4


# @spec RCESC-PROC-004, RCESC-PROC-005
@pytest.mark.asyncio
async def test_creates_at_sequence_one_when_none_exists():
    from modok.webhook.server import _process_root_cause_escalation

    client = _mock_client()
    client.query = AsyncMock(side_effect=[
        [_issue_row(ticket_id=str(i)) for i in range(1, 4)],
        [],
        [],  # no existing RootCauseEscalation
    ])
    with patch("modok.webhook.server._create_or_retry_root_cause_escalation", new=AsyncMock()) as mock_create:
        await _process_root_cause_escalation(client, "stagehand", "wifi-provisioning")
    assert mock_create.call_args.args[3] == 1  # sequence


# @spec RCESC-PROC-006
@pytest.mark.asyncio
async def test_retries_same_sequence_when_pending():
    from modok.webhook.server import _process_root_cause_escalation

    client = _mock_client()
    client.query = AsyncMock(side_effect=[
        [_issue_row(ticket_id=str(i)) for i in range(1, 4)],
        [],
        [[2, ""]],  # latest sequence=2, no issue number yet
    ])
    with patch("modok.webhook.server._create_or_retry_root_cause_escalation", new=AsyncMock()) as mock_create:
        await _process_root_cause_escalation(client, "stagehand", "wifi-provisioning")
    assert mock_create.call_args.args[3] == 2  # same sequence, not 3


# @spec RCESC-PROC-007, RCESC-PROC-008
@pytest.mark.asyncio
async def test_none_issue_state_writes_nothing():
    from modok.webhook.server import _process_root_cause_escalation

    client = _mock_client()
    client.query = AsyncMock(side_effect=[
        [_issue_row(ticket_id=str(i)) for i in range(1, 4)],
        [],
        [[1, "42"]],
    ])
    fake_project = type("P", (), {"slug": "stagehand", "github_repo": "acme/stagehand"})()
    fake_config = type("C", (), {"projects": [fake_project]})()
    with patch("modok.cli.config.ModokConfig.load", return_value=fake_config), \
         patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}), \
         patch("modok.ingestion.github.get_issue_state", new=AsyncMock(return_value=None)):
        result = await _process_root_cause_escalation(client, "stagehand", "wifi-provisioning")
    assert result == 0
    client.write_edge_by_parts.assert_not_called()


# @spec RCESC-PROC-009, RCESC-EDGE-002
@pytest.mark.asyncio
async def test_open_issue_state_appends_all_qualifying():
    from modok.webhook.server import _process_root_cause_escalation

    client = _mock_client()
    client.query = AsyncMock(side_effect=[
        [_issue_row(ticket_id=str(i)) for i in range(1, 4)],
        [],
        [[1, "42"]],
    ])
    fake_project = type("P", (), {"slug": "stagehand", "github_repo": "acme/stagehand"})()
    fake_config = type("C", (), {"projects": [fake_project]})()
    with patch("modok.cli.config.ModokConfig.load", return_value=fake_config), \
         patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}), \
         patch("modok.ingestion.github.get_issue_state", new=AsyncMock(return_value="open")), \
         patch("modok.ingestion.github.post_issue_comment", new=AsyncMock()) as mock_comment:
        result = await _process_root_cause_escalation(client, "stagehand", "wifi-provisioning")
    assert result == 1
    assert client.write_edge_by_parts.call_count == 3
    assert mock_comment.call_count == 3


# @spec RCESC-PROC-010
@pytest.mark.asyncio
async def test_closed_issue_state_opens_next_sequence():
    from modok.webhook.server import _process_root_cause_escalation

    client = _mock_client()
    client.query = AsyncMock(side_effect=[
        [_issue_row(ticket_id=str(i)) for i in range(1, 4)],
        [],
        [[1, "42"]],
    ])
    fake_project = type("P", (), {"slug": "stagehand", "github_repo": "acme/stagehand"})()
    fake_config = type("C", (), {"projects": [fake_project]})()
    with patch("modok.cli.config.ModokConfig.load", return_value=fake_config), \
         patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}), \
         patch("modok.ingestion.github.get_issue_state", new=AsyncMock(return_value="closed")), \
         patch("modok.webhook.server._create_or_retry_root_cause_escalation", new=AsyncMock()) as mock_create:
        await _process_root_cause_escalation(client, "stagehand", "wifi-provisioning")
    assert mock_create.call_args.args[3] == 2


# @spec RCESC-PROC-011
@pytest.mark.asyncio
async def test_missing_github_config_writes_nothing():
    from modok.webhook.server import _process_root_cause_escalation

    client = _mock_client()
    client.query = AsyncMock(side_effect=[
        [_issue_row(ticket_id=str(i)) for i in range(1, 4)],
        [],
        [[1, "42"]],
    ])
    with patch.dict("os.environ", {}, clear=True):
        result = await _process_root_cause_escalation(client, "stagehand", "wifi-provisioning")
    assert result == 0


# @spec RCESC-PROC-012
@pytest.mark.asyncio
async def test_exceptions_swallowed():
    from modok.webhook.server import _process_root_cause_escalation

    client = _mock_client()
    client.query = AsyncMock(side_effect=Exception("boom"))
    result = await _process_root_cause_escalation(client, "stagehand", "wifi-provisioning")
    assert result == 0


# @spec RCESC-PROC-013
@pytest.mark.asyncio
async def test_unknown_feature_returns_zero():
    from modok.webhook.server import _process_root_cause_escalation

    client = _mock_client()
    client.query = AsyncMock(side_effect=[[], []])
    result = await _process_root_cause_escalation(client, "stagehand", "no-such-feature")
    assert result == 0


# ---------------------------------------------------------------------------
# RCESC-CREATE-* — _create_or_retry_root_cause_escalation
# ---------------------------------------------------------------------------


# @spec RCESC-CREATE-001
@pytest.mark.asyncio
async def test_node_and_escalates_only_written_when_not_exists():
    from modok.webhook.server import _create_or_retry_root_cause_escalation

    client = _mock_client()
    client.node_exists_by_parts = AsyncMock(return_value=True)
    client.query = AsyncMock(return_value=[["42"]])  # already has issue number -> skip create

    await _create_or_retry_root_cause_escalation(
        client, "stagehand", "wifi-provisioning", 1, [_issue_row()]
    )
    client.upsert_node.assert_not_called()


# @spec RCESC-CREATE-002
@pytest.mark.asyncio
async def test_create_issue_skipped_when_already_has_number():
    from modok.webhook.server import _create_or_retry_root_cause_escalation

    client = _mock_client()
    client.node_exists_by_parts = AsyncMock(return_value=False)
    client.query = AsyncMock(return_value=[["42"]])  # race-check sees a number already set

    with patch("modok.ingestion.github.create_issue", new=AsyncMock()) as mock_create:
        await _create_or_retry_root_cause_escalation(
            client, "stagehand", "wifi-provisioning", 1, [_issue_row()]
        )
    mock_create.assert_not_called()


# @spec RCESC-CREATE-003
@pytest.mark.asyncio
async def test_issue_number_set_only_on_success():
    from modok.webhook.server import _create_or_retry_root_cause_escalation

    client = _mock_client()
    client.node_exists_by_parts = AsyncMock(return_value=False)
    client.query = AsyncMock(side_effect=[[[""]], None])  # race-check empty, then the SET call
    fake_project = type("P", (), {"slug": "stagehand", "github_repo": "acme/stagehand"})()
    fake_config = type("C", (), {"projects": [fake_project]})()

    with patch("modok.cli.config.ModokConfig.load", return_value=fake_config), \
         patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}), \
         patch("modok.ingestion.github.create_issue", new=AsyncMock(return_value="99")):
        await _create_or_retry_root_cause_escalation(
            client, "stagehand", "wifi-provisioning", 1, [_issue_row()]
        )
    set_call = client.query.call_args
    assert "99" in str(set_call)


# ---------------------------------------------------------------------------
# RCESC-GH-* — GitHub issue state / creation
# ---------------------------------------------------------------------------


# @spec RCESC-GH-001
@pytest.mark.asyncio
async def test_get_issue_state_returns_state_on_success():
    from modok.ingestion.github import get_issue_state

    with patch("httpx.AsyncClient") as mock_cls:
        mock_instance = mock_cls.return_value.__aenter__.return_value
        mock_instance.get = AsyncMock(return_value=httpx.Response(200, json={"state": "open"}))
        state = await get_issue_state("acme/stagehand", "tok", "42")
    assert state == "open"


# @spec RCESC-GH-002
@pytest.mark.asyncio
async def test_get_issue_state_404_returns_closed():
    from modok.ingestion.github import get_issue_state

    with patch("httpx.AsyncClient") as mock_cls:
        mock_instance = mock_cls.return_value.__aenter__.return_value
        mock_instance.get = AsyncMock(return_value=httpx.Response(404, json={}))
        state = await get_issue_state("acme/stagehand", "tok", "42")
    assert state == "closed"


# @spec RCESC-GH-003
@pytest.mark.asyncio
async def test_get_issue_state_network_error_returns_none():
    from modok.ingestion.github import get_issue_state

    with patch("httpx.AsyncClient") as mock_cls:
        mock_instance = mock_cls.return_value.__aenter__.return_value
        mock_instance.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        state = await get_issue_state("acme/stagehand", "tok", "42")  # must not raise
    assert state is None


# @spec label-color (direct, unscoped fix — orange visual alert per user request)
@pytest.mark.asyncio
async def test_ensure_label_color_patches_existing_label():
    from modok.ingestion.github import ensure_label_color

    with patch("httpx.AsyncClient") as mock_cls:
        mock_instance = mock_cls.return_value.__aenter__.return_value
        mock_instance.patch = AsyncMock(return_value=httpx.Response(200, json={}))
        await ensure_label_color("acme/stagehand", "tok", "modok-root-cause", "FFA500")
    mock_instance.patch.assert_called_once()
    assert "FFA500" in str(mock_instance.patch.call_args)


@pytest.mark.asyncio
async def test_ensure_label_color_creates_on_404():
    from modok.ingestion.github import ensure_label_color

    with patch("httpx.AsyncClient") as mock_cls:
        mock_instance = mock_cls.return_value.__aenter__.return_value
        mock_instance.patch = AsyncMock(return_value=httpx.Response(404, json={}))
        mock_instance.post = AsyncMock(return_value=httpx.Response(201, json={}))
        await ensure_label_color("acme/stagehand", "tok", "modok-root-cause", "FFA500")
    mock_instance.post.assert_called_once()


@pytest.mark.asyncio
async def test_ensure_label_color_swallows_failure():
    from modok.ingestion.github import ensure_label_color

    with patch("httpx.AsyncClient") as mock_cls:
        mock_instance = mock_cls.return_value.__aenter__.return_value
        mock_instance.patch = AsyncMock(side_effect=httpx.ConnectError("refused"))
        await ensure_label_color("acme/stagehand", "tok", "modok-root-cause", "FFA500")  # must not raise


@pytest.mark.asyncio
async def test_create_or_retry_ensures_label_color_before_creating_issue():
    from modok.webhook.server import _create_or_retry_root_cause_escalation

    client = _mock_client()
    client.node_exists_by_parts = AsyncMock(return_value=False)
    client.query = AsyncMock(return_value=[[""]])
    fake_project = type("P", (), {"slug": "stagehand", "github_repo": "acme/stagehand"})()
    fake_config = type("C", (), {"projects": [fake_project]})()

    call_order = []
    with patch("modok.cli.config.ModokConfig.load", return_value=fake_config), \
         patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}), \
         patch("modok.ingestion.github.ensure_label_color",
               new=AsyncMock(side_effect=lambda *a, **k: call_order.append("ensure_label_color"))), \
         patch("modok.ingestion.github.create_issue",
               new=AsyncMock(side_effect=lambda *a, **k: call_order.append("create_issue") or "99")):
        await _create_or_retry_root_cause_escalation(
            client, "stagehand", "wifi-provisioning", 1, [_issue_row()]
        )
    assert call_order == ["ensure_label_color", "create_issue"]


# @spec RCESC-GH-004, RCESC-GH-005
def test_root_cause_title_and_label():
    from modok.retrieval.formatting import format_root_cause_escalation_title

    title = format_root_cause_escalation_title("wifi-provisioning", 3, 1)
    assert title == "MODOK: wifi-provisioning has 3 open tickets in progress"
    assert "1" not in title.split("has")[0]  # no sequence number in the title


# ---------------------------------------------------------------------------
# RCESC-POLL-* — reconciliation sweep
# ---------------------------------------------------------------------------


# @spec RCESC-POLL-001
@pytest.mark.asyncio
async def test_reconcile_calls_shared_processor_per_feature():
    from modok.ingestion.ci_ingestion import reconcile_root_cause_escalations

    client = _mock_client(query_return=[["wifi-provisioning"], ["bluetooth-pairing"]])
    with patch("modok.webhook.server._process_root_cause_escalation", new=AsyncMock()) as mock_process:
        await reconcile_root_cause_escalations(client, "stagehand")
    assert mock_process.call_count == 2
    mock_process.assert_any_call(client, "stagehand", "wifi-provisioning")


# @spec RCESC-POLL-002
def test_reconcile_uses_lazy_import():
    import inspect
    from modok.ingestion import ci_ingestion

    source = inspect.getsource(ci_ingestion.reconcile_root_cause_escalations)
    assert "from modok.webhook.server import _process_root_cause_escalation" in source


# @spec RCESC-POLL-003
@pytest.mark.asyncio
async def test_ci_cycle_isolates_root_cause_sweep_failure():
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
         patch("modok.webhook.adapters.github_poll.reconcile_file_escalations", new=AsyncMock()), \
         patch("modok.webhook.adapters.github_poll.reconcile_root_cause_escalations",
               new=AsyncMock(side_effect=Exception("boom"))), \
         patch("modok.webhook.adapters.github_poll.save_last_workflow_sync"):
        await _run_ci_ingestion_cycle(client, project, "tok")  # must not raise


# ---------------------------------------------------------------------------
# RCESC-SCOPE-*
# ---------------------------------------------------------------------------


# @spec RCESC-SCOPE-001
def test_no_error_signature_grouping_in_source():
    import inspect
    from modok.webhook import server

    source = inspect.getsource(server._process_root_cause_escalation)
    assert "HAS_ERROR" not in source
    assert "ErrorSignature" not in source


# @spec RCESC-SCOPE-002
def test_exclusion_query_never_removes_includes_edges():
    import inspect
    from modok.webhook import server

    # INCLUDES is additive-only everywhere in this component — no DELETE,
    # no replace_edges_by_parts call targeting INCLUDES, ever.
    proc_source = inspect.getsource(server._process_root_cause_escalation)
    create_source = inspect.getsource(server._create_or_retry_root_cause_escalation)
    for source in (proc_source, create_source):
        assert "DELETE" not in source
        assert "replace_edges_by_parts" not in source


# @spec RCESC-SCOPE-004
def test_exclusion_query_scoped_per_feature():
    import inspect
    from modok.webhook import server

    source = inspect.getsource(server._process_root_cause_escalation)
    # The already-linked exclusion query filters on rce.feature_slug = $f —
    # a ticket linked to an escalation for a DIFFERENT feature is never
    # excluded from this feature's qualifying count.
    assert "rce.feature_slug = $f" in source


# @spec RCESC-SCOPE-003
def test_no_github_close_or_edit_calls_in_source():
    import inspect
    from modok.webhook import server

    proc_source = inspect.getsource(server._process_root_cause_escalation)
    create_source = inspect.getsource(server._create_or_retry_root_cause_escalation)
    for source in (proc_source, create_source):
        assert "state=closed" not in source.lower().replace(" ", "")
        assert "PATCH" not in source
