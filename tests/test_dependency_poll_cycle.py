"""
Tests for the dependency-ingestion poll cycle: cursor behavior, per-PR
processing, reconciliation sweep, and failure isolation
(docs/llds/dependency-graph-ingestion.md § Polling and Checkpoint Behavior,
§ Reconciliation, § Failure Handling, § Scope Boundary). Written before
implementation (Phase 4) — the module these tests target does not exist yet,
so every test here fails with ImportError/AttributeError until Phase 5.

Module name assumption: src/modok/ingestion/dependency_ingestion.py.

Interface assumptions (Phase 5 may adjust; the behavioral requirements
DEPG-POLL-*/DEPG-RECON-*/DEPG-ERR-*/DEPG-SCOPE-* do not depend on exact names):
  - fetch_merged_prs_since(github_repo, token, since) -> list[dict]
  - process_merged_pr_for_dependencies(client, project_slug, github_repo,
    token, pr: dict, *, manifest_globs=None) -> bool (True if any manifest
    was processed, False for a clean no-manifest-touched no-op)
  - run_dependency_ingestion_cycle(client, project_slug, github_repo, token,
    *, since, config_path) -> str (summary string for the poll log)
  - save_last_dependency_sync(config_path, project_slug, timestamp) -> None
  - reconcile_dependency_change_edges(client, project_slug) -> None
  - internal, patchable fetch helpers: _fetch_pr_files, _fetch_manifest_content
    (returns None on 404), _fetch_dependency_review (returns {} when
    unavailable, never raises to the caller)

**Cursor semantics clarified here** (the LLD's prose describes the intent;
this docstring pins the exact mechanics tests assert against): PRs in a
fetched batch are processed in ascending `updated_at` order. Every PR is
attempted regardless of an earlier PR's outcome (best-effort — a later PR's
writes are not withheld just because an earlier one failed). The cursor,
however, only ever advances through a strictly successful prefix from the
start of the batch: as soon as one PR fails, the cursor stops advancing for
the rest of the cycle, even if later PRs in the same batch succeed — this is
what guarantees the failed PR is refetched next cycle rather than silently
skipped once the cursor moves past its timestamp.

Specs verified: DEPG-POLL-001 through DEPG-POLL-006, DEPG-RECON-001,
DEPG-ERR-001 through DEPG-ERR-003, DEPG-SCOPE-001, DEPG-DIFF-001.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_client() -> MagicMock:
    client = MagicMock()
    client.upsert_node = AsyncMock()
    client.write_edge_by_parts = AsyncMock()
    client.node_exists_by_parts = AsyncMock(return_value=True)
    client.replace_edges_by_parts = AsyncMock()
    client.query = AsyncMock(return_value=[])
    return client


def _pr(number: int, updated_at: str, merge_commit_sha: str = "merge123", head_sha: str = "head456") -> dict:
    return {
        "number": number,
        "updated_at": updated_at,
        "merged_at": updated_at,
        "merge_commit_sha": merge_commit_sha,
        "head": {"sha": head_sha},
        "title": f"PR {number}",
        "user": {"login": "someone"},
    }


# ---------------------------------------------------------------------------
# DEPG-POLL-001 — own cursor, independent of last_github_sync/last_workflow_sync
# ---------------------------------------------------------------------------


# @spec DEPG-POLL-001
def test_save_last_dependency_sync_writes_its_own_field():
    from modok.ingestion.dependency_ingestion import save_last_dependency_sync

    with patch("modok.ingestion.dependency_ingestion._update_project_config_field") as mock_update:
        save_last_dependency_sync("config.toml", "stagehand", "2026-07-16T12:00:00Z")
        args = mock_update.call_args[0]
        assert args[2] == "last_dependency_sync"


# ---------------------------------------------------------------------------
# DEPG-POLL-002 — ascending-order processing
# ---------------------------------------------------------------------------


# @spec DEPG-POLL-002
@pytest.mark.asyncio
async def test_prs_processed_in_ascending_updated_at_order():
    from modok.ingestion.dependency_ingestion import run_dependency_ingestion_cycle

    client = _mock_client()
    processed_order: list[int] = []

    async def _fake_process(client, project_slug, github_repo, token, pr, **kwargs):
        processed_order.append(pr["number"])
        return False

    with patch(
        "modok.ingestion.dependency_ingestion.fetch_merged_prs_since",
        new=AsyncMock(return_value=[_pr(3, "t3"), _pr(1, "t1"), _pr(2, "t2")]),
    ), patch(
        "modok.ingestion.dependency_ingestion.process_merged_pr_for_dependencies",
        new=_fake_process,
    ), patch(
        "modok.ingestion.dependency_ingestion.save_last_dependency_sync",
    ):
        await run_dependency_ingestion_cycle(
            client, "stagehand", "owner/repo", "tok", since=None, config_path="config.toml"
        )

    assert processed_order == [1, 2, 3]


# ---------------------------------------------------------------------------
# DEPG-POLL-003/004 — per-PR cursor advance, freezing on first failure
# ---------------------------------------------------------------------------


# @spec DEPG-POLL-003, DEPG-POLL-004
@pytest.mark.asyncio
async def test_cursor_freezes_at_first_failure_but_processing_continues():
    from modok.ingestion.dependency_ingestion import run_dependency_ingestion_cycle

    client = _mock_client()
    processed = []

    async def _fake_process(client, project_slug, github_repo, token, pr, **kwargs):
        processed.append(pr["number"])
        if pr["number"] == 2:
            raise RuntimeError("simulated failure fetching PR files")
        return True

    with patch(
        "modok.ingestion.dependency_ingestion.fetch_merged_prs_since",
        new=AsyncMock(return_value=[_pr(1, "t1"), _pr(2, "t2"), _pr(3, "t3")]),
    ), patch(
        "modok.ingestion.dependency_ingestion.process_merged_pr_for_dependencies",
        new=_fake_process,
    ), patch(
        "modok.ingestion.dependency_ingestion.save_last_dependency_sync",
    ) as mock_save:
        await run_dependency_ingestion_cycle(
            client, "stagehand", "owner/repo", "tok", since=None, config_path="config.toml"
        )

    # All three PRs are attempted (best-effort) despite PR 2 failing.
    assert processed == [1, 2, 3]
    # The cursor only ever advanced past PR 1 — never past the failed PR 2,
    # even though PR 3 (newer, after the failure) succeeded.
    saved_timestamps = [c.args[2] if len(c.args) > 2 else c.kwargs.get("timestamp") for c in mock_save.call_args_list]
    assert "t1" in saved_timestamps
    assert "t2" not in saved_timestamps
    assert "t3" not in saved_timestamps


# ---------------------------------------------------------------------------
# DEPG-POLL-005 — no manifest touched is a no-op, cursor still advances
# ---------------------------------------------------------------------------


# @spec DEPG-POLL-005
@pytest.mark.asyncio
async def test_pr_touching_no_manifest_is_a_noop_and_advances_cursor():
    from modok.ingestion.dependency_ingestion import process_merged_pr_for_dependencies

    client = _mock_client()
    with patch(
        "modok.ingestion.dependency_ingestion._fetch_pr_files",
        new=AsyncMock(return_value=[{"filename": "client/stagehand_client/stagehand_ble.py", "status": "modified"}]),
    ):
        touched = await process_merged_pr_for_dependencies(
            client, "stagehand", "owner/repo", "tok", _pr(1, "t1")
        )

    assert touched is False
    client.upsert_node.assert_not_awaited()


# ---------------------------------------------------------------------------
# DEPG-POLL-006 — isolation from other poll-cycle steps
# ---------------------------------------------------------------------------


# @spec DEPG-POLL-006
@pytest.mark.asyncio
async def test_cycle_level_failure_does_not_raise_to_caller():
    """run_dependency_ingestion_cycle itself must not propagate an exception
    from fetch_merged_prs_since failing — matching the existing isolation
    between issue/PR sync and CI ingestion in _poll_once, where each step's
    try/except is independent."""
    from modok.ingestion.dependency_ingestion import run_dependency_ingestion_cycle

    client = _mock_client()
    with patch(
        "modok.ingestion.dependency_ingestion.fetch_merged_prs_since",
        new=AsyncMock(side_effect=RuntimeError("GitHub API down")),
    ):
        # Should not raise — the poll adapter's own try/except around this
        # step is a second layer, but this function's own contract is to
        # fail soft, consistent with discover_workflow_runs's precedent.
        await run_dependency_ingestion_cycle(
            client, "stagehand", "owner/repo", "tok", since=None, config_path="config.toml"
        )


# ---------------------------------------------------------------------------
# DEPG-RECON-001 — reconciliation sweep
# ---------------------------------------------------------------------------


# @spec DEPG-RECON-001
@pytest.mark.asyncio
async def test_reconciliation_writes_missing_edges_for_existing_targets():
    from modok.ingestion.dependency_ingestion import reconcile_dependency_change_edges

    client = _mock_client()
    client.query = AsyncMock(
        return_value=[
            [
                {
                    "id": 1,
                    "properties": {
                        "node_type": "DependencyChange",
                        "project_slug": "stagehand",
                        "manifest_path": "client/requirements.txt",
                        "package_purl": "pkg:pypi/bleak",
                        "commit_sha": "merge123",
                    },
                }
            ]
        ]
    )
    client.node_exists_by_parts = AsyncMock(return_value=True)

    await reconcile_dependency_change_edges(client, "stagehand")

    calls = [str(c) for c in client.write_edge_by_parts.call_args_list]
    assert any("INTRODUCED_BY" in c or "MERGED_VIA" in c for c in calls)


# @spec DEPG-RECON-001
@pytest.mark.asyncio
async def test_reconciliation_does_not_depend_on_a_cursor_argument():
    import inspect

    from modok.ingestion.dependency_ingestion import reconcile_dependency_change_edges

    sig = inspect.signature(reconcile_dependency_change_edges)
    assert "since" not in sig.parameters
    assert "cursor" not in sig.parameters


# ---------------------------------------------------------------------------
# DEPG-ERR-001 — PR files-list fetch failure
# ---------------------------------------------------------------------------


# @spec DEPG-ERR-001
@pytest.mark.asyncio
async def test_pr_files_fetch_failure_propagates_to_caller_for_cursor_freeze():
    """process_merged_pr_for_dependencies raising is exactly what lets
    run_dependency_ingestion_cycle freeze the cursor (DEPG-POLL-004) — it
    must not swallow the failure itself."""
    from modok.ingestion.dependency_ingestion import process_merged_pr_for_dependencies

    client = _mock_client()
    with patch(
        "modok.ingestion.dependency_ingestion._fetch_pr_files",
        new=AsyncMock(side_effect=RuntimeError("5xx from GitHub")),
    ):
        with pytest.raises(RuntimeError):
            await process_merged_pr_for_dependencies(
                client, "stagehand", "owner/repo", "tok", _pr(1, "t1")
            )


# ---------------------------------------------------------------------------
# DEPG-ERR-002 — Contents API 404 for a manifest at merge_commit_sha
# ---------------------------------------------------------------------------


# @spec DEPG-ERR-002, DEPG-DIFF-001
@pytest.mark.asyncio
async def test_deleted_manifest_records_removed_for_every_prior_package_no_new_snapshot():
    from modok.ingestion.dependency_ingestion import process_merged_pr_for_dependencies

    client = _mock_client()
    with patch(
        "modok.ingestion.dependency_ingestion._fetch_pr_files",
        new=AsyncMock(return_value=[{"filename": "client/requirements.txt", "status": "removed"}]),
    ), patch(
        "modok.ingestion.dependency_ingestion._fetch_manifest_content",
        new=AsyncMock(return_value=None),  # 404
    ) as mock_content, patch(
        "modok.ingestion.dependency_ingestion.find_prior_snapshot",
        # Bundles the prior snapshot's reconstructed {package: version} set
        # alongside its node properties, avoiding a second traversal helper.
        new=AsyncMock(return_value={
            "properties": {"captured_at": "2026-01-01T00:00:00Z"},
            "packages": {"bleak": ">=0.21.0"},
        }),
    ), patch(
        "modok.ingestion.dependency_ingestion.write_dependency_snapshot",
        new=AsyncMock(),
    ) as mock_write_snapshot, patch(
        "modok.ingestion.dependency_ingestion.write_dependency_change",
        new=AsyncMock(),
    ) as mock_write_change:
        await process_merged_pr_for_dependencies(
            client, "stagehand", "owner/repo", "tok", _pr(1, "t1", merge_commit_sha="merge123")
        )

    # Content fetched at merge_commit_sha, not head.sha (DEPG-DIFF-001).
    fetch_call = mock_content.call_args
    assert "merge123" in fetch_call.args or "merge123" in fetch_call.kwargs.values()

    mock_write_snapshot.assert_not_awaited()
    assert mock_write_change.await_count == 1
    change_call = mock_write_change.call_args
    assert change_call.kwargs.get("change_kind") == "removed"


# ---------------------------------------------------------------------------
# DEPG-ERR-003 — rate limiting reuses existing handling
# ---------------------------------------------------------------------------


# @spec DEPG-ERR-003
def test_dependency_ingestion_reuses_existing_retry_helper():
    import modok.ingestion.ci_ingestion as ci_mod
    import modok.ingestion.dependency_ingestion as dep_mod

    assert hasattr(dep_mod, "_with_retry_async")
    assert dep_mod._with_retry_async is ci_mod._with_retry_async, (
        "Rate-limit handling for the new Actions/Contents API calls should reuse "
        "the same Retry-After-aware helper CI ingestion already extended from "
        "GithubIngester, not a third independent implementation."
    )


# ---------------------------------------------------------------------------
# DEPG-SCOPE-001 — no Investigation/InvestigationMilestone/comment side effects
# ---------------------------------------------------------------------------


# @spec DEPG-SCOPE-001
@pytest.mark.asyncio
async def test_processing_a_dependency_change_never_writes_investigation_nodes():
    from modok.ingestion.dependency_ingestion import process_merged_pr_for_dependencies

    client = _mock_client()
    with patch(
        "modok.ingestion.dependency_ingestion._fetch_pr_files",
        new=AsyncMock(return_value=[{"filename": "client/requirements.txt", "status": "modified"}]),
    ), patch(
        "modok.ingestion.dependency_ingestion._fetch_manifest_content",
        new=AsyncMock(return_value="bleak>=0.22.0\n"),
    ), patch(
        "modok.ingestion.dependency_ingestion.find_prior_snapshot",
        new=AsyncMock(return_value=None),
    ), patch(
        "modok.ingestion.dependency_ingestion._fetch_dependency_review",
        new=AsyncMock(return_value={}),
    ):
        await process_merged_pr_for_dependencies(
            client, "stagehand", "owner/repo", "tok", _pr(1, "t1")
        )

    for call in client.upsert_node.call_args_list:
        node = call.args[0]
        assert node.node_type not in ("Investigation", "InvestigationMilestone")

    import modok.ingestion.dependency_ingestion as dep_mod

    assert not hasattr(dep_mod, "post_issue_comment"), (
        "Dependency-graph ingestion writes graph facts only — it must not "
        "import a comment-posting helper (docs/high-level-design.md non-goal)."
    )
