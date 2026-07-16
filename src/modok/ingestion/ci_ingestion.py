"""Continuous CI ingestion — extends the existing 30-second GitHub poller to
discover and expand GitHub Actions workflow runs, jobs, steps, and JUnit test
results into the graph. See docs/llds/continuous-ci-ingestion.md."""
# @spec CIING-EDGE-001, CIING-EDGE-002, CIING-EDGE-003, CIING-EDGE-004,
#       CIING-EDGE-005, CIING-POLL-001, CIING-POLL-002, CIING-POLL-003,
#       CIING-POLL-004, CIING-POLL-005, CIING-POLL-006, CIING-POLL-007,
#       CIING-POLL-008, CIING-POLL-009

from __future__ import annotations

import asyncio
import fnmatch
import io
import sys
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from modok.ingestion.git_history import _update_project_config_field
from modok.quine.models import TestExecution, TestFailure, WorkflowJob, WorkflowJobStep, WorkflowRun

_API_BASE = "https://api.github.com"

# @spec CIING-EDGE-004 — conclusions for which a run's execution is meaningful
# enough to assert TESTED_COMMIT (as opposed to the neutral TARGETED_COMMIT,
# written regardless of conclusion — see § Targeted vs. Tested Commit).
_TESTED_COMMIT_CONCLUSIONS = {"success", "failure", "timed_out"}

_DEFAULT_ARTIFACT_PATTERN = "**/junit*.xml"

# Deliberately one page (most-recent-first) per discovery cycle, not a full
# historical backfill — the discovery cursor (last_workflow_sync) catches up
# incrementally cycle over cycle. See discover_workflow_runs' page-limit
# warning below for why hitting this cap is surfaced, not silent.
_RUNS_PAGE_SIZE = 100


def _gh_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


# ---------------------------------------------------------------------------
# GitHub API fetch helpers — thin, real implementations; patched out in tests.
# ---------------------------------------------------------------------------


async def _with_retry_async(
    http: httpx.AsyncClient, url: str, params: dict | None = None
) -> httpx.Response:
    """GET with one retry on 429, honoring Retry-After — mirrors
    GithubIngester._with_retry's semantics for the new Actions API calls
    (CIING-POLL-006's rate-limit handling)."""
    resp = await http.get(url, params=params)
    if resp.status_code == 429:
        wait = int(resp.headers.get("Retry-After", "60"))
        await asyncio.sleep(wait)
        resp = await http.get(url, params=params)
        if resp.status_code == 429:
            wait2 = int(resp.headers.get("Retry-After", wait))
            raise RuntimeError(f"GitHub API rate limit exceeded — retry after {wait2} seconds")
    return resp


async def _fetch_workflow_runs_page(
    github_repo: str, token: str, since: str | None
) -> list[dict]:
    """Fetch the most recent page of workflow runs — not a full historical
    backfill; the discovery cursor (last_workflow_sync) catches up
    incrementally. If this page is full (_RUNS_PAGE_SIZE items), older runs
    may exist that this cycle didn't check — logged so that's visible rather
    than a silent gap, not treated as an error."""
    params: dict[str, Any] = {"per_page": _RUNS_PAGE_SIZE}
    if since:
        params["created"] = f">={since}"
    async with httpx.AsyncClient(headers=_gh_headers(token), timeout=30) as http:
        resp = await _with_retry_async(
            http, f"{_API_BASE}/repos/{github_repo}/actions/runs", params
        )
        resp.raise_for_status()
        runs = resp.json().get("workflow_runs", [])
    if len(runs) >= _RUNS_PAGE_SIZE:
        print(
            f"ci-ingestion: {github_repo} — fetched {len(runs)} workflow run(s) "
            "(page limit); older/additional runs may not have been checked this cycle",
            file=sys.stderr,
        )
    return runs


async def _fetch_jobs(github_repo: str, run_id: str, token: str) -> list[dict]:
    async with httpx.AsyncClient(headers=_gh_headers(token), timeout=30) as http:
        resp = await _with_retry_async(
            http,
            f"{_API_BASE}/repos/{github_repo}/actions/runs/{run_id}/jobs",
            {"per_page": 100},
        )
        resp.raise_for_status()
        return resp.json().get("jobs", [])


async def _fetch_artifact(github_repo: str, run_id: str, token: str, artifact_pattern: str) -> bytes:
    async with httpx.AsyncClient(headers=_gh_headers(token), timeout=30) as http:
        resp = await _with_retry_async(
            http, f"{_API_BASE}/repos/{github_repo}/actions/runs/{run_id}/artifacts"
        )
        resp.raise_for_status()
        artifacts = resp.json().get("artifacts", [])
        match = next(
            (a for a in artifacts if fnmatch.fnmatch(a.get("name", ""), artifact_pattern)), None
        )
        if not match:
            return b""
        download_resp = await _with_retry_async(http, match["archive_download_url"])
        download_resp.raise_for_status()
        return download_resp.content


def _parse_junit(data: bytes) -> list[dict]:
    """Parse a JUnit XML test-results artifact (a zip of one or more JUnit
    XML files) into execution dicts. Raises on a corrupt/non-zip artifact —
    callers catch and log distinctly from an Actions API fetch failure
    (CIING-POLL-006)."""
    executions: list[dict] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            if not name.endswith(".xml"):
                continue
            root = ET.fromstring(zf.read(name))
            for tc in root.iter("testcase"):
                classname = tc.get("classname", "")
                test_name = tc.get("name", "")
                duration = tc.get("time")
                fail_el = tc.find("failure")
                if fail_el is None:
                    fail_el = tc.find("error")
                if fail_el is not None:
                    executions.append(
                        {
                            "classname": classname,
                            "test_name": test_name,
                            "status": "failed",
                            "duration_seconds": float(duration) if duration else None,
                            "failure": {
                                "failure_type": fail_el.get("type", ""),
                                "message": fail_el.get("message", ""),
                                "assertion_text": fail_el.get("message", ""),
                                "stack_trace_excerpt": (fail_el.text or "")[:2000],
                            },
                        }
                    )
                else:
                    executions.append(
                        {
                            "classname": classname,
                            "test_name": test_name,
                            "status": "passed",
                            "duration_seconds": float(duration) if duration else None,
                        }
                    )
    return executions


# ---------------------------------------------------------------------------
# CIING-EDGE-001/002 — structural edges for jobs, steps, executions, failures
# ---------------------------------------------------------------------------


async def write_workflow_job(
    client: Any, project_slug: str, *, run_id: str, run_attempt: int, job: dict
) -> None:
    github_job_id = str(job["id"])
    node = WorkflowJob(
        node_type="WorkflowJob",
        project_slug=project_slug,
        github_job_id=github_job_id,
        run_id=run_id,
        run_attempt=run_attempt,
        name=job.get("name", ""),
        status=job.get("status", ""),
        conclusion=job.get("conclusion"),
        started_at=job.get("started_at"),
        completed_at=job.get("completed_at"),
        url=job.get("html_url", ""),
    )
    await client.upsert_node(node)
    await client.write_edge_by_parts(
        ("workflow-run", project_slug, run_id),
        "HAS_JOB",
        ("workflow-job", project_slug, run_id, run_attempt, github_job_id),
    )


async def write_workflow_job_step(
    client: Any,
    project_slug: str,
    *,
    run_id: str,
    run_attempt: int,
    github_job_id: str,
    step: dict,
) -> None:
    step_number = step["number"]
    node = WorkflowJobStep(
        node_type="WorkflowJobStep",
        project_slug=project_slug,
        run_id=run_id,
        run_attempt=run_attempt,
        github_job_id=github_job_id,
        step_number=step_number,
        name=step.get("name", ""),
        status=step.get("status", ""),
        conclusion=step.get("conclusion"),
        started_at=step.get("started_at"),
        completed_at=step.get("completed_at"),
    )
    await client.upsert_node(node)
    await client.write_edge_by_parts(
        ("workflow-job", project_slug, run_id, run_attempt, github_job_id),
        "HAS_STEP",
        ("workflow-job-step", project_slug, run_id, run_attempt, github_job_id, step_number),
    )


async def write_test_execution(
    client: Any, project_slug: str, *, run_id: str, run_attempt: int, execution: dict
) -> None:
    classname = execution.get("classname", "")
    test_name = execution.get("test_name", "")
    node = TestExecution(
        node_type="TestExecution",
        project_slug=project_slug,
        run_id=run_id,
        run_attempt=run_attempt,
        suite_name=execution.get("suite_name", ""),
        classname=classname,
        test_name=test_name,
        status=execution.get("status", ""),
        duration_seconds=execution.get("duration_seconds"),
    )
    await client.upsert_node(node)
    await client.write_edge_by_parts(
        ("test-execution", project_slug, run_id, run_attempt, classname, test_name),
        "RAN_IN",
        ("workflow-run", project_slug, run_id),
    )


async def write_test_failure(
    client: Any,
    project_slug: str,
    *,
    run_id: str,
    run_attempt: int,
    classname: str,
    test_name: str,
    failure: dict,
    matched_error_slug: str | None,
) -> None:
    node = TestFailure(
        node_type="TestFailure",
        project_slug=project_slug,
        run_id=run_id,
        run_attempt=run_attempt,
        classname=classname,
        test_name=test_name,
        failure_type=failure.get("failure_type", ""),
        message=failure.get("message", ""),
        assertion_text=failure.get("assertion_text"),
        stack_trace_excerpt=failure.get("stack_trace_excerpt"),
        observed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    await client.upsert_node(node)
    tf_parts = ("test-failure", project_slug, run_id, run_attempt, classname, test_name)
    await client.write_edge_by_parts(
        tf_parts,
        "OCCURRED_IN",
        ("test-execution", project_slug, run_id, run_attempt, classname, test_name),
    )
    # @spec CIING-EDGE-002 — HAS_ERROR only on an actual ErrorSignatureMatcher hit;
    # matched_error_slug is the matched ErrorSignature's normalized_error value
    # (the field name mirrors the shared matcher's own naming, not a registry slug).
    if matched_error_slug:
        await client.write_edge_by_parts(
            tf_parts, "HAS_ERROR", ("error", project_slug, matched_error_slug)
        )


# ---------------------------------------------------------------------------
# CIING-EDGE-003/004/005 — Targeted vs. Tested Commit, reconciliation sweep
# ---------------------------------------------------------------------------


async def write_commit_edges(client: Any, project_slug: str, run: dict) -> None:
    head_sha = run.get("head_sha")
    if not head_sha:
        return
    commit_parts = ("commit", project_slug, head_sha)
    if not await client.node_exists_by_parts(commit_parts):
        return
    run_parts = ("workflow-run", project_slug, str(run["run_id"]))
    # @spec CIING-EDGE-003 — neutral association, written regardless of conclusion
    await client.write_edge_by_parts(run_parts, "TARGETED_COMMIT", commit_parts)
    # @spec CIING-EDGE-004 — asserts meaningful execution, only for qualifying conclusions
    if run.get("conclusion") in _TESTED_COMMIT_CONCLUSIONS:
        await client.write_edge_by_parts(run_parts, "TESTED_COMMIT", commit_parts)


# @spec CIING-EDGE-005
async def reconcile_commit_edges(client: Any, project_slug: str) -> None:
    """Independent, unconditional per-cycle sweep — adds TARGETED_COMMIT/
    TESTED_COMMIT when a commit now exists or a run's conclusion changed to
    qualify, and removes a stale TESTED_COMMIT when a conclusion no longer
    qualifies (e.g. a manual re-run reset it). Takes no cursor or
    expansion_state argument (CIING-EDGE-005) — it re-derives everything from
    current WorkflowRun state each time."""
    rows = await client.query(
        "MATCH (n) WHERE n.node_type = 'WorkflowRun' AND n.project_slug = $p RETURN n",
        {"p": project_slug},
    )
    for row in rows:
        if not row:
            continue
        props = row[0].get("properties", {})
        run_id = props.get("run_id")
        head_sha = props.get("head_sha")
        if not run_id or not head_sha:
            continue
        commit_parts = ("commit", project_slug, head_sha)
        if not await client.node_exists_by_parts(commit_parts):
            continue
        run_parts = ("workflow-run", project_slug, run_id)
        await client.write_edge_by_parts(run_parts, "TARGETED_COMMIT", commit_parts)
        if props.get("conclusion") in _TESTED_COMMIT_CONCLUSIONS:
            await client.write_edge_by_parts(run_parts, "TESTED_COMMIT", commit_parts)
        else:
            await client.replace_edges_by_parts(run_parts, "TESTED_COMMIT", [])


# ---------------------------------------------------------------------------
# CIING-POLL-001..003 — discovery cursor, expansion backlog
# ---------------------------------------------------------------------------


async def _get_workflow_run_properties(client: Any, project_slug: str, run_id: str) -> dict:
    rows = await client.query(
        "MATCH (n) WHERE id(n) = idFrom('workflow-run', $p, $r) RETURN n",
        {"p": project_slug, "r": run_id},
    )
    if rows and rows[0]:
        return rows[0][0].get("properties", {}) or {}
    return {}


def _workflow_run_from_props(project_slug: str, run_id: str, props: dict, **overrides: Any) -> WorkflowRun:
    data: dict[str, Any] = {
        "node_type": "WorkflowRun",
        "project_slug": project_slug,
        "run_id": run_id,
        "workflow_name": props.get("workflow_name", ""),
        "head_sha": props.get("head_sha", ""),
        "head_branch": props.get("head_branch", ""),
        "event": props.get("event", ""),
        "status": props.get("status", ""),
        "conclusion": props.get("conclusion"),
        "run_number": props.get("run_number", 0),
        "latest_run_attempt": props.get("latest_run_attempt", 1),
        "created_at": props.get("created_at", ""),
        "updated_at": props.get("updated_at", ""),
        "url": props.get("url", ""),
        "expansion_state": props.get("expansion_state", "discovered"),
        "expansion_attempts": props.get("expansion_attempts", 0),
        "expansion_last_error": props.get("expansion_last_error"),
        "expansion_last_attempted_at": props.get("expansion_last_attempted_at"),
    }
    data.update(overrides)
    return WorkflowRun(**data)


# @spec CIING-POLL-001 — discovery cursor advances independent of expansion outcome
async def discover_workflow_runs(
    client: Any, project_slug: str, github_repo: str, token: str, since: str | None
) -> list[dict]:
    runs = await _fetch_workflow_runs_page(github_repo, token, since)
    for run in runs:
        run_id = str(run["id"])
        existing = await _get_workflow_run_properties(client, project_slug, run_id)
        node = _workflow_run_from_props(
            project_slug,
            run_id,
            existing,
            workflow_name=run.get("name", existing.get("workflow_name", "")),
            head_sha=run.get("head_sha", existing.get("head_sha", "")),
            head_branch=run.get("head_branch", existing.get("head_branch", "")),
            event=run.get("event", existing.get("event", "")),
            status=run.get("status", existing.get("status", "")),
            conclusion=run.get("conclusion", existing.get("conclusion")),
            run_number=run.get("run_number", existing.get("run_number", 0)),
            latest_run_attempt=run.get("run_attempt", existing.get("latest_run_attempt", 1)),
            created_at=run.get("created_at", existing.get("created_at", "")),
            updated_at=run.get("updated_at", existing.get("updated_at", "")),
            url=run.get("html_url", existing.get("url", "")),
            # @spec CIING-POLL-001 — never reset an already-in-progress or
            # already-complete expansion_state back to "discovered"
            expansion_state=existing.get("expansion_state", "discovered"),
        )
        await client.upsert_node(node)
    return runs


# @spec CIING-POLL-002, CIING-POLL-003
async def find_expansion_backlog(client: Any, project_slug: str) -> list[str]:
    rows = await client.query(
        "MATCH (n) WHERE n.node_type = 'WorkflowRun' AND n.project_slug = $p "
        "AND NOT n.expansion_state IN ['complete', 'terminal_failure'] RETURN n",
        {"p": project_slug},
    )
    return [row[0].get("properties", {}).get("run_id") for row in rows if row]


def save_last_workflow_sync(config_path: Path, project_slug: str, timestamp: str) -> None:
    """Write last_workflow_sync for a project to the TOML config file
    in-place — the discovery high-water mark, independent of
    last_github_sync (see § Poll Cycle Extension)."""
    _update_project_config_field(config_path, project_slug, "last_workflow_sync", timestamp)


# ---------------------------------------------------------------------------
# CIING-POLL-004..009 — per-run expansion, failure isolation
# ---------------------------------------------------------------------------


# @spec CIING-POLL-004, CIING-POLL-005, CIING-POLL-006, CIING-POLL-007, CIING-POLL-008, CIING-POLL-009
async def expand_workflow_run(
    client: Any,
    project_slug: str,
    *,
    run_id: str,
    token: str,
    github_repo: str = "",
    artifact_pattern: str | None = _DEFAULT_ARTIFACT_PATTERN,
) -> None:
    props = await _get_workflow_run_properties(client, project_slug, run_id)
    run_attempt = int(props.get("latest_run_attempt", 1))
    attempts = int(props.get("expansion_attempts", 0)) + 1
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # @spec CIING-POLL-008 — expansion_state set to pending before any fetch is attempted
    await client.upsert_node(
        _workflow_run_from_props(
            project_slug,
            run_id,
            props,
            expansion_state="expansion_pending",
            expansion_attempts=attempts,
            expansion_last_attempted_at=now,
        )
    )

    # @spec CIING-POLL-005, CIING-POLL-006 — per-run failure isolation, distinguishable logging
    try:
        jobs = await _fetch_jobs(github_repo, run_id, token)
    except Exception as exc:
        print(
            f"ci-ingestion: Actions API error fetching jobs for run {run_id}: {exc}",
            file=sys.stderr,
        )
        await client.upsert_node(
            _workflow_run_from_props(
                project_slug,
                run_id,
                props,
                expansion_state="retryable_failure",
                expansion_attempts=attempts,
                expansion_last_attempted_at=now,
                expansion_last_error=str(exc),
            )
        )
        return

    # @spec CIING-POLL-004 — incremental writes: jobs already fetched are
    # written even if the artifact step below fails.
    for job in jobs:
        await write_workflow_job(client, project_slug, run_id=run_id, run_attempt=run_attempt, job=job)

    partially_ingested = False
    # @spec CIING-POLL-009 — no artifact pattern configured reaches "complete" without fetching
    if artifact_pattern:
        try:
            artifact_bytes = await _fetch_artifact(github_repo, run_id, token, artifact_pattern)
            executions = _parse_junit(artifact_bytes)
            for execution in executions:
                await write_test_execution(
                    client, project_slug, run_id=run_id, run_attempt=run_attempt, execution=execution
                )
                if execution.get("status") == "failed" and execution.get("failure"):
                    await write_test_failure(
                        client,
                        project_slug,
                        run_id=run_id,
                        run_attempt=run_attempt,
                        classname=execution.get("classname", ""),
                        test_name=execution.get("test_name", ""),
                        failure=execution["failure"],
                        matched_error_slug=execution.get("matched_error_slug"),
                    )
        except Exception as exc:
            print(f"ci-ingestion: corrupt artifact for run {run_id}: {exc}", file=sys.stderr)
            partially_ingested = True

    final_state = "partially_ingested" if partially_ingested else "complete"
    await client.upsert_node(
        _workflow_run_from_props(
            project_slug,
            run_id,
            props,
            expansion_state=final_state,
            expansion_attempts=attempts,
            expansion_last_attempted_at=now,
        )
    )
