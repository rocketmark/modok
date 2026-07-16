"""
Tests for the new CI-ingestion edges (docs/llds/continuous-ci-ingestion.md
§ New Edges, § Targeted vs. Tested Commit). Written before implementation
(Phase 5) — the module these tests target (modok.ingestion.ci_ingestion) does
not exist yet, so every test here fails with ImportError until Phase 6.

Module name assumption: src/modok/ingestion/ci_ingestion.py, mirroring the
existing src/modok/ingestion/github.py and git_history.py sibling modules.
Phase 6 may rename; the behavioral requirements below (CIING-EDGE-*) do not
depend on the exact module path.

Specs verified: CIING-EDGE-001 through CIING-EDGE-005.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _mock_client(commit_exists: bool = True) -> MagicMock:
    client = MagicMock()
    client.upsert_node = AsyncMock()
    client.write_edge_by_parts = AsyncMock()
    client.node_exists_by_parts = AsyncMock(return_value=commit_exists)
    client.replace_edges_by_parts = AsyncMock()
    return client


def _make_run(run_id: str = "100", head_sha: str = "abc123", conclusion: str = "success") -> dict:
    return {
        "run_id": run_id,
        "head_sha": head_sha,
        "conclusion": conclusion,
        "status": "completed",
    }


# ---------------------------------------------------------------------------
# CIING-EDGE-001 — structural edges
# ---------------------------------------------------------------------------


# @spec CIING-EDGE-001
@pytest.mark.asyncio
async def test_writes_has_job_and_has_step_edges():
    from modok.ingestion.ci_ingestion import write_workflow_job, write_workflow_job_step

    client = _mock_client()
    await write_workflow_job(
        client, "stagehand", run_id="100", run_attempt=1,
        job={"id": "999", "name": "build", "status": "completed", "conclusion": "success"},
    )
    calls = [str(c) for c in client.write_edge_by_parts.call_args_list]
    assert any("HAS_JOB" in c for c in calls)

    await write_workflow_job_step(
        client, "stagehand", run_id="100", run_attempt=1, github_job_id="999",
        step={"number": 1, "name": "checkout", "status": "completed", "conclusion": "success"},
    )
    calls = [str(c) for c in client.write_edge_by_parts.call_args_list]
    assert any("HAS_STEP" in c for c in calls)


# @spec CIING-EDGE-001
@pytest.mark.asyncio
async def test_writes_ran_in_and_occurred_in_edges():
    from modok.ingestion.ci_ingestion import write_test_execution, write_test_failure

    client = _mock_client()
    await write_test_execution(
        client, "stagehand", run_id="100", run_attempt=1,
        execution={"classname": "TestDb", "test_name": "test_connect", "status": "failed"},
    )
    calls = [str(c) for c in client.write_edge_by_parts.call_args_list]
    assert any("RAN_IN" in c for c in calls)

    await write_test_failure(
        client, "stagehand", run_id="100", run_attempt=1,
        classname="TestDb", test_name="test_connect",
        failure={"failure_type": "AssertionError", "message": "boom"},
        matched_error_slug=None,
    )
    calls = [str(c) for c in client.write_edge_by_parts.call_args_list]
    assert any("OCCURRED_IN" in c for c in calls)


# @spec CIING-EDGE-002
@pytest.mark.asyncio
async def test_writes_has_error_only_on_matcher_hit():
    from modok.ingestion.ci_ingestion import write_test_failure

    client = _mock_client(commit_exists=True)
    await write_test_failure(
        client, "stagehand", run_id="100", run_attempt=1,
        classname="TestDb", test_name="test_connect",
        failure={"failure_type": "AssertionError", "message": "boom"},
        matched_error_slug="db-timeout",
    )
    calls = [str(c) for c in client.write_edge_by_parts.call_args_list]
    assert any("HAS_ERROR" in c for c in calls)


# @spec CIING-EDGE-002
@pytest.mark.asyncio
async def test_no_has_error_edge_when_matcher_finds_nothing():
    from modok.ingestion.ci_ingestion import write_test_failure

    client = _mock_client()
    await write_test_failure(
        client, "stagehand", run_id="100", run_attempt=1,
        classname="TestDb", test_name="test_connect",
        failure={"failure_type": "AssertionError", "message": "boom"},
        matched_error_slug=None,
    )
    calls = [str(c) for c in client.write_edge_by_parts.call_args_list]
    assert not any("HAS_ERROR" in c for c in calls)


# ---------------------------------------------------------------------------
# CIING-EDGE-003/004 — TARGETED_COMMIT vs TESTED_COMMIT
# ---------------------------------------------------------------------------


# @spec CIING-EDGE-003
@pytest.mark.asyncio
async def test_targeted_commit_written_regardless_of_conclusion():
    from modok.ingestion.ci_ingestion import write_commit_edges

    client = _mock_client(commit_exists=True)
    run = _make_run(conclusion="cancelled")
    await write_commit_edges(client, "stagehand", run)

    calls = [str(c) for c in client.write_edge_by_parts.call_args_list]
    assert any("TARGETED_COMMIT" in c for c in calls)


# @spec CIING-EDGE-003
@pytest.mark.asyncio
async def test_targeted_commit_skipped_when_commit_absent():
    from modok.ingestion.ci_ingestion import write_commit_edges

    client = _mock_client(commit_exists=False)
    run = _make_run(conclusion="success")
    await write_commit_edges(client, "stagehand", run)

    calls = [str(c) for c in client.write_edge_by_parts.call_args_list]
    assert not any("TARGETED_COMMIT" in c for c in calls)
    assert not any("TESTED_COMMIT" in c for c in calls)


# @spec CIING-EDGE-004
@pytest.mark.asyncio
@pytest.mark.parametrize("conclusion", ["success", "failure", "timed_out"])
async def test_tested_commit_written_for_qualifying_conclusions(conclusion):
    from modok.ingestion.ci_ingestion import write_commit_edges

    client = _mock_client(commit_exists=True)
    run = _make_run(conclusion=conclusion)
    await write_commit_edges(client, "stagehand", run)

    calls = [str(c) for c in client.write_edge_by_parts.call_args_list]
    assert any("TESTED_COMMIT" in c for c in calls)


# @spec CIING-EDGE-004
@pytest.mark.asyncio
@pytest.mark.parametrize("conclusion", ["cancelled", "startup_failure", "action_required"])
async def test_tested_commit_not_written_for_non_qualifying_conclusions(conclusion):
    from modok.ingestion.ci_ingestion import write_commit_edges

    client = _mock_client(commit_exists=True)
    run = _make_run(conclusion=conclusion)
    await write_commit_edges(client, "stagehand", run)

    # TARGETED_COMMIT still fires (neutral association); TESTED_COMMIT must not.
    calls = [str(c) for c in client.write_edge_by_parts.call_args_list]
    assert any("TARGETED_COMMIT" in c for c in calls)
    assert not any("TESTED_COMMIT" in c for c in calls)


# ---------------------------------------------------------------------------
# CIING-EDGE-005 — reconciliation sweep
# ---------------------------------------------------------------------------


# @spec CIING-EDGE-005
@pytest.mark.asyncio
async def test_reconciliation_adds_missing_commit_edges():
    from modok.ingestion.ci_ingestion import reconcile_commit_edges

    client = _mock_client(commit_exists=True)
    # Simulate one WorkflowRun whose commit now exists but has no edges yet.
    client.query = AsyncMock(
        return_value=[[{"properties": {
            "run_id": "100", "head_sha": "abc123", "conclusion": "success",
        }}]]
    )
    await reconcile_commit_edges(client, "stagehand")

    calls = [str(c) for c in client.write_edge_by_parts.call_args_list]
    assert any("TARGETED_COMMIT" in c for c in calls)
    assert any("TESTED_COMMIT" in c for c in calls)


# @spec CIING-EDGE-005
@pytest.mark.asyncio
async def test_reconciliation_removes_tested_commit_when_conclusion_no_longer_qualifies():
    """A run whose conclusion has changed (e.g. reset by a manual re-run) to a
    non-qualifying value must have its stale TESTED_COMMIT edge removed via
    replace_edges_by_parts, not left in place."""
    from modok.ingestion.ci_ingestion import reconcile_commit_edges

    client = _mock_client(commit_exists=True)
    client.query = AsyncMock(
        return_value=[[{"properties": {
            "run_id": "100", "head_sha": "abc123", "conclusion": "cancelled",
        }}]]
    )
    await reconcile_commit_edges(client, "stagehand")

    client.replace_edges_by_parts.assert_awaited()
    calls = [str(c) for c in client.replace_edges_by_parts.call_args_list]
    assert any("TESTED_COMMIT" in c for c in calls)


# @spec CIING-EDGE-005
@pytest.mark.asyncio
async def test_reconciliation_does_not_depend_on_cursor_or_expansion_state():
    """The reconciliation sweep is an independent, unconditional per-cycle
    step — it must run (and be callable) without any cursor or expansion_state
    argument, unlike the discovery/expansion machinery in CIING-POLL-*."""
    import inspect

    from modok.ingestion.ci_ingestion import reconcile_commit_edges

    sig = inspect.signature(reconcile_commit_edges)
    param_names = set(sig.parameters.keys())
    assert "cursor" not in param_names
    assert "expansion_state" not in param_names
