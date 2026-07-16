"""
Tests for the new Quine node types introduced by Continuous CI Ingestion
(docs/llds/continuous-ci-ingestion.md § New Node Types). Written before
implementation (Phase 5) — none of these types exist yet, so every test here
fails with ImportError until Phase 6.

Specs verified: CIING-NODE-001 through CIING-NODE-006.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# CIING-NODE-001 — WorkflowRun
# ---------------------------------------------------------------------------


# @spec CIING-NODE-001
def test_workflow_run_model_has_required_fields():
    from modok.quine.models import WorkflowRun

    node = WorkflowRun(
        node_type="WorkflowRun",
        project_slug="stagehand",
        run_id="12345",
        workflow_name="CI",
        head_sha="abc123",
        head_branch="main",
        event="push",
        status="completed",
        conclusion="success",
        run_number=7,
        latest_run_attempt=1,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:05:00Z",
        url="https://github.com/owner/repo/actions/runs/12345",
    )
    assert node.run_id == "12345"
    assert node.latest_run_attempt == 1


# @spec CIING-NODE-001
def test_workflow_run_model_has_expansion_state_fields():
    from modok.quine.models import WorkflowRun

    node = WorkflowRun(
        node_type="WorkflowRun",
        project_slug="stagehand",
        run_id="12345",
        workflow_name="CI",
        head_sha="abc123",
        head_branch="main",
        event="push",
        status="completed",
        conclusion="success",
        run_number=7,
        latest_run_attempt=1,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:05:00Z",
        url="https://github.com/owner/repo/actions/runs/12345",
        expansion_state="discovered",
        expansion_attempts=0,
        expansion_last_error=None,
        expansion_last_attempted_at=None,
    )
    assert node.expansion_state == "discovered"
    assert node.expansion_attempts == 0


# ---------------------------------------------------------------------------
# CIING-NODE-002/003 — WorkflowJob, WorkflowJobStep
# ---------------------------------------------------------------------------


# @spec CIING-NODE-002
def test_workflow_job_model_carries_run_attempt():
    from modok.quine.models import WorkflowJob

    node = WorkflowJob(
        node_type="WorkflowJob",
        project_slug="stagehand",
        github_job_id="999",
        run_id="12345",
        run_attempt=2,
        name="build",
        status="completed",
        conclusion="success",
        started_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-01T00:02:00Z",
        url="https://github.com/owner/repo/actions/runs/12345/job/999",
    )
    assert node.run_attempt == 2
    assert node.github_job_id == "999"


# @spec CIING-NODE-003
def test_workflow_job_step_model_scoped_to_job():
    from modok.quine.models import WorkflowJobStep

    node = WorkflowJobStep(
        node_type="WorkflowJobStep",
        project_slug="stagehand",
        run_id="12345",
        run_attempt=2,
        github_job_id="999",
        step_number=3,
        name="Run tests",
        status="completed",
        conclusion="success",
        started_at="2026-01-01T00:00:30Z",
        completed_at="2026-01-01T00:01:30Z",
    )
    assert node.step_number == 3
    assert node.github_job_id == "999"
    assert node.run_attempt == 2


# ---------------------------------------------------------------------------
# CIING-NODE-004/005 — TestExecution, TestFailure
# ---------------------------------------------------------------------------


# @spec CIING-NODE-004
def test_test_execution_model_keyed_with_run_attempt():
    from modok.quine.models import TestExecution

    node = TestExecution(
        node_type="TestExecution",
        project_slug="stagehand",
        run_id="12345",
        run_attempt=1,
        suite_name="tests.test_db",
        classname="TestDb",
        test_name="test_connect",
        status="failed",
        duration_seconds=1.5,
    )
    assert node.run_attempt == 1
    assert node.classname == "TestDb"


# @spec CIING-NODE-005
def test_test_failure_model_has_populated_fields():
    from modok.quine.models import TestFailure

    node = TestFailure(
        node_type="TestFailure",
        project_slug="stagehand",
        run_id="12345",
        run_attempt=1,
        classname="TestDb",
        test_name="test_connect",
        failure_type="AssertionError",
        message="connection timed out",
        assertion_text="assert conn is not None",
        stack_trace_excerpt="  raise DB_TIMEOUT(...)",
        observed_at="2026-01-01T00:01:00Z",
    )
    assert node.observed_at == "2026-01-01T00:01:00Z"
    assert node.run_attempt == 1


# @spec CIING-NODE-005, SQ-MILE-008
def test_test_failure_reserved_fields_default_unset():
    """latest_outcome/superseded_by/resolved_at/is_current are reserved, not
    implemented in this slice — they must exist (so a future slice can
    populate them without a schema migration) but default to an unset/None
    state, not a value implying they've been computed."""
    from modok.quine.models import TestFailure

    node = TestFailure(
        node_type="TestFailure",
        project_slug="stagehand",
        run_id="12345",
        run_attempt=1,
        classname="TestDb",
        test_name="test_connect",
        failure_type="AssertionError",
        message="connection timed out",
        assertion_text="assert conn is not None",
        stack_trace_excerpt="  raise DB_TIMEOUT(...)",
        observed_at="2026-01-01T00:01:00Z",
    )
    assert node.latest_outcome is None
    assert node.superseded_by is None
    assert node.resolved_at is None
    assert node.is_current is None


# ---------------------------------------------------------------------------
# CIING-NODE-006 — no WorkflowAttempt node type
# ---------------------------------------------------------------------------


# @spec CIING-NODE-006
def test_no_workflow_attempt_node_type_exists():
    import modok.quine.models as models

    assert not hasattr(models, "WorkflowAttempt"), (
        "Attempt identity is carried as a run_attempt property on "
        "WorkflowJob/WorkflowJobStep/TestExecution/TestFailure and "
        "latest_run_attempt on WorkflowRun — not a separate node type."
    )


# @spec CIING-NODE-001, CIING-NODE-002, CIING-NODE-003, CIING-NODE-004, CIING-NODE-005
def test_all_new_types_registered_in_node_type_map():
    from modok.quine.models import _NODE_TYPE_MAP

    for name in (
        "WorkflowRun", "WorkflowJob", "WorkflowJobStep",
        "TestExecution", "TestFailure", "InvestigationMilestone",
    ):
        assert name in _NODE_TYPE_MAP, f"{name} must be registered in _NODE_TYPE_MAP"


# @spec CIING-NODE-005 (InvestigationMilestone identity — see SQ-MILE-003 for
# the milestone-kind-specific key; this only checks the model's own fields)
def test_investigation_milestone_model_has_required_fields():
    from modok.quine.models import InvestigationMilestone

    node = InvestigationMilestone(
        node_type="InvestigationMilestone",
        project_slug="stagehand",
        milestone_kind="ci-corroborated",
        standing_query_name="ci-corroboration-pattern",
        created_at="2026-01-01T00:01:00Z",
    )
    assert node.milestone_kind == "ci-corroborated"
