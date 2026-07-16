"""
Tests for the Continuous CI Ingestion poll-cycle extension
(docs/llds/continuous-ci-ingestion.md § Poll Cycle Extension). Written before
implementation (Phase 5) — the module these tests target does not exist yet,
so every test here fails with ImportError/AttributeError until Phase 6.

Interface assumptions (Phase 6 may adjust; the behavioral requirements
CIING-POLL-* do not depend on exact names):
  - modok.ingestion.ci_ingestion.discover_workflow_runs(client, project_slug,
    github_repo, token, since) -> list[dict]: fetches + upserts WorkflowRun
    nodes with expansion_state="discovered" if new; does not expand them.
  - modok.ingestion.ci_ingestion.find_expansion_backlog(client, project_slug)
    -> list[str]: run_ids not yet complete/terminal_failure.
  - modok.ingestion.ci_ingestion.expand_workflow_run(client, project_slug,
    run_id, token) -> None: does jobs/steps/artifact/test expansion for one
    run, updating its expansion_state; sub-steps are injectable via patched
    module-level helpers (_fetch_jobs, _fetch_artifact, _parse_junit) so
    individual failure causes can be simulated without a real HTTP mock.

Specs verified: CIING-POLL-001 through CIING-POLL-009.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _default_fetch_artifact(monkeypatch):
    """Prevent tests that don't care about artifact-fetch behavior (only
    _fetch_jobs is patched) from making a real network call to GitHub.
    test_actions_api_error_and_corrupt_artifact_log_differently and
    test_no_artifact_pattern_configured_reaches_complete_without_fetching
    patch _fetch_artifact themselves within their own `with` block, which
    takes precedence for its duration — same pattern as
    test_ingestion_github.py's _isolated_config fixture."""
    import modok.ingestion.ci_ingestion as ci_ingestion

    monkeypatch.setattr(ci_ingestion, "_fetch_artifact", AsyncMock(return_value=b""))


def _mock_client() -> MagicMock:
    client = MagicMock()
    client.upsert_node = AsyncMock()
    client.write_edge_by_parts = AsyncMock()
    client.node_exists_by_parts = AsyncMock(return_value=True)
    client.query = AsyncMock(return_value=[])
    return client


# ---------------------------------------------------------------------------
# CIING-POLL-001 — discovery cursor advances independent of expansion outcome
# ---------------------------------------------------------------------------


# @spec CIING-POLL-001
@pytest.mark.asyncio
async def test_discovery_upserts_workflow_run_with_discovered_state():
    from modok.ingestion.ci_ingestion import discover_workflow_runs

    client = _mock_client()
    with patch("modok.ingestion.ci_ingestion._fetch_workflow_runs_page", new=AsyncMock(
        return_value=[{"id": "100", "head_sha": "abc", "status": "queued", "updated_at": "t1"}]
    )):
        await discover_workflow_runs(client, "stagehand", "owner/repo", "tok", since=None)

    client.upsert_node.assert_awaited()
    node = client.upsert_node.call_args[0][0]
    assert node.expansion_state == "discovered"


# @spec CIING-POLL-001
@pytest.mark.asyncio
async def test_discovery_does_not_touch_expansion_state_of_already_known_run():
    """Re-discovering a run whose metadata changed (e.g. status updated) must
    not reset an already-in-progress or already-complete expansion_state back
    to 'discovered'."""
    from modok.ingestion.ci_ingestion import discover_workflow_runs

    client = _mock_client()
    client.query = AsyncMock(return_value=[[{"properties": {"expansion_state": "complete"}}]])
    with patch("modok.ingestion.ci_ingestion._fetch_workflow_runs_page", new=AsyncMock(
        return_value=[{"id": "100", "head_sha": "abc", "status": "completed", "updated_at": "t2"}]
    )):
        await discover_workflow_runs(client, "stagehand", "owner/repo", "tok", since=None)

    node = client.upsert_node.call_args[0][0]
    assert node.expansion_state == "complete"


# ---------------------------------------------------------------------------
# CIING-POLL-002/003 — expansion_state property + independent backlog query
# ---------------------------------------------------------------------------


# @spec CIING-POLL-002, CIING-POLL-003
@pytest.mark.asyncio
async def test_expansion_backlog_excludes_complete_and_terminal_failure():
    from modok.ingestion.ci_ingestion import find_expansion_backlog

    client = _mock_client()
    client.query = AsyncMock(return_value=[[{"properties": {"run_id": "101"}}]])
    backlog = await find_expansion_backlog(client, "stagehand")

    query_text = client.query.call_args[0][0]
    assert "complete" in query_text
    assert "terminal_failure" in query_text
    assert backlog == ["101"]


# @spec CIING-POLL-003
@pytest.mark.asyncio
async def test_expansion_backlog_is_independent_of_current_cycle_discovery():
    """The backlog query takes only (client, project_slug) — it must not
    require or depend on the current cycle's freshly-discovered run list."""
    import inspect

    from modok.ingestion.ci_ingestion import find_expansion_backlog

    sig = inspect.signature(find_expansion_backlog)
    assert set(sig.parameters.keys()) <= {"client", "project_slug"}


# ---------------------------------------------------------------------------
# CIING-POLL-004 — incremental writes, not buffer-and-discard
# ---------------------------------------------------------------------------


# @spec CIING-POLL-004
@pytest.mark.asyncio
async def test_successfully_fetched_jobs_written_even_if_artifact_step_fails():
    from modok.ingestion.ci_ingestion import expand_workflow_run

    client = _mock_client()
    with patch(
        "modok.ingestion.ci_ingestion._fetch_jobs",
        new=AsyncMock(return_value=[{"id": "1", "name": "build", "status": "completed"}]),
    ), patch(
        "modok.ingestion.ci_ingestion._fetch_artifact",
        new=AsyncMock(side_effect=Exception("artifact API error")),
    ):
        await expand_workflow_run(client, "stagehand", run_id="100", token="tok")

    # The job upsert must have happened despite the later artifact failure.
    upserted_types = [type(c[0][0]).__name__ for c in client.upsert_node.call_args_list]
    assert "WorkflowJob" in upserted_types


# ---------------------------------------------------------------------------
# CIING-POLL-005 — per-run failure isolation
# ---------------------------------------------------------------------------


# @spec CIING-POLL-005
@pytest.mark.asyncio
async def test_one_run_failure_does_not_raise_or_block_caller():
    from modok.ingestion.ci_ingestion import expand_workflow_run

    client = _mock_client()
    with patch(
        "modok.ingestion.ci_ingestion._fetch_jobs",
        new=AsyncMock(side_effect=Exception("Actions API unreachable")),
    ):
        # Must not raise — failure is caught and reflected in expansion_state,
        # not propagated to the caller (which processes other runs next).
        await expand_workflow_run(client, "stagehand", run_id="100", token="tok")

    node = client.upsert_node.call_args[0][0]
    assert node.expansion_state == "retryable_failure"


# ---------------------------------------------------------------------------
# CIING-POLL-006 — distinguishable failure logging
# ---------------------------------------------------------------------------


# @spec CIING-POLL-006
@pytest.mark.asyncio
async def test_actions_api_error_and_corrupt_artifact_log_differently(capsys):
    from modok.ingestion.ci_ingestion import expand_workflow_run

    client = _mock_client()
    with patch(
        "modok.ingestion.ci_ingestion._fetch_jobs",
        new=AsyncMock(side_effect=Exception("Actions API unreachable")),
    ):
        await expand_workflow_run(client, "stagehand", run_id="100", token="tok")
    api_error_log = capsys.readouterr().err

    client2 = _mock_client()
    with patch(
        "modok.ingestion.ci_ingestion._fetch_jobs",
        new=AsyncMock(return_value=[]),
    ), patch(
        "modok.ingestion.ci_ingestion._fetch_artifact",
        new=AsyncMock(return_value=b"not a valid zip"),
    ):
        await expand_workflow_run(client2, "stagehand", run_id="101", token="tok")
    corrupt_artifact_log = capsys.readouterr().err

    assert api_error_log != corrupt_artifact_log
    assert "api" in api_error_log.lower() or "unreachable" in api_error_log.lower()
    assert "artifact" in corrupt_artifact_log.lower() or "corrupt" in corrupt_artifact_log.lower()


# ---------------------------------------------------------------------------
# CIING-POLL-007 — idempotent retry
# ---------------------------------------------------------------------------


# @spec CIING-POLL-007
@pytest.mark.asyncio
async def test_retrying_a_run_does_not_duplicate_already_written_jobs():
    from modok.ingestion.ci_ingestion import expand_workflow_run

    client = _mock_client()
    with patch(
        "modok.ingestion.ci_ingestion._fetch_jobs",
        new=AsyncMock(return_value=[{"id": "1", "name": "build", "status": "completed"}]),
    ):
        await expand_workflow_run(client, "stagehand", run_id="100", token="tok")
        await expand_workflow_run(client, "stagehand", run_id="100", token="tok")

    # upsert_node is idempotent by deterministic key — calling twice is safe,
    # not asserted-not-called-twice, but must not error and must leave the
    # same WorkflowJob identity both times.
    job_upserts = [c[0][0] for c in client.upsert_node.call_args_list if type(c[0][0]).__name__ == "WorkflowJob"]
    assert len(job_upserts) == 2
    assert job_upserts[0].github_job_id == job_upserts[1].github_job_id


# ---------------------------------------------------------------------------
# CIING-POLL-008 — expansion_state transition timing
# ---------------------------------------------------------------------------


# @spec CIING-POLL-008
@pytest.mark.asyncio
async def test_expansion_state_set_to_pending_before_fetch_attempted():
    from modok.ingestion.ci_ingestion import expand_workflow_run

    client = _mock_client()
    states_seen = []

    async def _capture_upsert(node):
        if type(node).__name__ == "WorkflowRun":
            states_seen.append(node.expansion_state)

    client.upsert_node = AsyncMock(side_effect=_capture_upsert)
    with patch(
        "modok.ingestion.ci_ingestion._fetch_jobs",
        new=AsyncMock(return_value=[]),
    ):
        await expand_workflow_run(client, "stagehand", run_id="100", token="tok")

    assert "expansion_pending" in states_seen
    assert states_seen.index("expansion_pending") < states_seen.index(states_seen[-1]) or len(states_seen) == 1


# @spec CIING-POLL-008
@pytest.mark.asyncio
async def test_expansion_attempts_increments_each_attempt():
    from modok.ingestion.ci_ingestion import expand_workflow_run

    client = _mock_client()
    with patch(
        "modok.ingestion.ci_ingestion._fetch_jobs",
        new=AsyncMock(return_value=[]),
    ):
        await expand_workflow_run(client, "stagehand", run_id="100", token="tok")

    run_node_upserts = [
        c[0][0] for c in client.upsert_node.call_args_list if type(c[0][0]).__name__ == "WorkflowRun"
    ]
    assert any(n.expansion_attempts >= 1 for n in run_node_upserts)


# ---------------------------------------------------------------------------
# CIING-POLL-009 — no artifact pattern configured reaches "complete"
# ---------------------------------------------------------------------------


# @spec CIING-POLL-009
@pytest.mark.asyncio
async def test_no_artifact_pattern_configured_reaches_complete_without_fetching():
    from modok.ingestion.ci_ingestion import expand_workflow_run

    client = _mock_client()
    with patch(
        "modok.ingestion.ci_ingestion._fetch_jobs", new=AsyncMock(return_value=[])
    ), patch(
        "modok.ingestion.ci_ingestion._fetch_artifact"
    ) as mock_fetch_artifact:
        await expand_workflow_run(
            client, "stagehand", run_id="100", token="tok", artifact_pattern=None
        )

    mock_fetch_artifact.assert_not_called()
    run_node_upserts = [
        c[0][0] for c in client.upsert_node.call_args_list if type(c[0][0]).__name__ == "WorkflowRun"
    ]
    assert any(n.expansion_state == "complete" for n in run_node_upserts)
