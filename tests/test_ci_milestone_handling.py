"""
Tests for the Investigation+Milestone model (docs/llds/continuous-ci-ingestion.md
§ Investigation and Milestone Model) and the /standing-query/result route's
payload-shape dispatch (SQ-ROUTE-007). Written before implementation
(Phase 5) — MilestoneData, the "milestone" IngestEvent kind, and
run_ingest_event's milestone branch do not exist yet, so most tests here fail
until Phase 6.

Specs verified: SQ-ROUTE-007, SQ-MILE-001 through SQ-MILE-012.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modok.webhook.models import WebhookConfig
from modok.webhook.server import build_app


def _make_config():
    return WebhookConfig(github_secret="test-secret", bearer_token="test-token")


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch):
    """Prevent tests that don't explicitly mock GitHub write-back config from
    depending on the developer machine's real ~/.modok/config.toml — most
    tests here reach _maybe_post_ci_corroboration_comment's config resolution
    without patching it. Same pattern as test_ingestion_github.py's
    _isolated_config; a test's own `with patch("modok.cli.config.ModokConfig.load")`
    block takes precedence for its duration."""
    from modok.cli.config import ModokConfig

    def _raise_no_config():
        raise FileNotFoundError("no ~/.modok/config.toml in test environment")

    monkeypatch.setattr(ModokConfig, "load", staticmethod(_raise_no_config))


@pytest.fixture()
def mock_quine():
    client = AsyncMock()
    client.ping = AsyncMock(return_value=True)
    client.upsert_node = AsyncMock(return_value=None)
    client.write_edge_by_parts = AsyncMock(return_value=None)
    client.node_exists_by_parts = AsyncMock(return_value=False)
    return client


@pytest.fixture()
def app(mock_quine):
    return build_app(config=_make_config(), quine_client=mock_quine, known_project_slugs={"test-project"})


@pytest.fixture()
def client(app):
    return TestClient(app, raise_server_exceptions=False)


def _milestone_match_object(**overrides) -> dict:
    defaults = dict(
        project_slug="test-project",
        source_system="github",
        ticket_id="42",
        workflow_run_id="100",
        test_failure_id="TestDb::test_connect",
        error_signature="DB_TIMEOUT",
        milestone_kind="ci-corroborated",
        standing_query_name="ci-corroboration-pattern",
    )
    defaults.update(overrides)
    return defaults


def _investigation_match_object(**overrides) -> dict:
    defaults = dict(
        project_slug="test-project",
        source_system="github",
        ticket_id="42",
        known_issue_id="ki-1",
        fix_id="fix-1",
    )
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# SQ-ROUTE-007 — same route, dispatch on payload shape
# ---------------------------------------------------------------------------


# @spec SQ-ROUTE-007
def test_standing_query_result_dispatches_milestone_kind_when_milestone_kind_present(client):
    captured = {}

    def fake_run_ingest_event(event, quine_client):
        captured["event"] = event
        return 1

    with patch("modok.webhook.server.run_ingest_event", side_effect=fake_run_ingest_event):
        resp = client.post("/standing-query/result", json=_milestone_match_object())
    assert resp.status_code == 200
    assert captured["event"].kind == "milestone"


# @spec SQ-ROUTE-007
def test_standing_query_result_dispatches_investigation_kind_when_milestone_kind_absent(client):
    captured = {}

    def fake_run_ingest_event(event, quine_client):
        captured["event"] = event
        return 1

    with patch("modok.webhook.server.run_ingest_event", side_effect=fake_run_ingest_event):
        resp = client.post("/standing-query/result", json=_investigation_match_object())
    assert resp.status_code == 200
    assert captured["event"].kind == "investigation"


# ---------------------------------------------------------------------------
# SQ-MILE-001/002 — stable Investigation identity, get-or-create
# ---------------------------------------------------------------------------


def _mock_client(milestone_exists: bool = False) -> MagicMock:
    client = MagicMock()
    client.upsert_node = AsyncMock()
    client.write_edge_by_parts = AsyncMock()
    client.node_exists_by_parts = AsyncMock(return_value=milestone_exists)
    client.query = AsyncMock(return_value=[])
    return client


def _milestone_event():
    from modok.webhook.models import IngestEvent, MilestoneData

    return IngestEvent(
        kind="milestone",
        project_slug="stagehand",
        data=MilestoneData(
            source_system="github",
            ticket_id="42",
            milestone_kind="ci-corroborated",
            standing_query_name="ci-corroboration-pattern",
            workflow_run_id="100",
            test_failure_id="TestDb::test_connect",
            error_signature="DB_TIMEOUT",
        ),
    )


# @spec SQ-MILE-001
def test_milestone_branch_investigation_id_is_stable_regardless_of_evidence():
    from modok.webhook.server import run_ingest_event

    client = _mock_client(milestone_exists=False)
    run_ingest_event(_milestone_event(), client)

    inv_upserts = [c[0][0] for c in client.upsert_node.call_args_list if type(c[0][0]).__name__ == "Investigation"]
    assert len(inv_upserts) == 1
    assert inv_upserts[0].investigation_id == "github-42"


# @spec SQ-MILE-002
def test_milestone_branch_upserts_investigation_unconditionally():
    from modok.webhook.server import run_ingest_event

    client = _mock_client(milestone_exists=False)
    run_ingest_event(_milestone_event(), client)

    inv_upserts = [c[0][0] for c in client.upsert_node.call_args_list if type(c[0][0]).__name__ == "Investigation"]
    assert len(inv_upserts) == 1

    invest_edges = [str(c) for c in client.write_edge_by_parts.call_args_list]
    assert any("INVESTIGATES" in c for c in invest_edges)


# ---------------------------------------------------------------------------
# SQ-MILE-003/004 — distinct milestone identity, dedup by that identity
# ---------------------------------------------------------------------------


# @spec SQ-MILE-003
def test_milestone_identity_includes_error_signature_and_test_failure_key():
    """The parts tuple checked for dedup (and later used to address the
    InvestigationMilestone node) must vary with test_failure_id and
    error_signature, not just milestone_kind — otherwise two distinct
    corroborating failures on the same issue would collide on one identity."""
    from modok.webhook.server import run_ingest_event

    client = _mock_client(milestone_exists=False)
    run_ingest_event(_milestone_event(), client)

    milestone_parts = client.node_exists_by_parts.call_args[0][0]
    assert milestone_parts[0] == "investigation-milestone"
    assert "TestDb::test_connect" in milestone_parts  # test_failure_id
    assert "DB_TIMEOUT" in milestone_parts  # error_signature


# @spec SQ-MILE-004
def test_milestone_dedup_skips_all_writes_when_milestone_already_exists():
    from modok.webhook.server import run_ingest_event

    client = _mock_client(milestone_exists=True)
    with patch("modok.webhook.server.post_issue_comment", new=AsyncMock()) as mock_post:
        run_ingest_event(_milestone_event(), client)

    milestone_upserts = [
        c[0][0] for c in client.upsert_node.call_args_list
        if type(c[0][0]).__name__ == "InvestigationMilestone"
    ]
    assert milestone_upserts == []
    mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# SQ-MILE-005 — milestone/evidence write always happens for a new milestone
# ---------------------------------------------------------------------------


# @spec SQ-MILE-005
def test_new_milestone_writes_has_milestone_and_evidenced_by_edges():
    from modok.webhook.server import run_ingest_event

    client = _mock_client(milestone_exists=False)
    run_ingest_event(_milestone_event(), client)

    calls = [str(c) for c in client.write_edge_by_parts.call_args_list]
    assert any("HAS_MILESTONE" in c for c in calls)
    assert any("EVIDENCED_BY" in c for c in calls)


# ---------------------------------------------------------------------------
# SQ-MILE-006 — multiple distinct corroborations accumulate, never overwrite
# ---------------------------------------------------------------------------


# @spec SQ-MILE-006
def test_two_distinct_corroborating_failures_produce_two_milestones_one_investigation():
    from modok.webhook.models import IngestEvent, MilestoneData
    from modok.webhook.server import run_ingest_event

    client = _mock_client(milestone_exists=False)

    event_a = _milestone_event()
    event_b = IngestEvent(
        kind="milestone",
        project_slug="stagehand",
        data=MilestoneData(
            source_system="github", ticket_id="42", milestone_kind="ci-corroborated",
            standing_query_name="ci-corroboration-pattern", workflow_run_id="105",
            test_failure_id="TestDb::test_connect_2", error_signature="DB_TIMEOUT",
        ),
    )

    run_ingest_event(event_a, client)
    run_ingest_event(event_b, client)

    inv_upserts = [c[0][0] for c in client.upsert_node.call_args_list if type(c[0][0]).__name__ == "Investigation"]
    milestone_upserts = [
        c[0][0] for c in client.upsert_node.call_args_list
        if type(c[0][0]).__name__ == "InvestigationMilestone"
    ]
    # Investigation upserted twice (get-or-create both times) but always to the
    # same stable identity; two genuinely distinct milestones.
    assert all(n.investigation_id == "github-42" for n in inv_upserts)
    assert len(milestone_upserts) == 2


# ---------------------------------------------------------------------------
# SQ-MILE-007 — workflow_run_id/test_failure_id are presentation-only
# ---------------------------------------------------------------------------


# @spec SQ-MILE-007
def test_milestone_data_fields_not_part_of_investigation_identity():
    """Two MilestoneData instances differing in workflow_run_id/test_failure_id/
    error_signature must still resolve to the SAME investigation_id via the
    real _milestone_investigation_id function (source_system + ticket_id
    only) — those three fields play no role in Investigation identity, only
    in the (possibly-posted) comment text."""
    from modok.webhook.models import MilestoneData
    from modok.webhook.server import _milestone_investigation_id

    a = MilestoneData(
        source_system="github", ticket_id="42", milestone_kind="ci-corroborated",
        standing_query_name="ci-corroboration-pattern", workflow_run_id="100",
        test_failure_id="TestDb::test_connect", error_signature="DB_TIMEOUT",
    )
    b = MilestoneData(
        source_system="github", ticket_id="42", milestone_kind="ci-corroborated",
        standing_query_name="ci-corroboration-pattern", workflow_run_id="999",
        test_failure_id="TestDb::test_other", error_signature="CONN_RESET",
    )
    assert _milestone_investigation_id(a) == _milestone_investigation_id(b) == "github-42"


# ---------------------------------------------------------------------------
# SQ-MILE-009 — comment posted only on first CI-corroboration transition
# ---------------------------------------------------------------------------


# @spec SQ-MILE-009
def test_first_milestone_for_investigation_posts_comment():
    from modok.webhook.server import run_ingest_event

    client = _mock_client(milestone_exists=False)
    # No prior ci-corroborated milestone exists on this Investigation yet.
    client.query = AsyncMock(return_value=[])
    with patch("modok.webhook.server.post_issue_comment", new=AsyncMock()) as mock_post, \
         patch("modok.cli.config.ModokConfig.load") as mock_cfg:
        mock_cfg.return_value.project.return_value.github_repo = "owner/repo"
        with patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}):
            run_ingest_event(_milestone_event(), client)

    mock_post.assert_called_once()


# @spec SQ-MILE-009
def test_second_milestone_for_same_investigation_posts_no_comment():
    from modok.webhook.models import IngestEvent, MilestoneData
    from modok.webhook.server import run_ingest_event

    client = _mock_client(milestone_exists=False)
    # A prior ci-corroborated milestone already exists on this Investigation.
    client.query = AsyncMock(return_value=[[{"properties": {"milestone_kind": "ci-corroborated"}}]])

    second_event = IngestEvent(
        kind="milestone",
        project_slug="stagehand",
        data=MilestoneData(
            source_system="github", ticket_id="42", milestone_kind="ci-corroborated",
            standing_query_name="ci-corroboration-pattern", workflow_run_id="105",
            test_failure_id="TestDb::test_connect_2", error_signature="DB_TIMEOUT",
        ),
    )
    with patch("modok.webhook.server.post_issue_comment", new=AsyncMock()) as mock_post:
        run_ingest_event(second_event, client)

    # The milestone/evidence write still happens (SQ-MILE-005) even though no
    # comment is posted.
    milestone_upserts = [
        c[0][0] for c in client.upsert_node.call_args_list
        if type(c[0][0]).__name__ == "InvestigationMilestone"
    ]
    assert len(milestone_upserts) == 1
    mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# SQ-MILE-010/011 — comment wording constraints
# ---------------------------------------------------------------------------


# @spec SQ-MILE-010, SQ-MILE-011, SQ-MILE-012
def test_first_transition_comment_wording_constraints():
    from modok.retrieval.formatting import format_ci_corroboration_milestone_markdown

    body = format_ci_corroboration_milestone_markdown(
        error_signature="DB_TIMEOUT",
        test_failure_id="TestDb::test_connect",
        workflow_name="CI",
        head_sha="abc123",
        workflow_run_id="100",
    )
    lowered = body.lower()
    # Must not claim to open/start a new investigation.
    assert "opened" not in lowered
    assert "started" not in lowered
    assert "new investigation" not in lowered
    # Must not claim to supersede an earlier packet.
    assert "supersed" not in lowered
    # Must not contain a link to another comment (no comment-scanning
    # mechanism exists to produce one in this slice).
    assert "#issuecomment-" not in body
