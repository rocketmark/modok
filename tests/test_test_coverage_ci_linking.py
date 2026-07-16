"""
Tests for Test-Coverage CI Linking: classname -> TestFile resolution, the
new EXECUTES edge, inline + reconciliation-sweep resolution timing, and
failure/scope handling (docs/llds/test-coverage-ci-linking.md). Written
before implementation (Phase 4) — the functions these tests target do not
exist yet, so every test here fails with ImportError/AttributeError until
Phase 5.

Module: src/modok/ingestion/ci_ingestion.py (additive extension — existing
functions write_test_execution/write_test_failure/expand_workflow_run are
not modified in behavior, only expand_workflow_run gains one new call site).

Interface assumptions (Phase 5 may adjust; the behavioral requirements
TCLINK-* do not depend on exact names):
  - _candidate_paths_for_classname(classname: str) -> list[str]
  - resolve_test_execution_link(client, project_slug, classname: str)
      -> tuple[str, str | None]   # (link_state, path) where link_state is
      "resolved" | "ambiguous" | "unresolved"
  - link_test_execution_to_file(client, project_slug, *, run_id, run_attempt,
      execution: dict) -> None   # inline call site, called from
      expand_workflow_run right after write_test_execution
  - reconcile_test_execution_links(client, project_slug) -> None   # sweep,
      querying TestExecution nodes where link_state IS NULL OR link_state =
      'ambiguous' (inclusion-style — 'resolved' is excluded simply by never
      being named as an allowed value; a no-match result is never persisted
      as anything other than unset, so it is retried indefinitely simply by
      staying in the unset bucket — no bounded-attempts exclusion in v1,
      see docs/llds/test-coverage-ci-linking.md § Where Resolution Runs,
      Cost caveat, for why that was drafted and rejected)
  - TestExecution model gains a `link_state: str | None = None` field

Specs verified: TCLINK-RESOLVE-001 through TCLINK-RESOLVE-006,
TCLINK-EDGE-001 through TCLINK-EDGE-003, TCLINK-POLL-001 through
TCLINK-POLL-005, TCLINK-ERR-001, TCLINK-ERR-002, TCLINK-SCOPE-001,
TCLINK-SCOPE-003.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_client(query_return=None) -> MagicMock:
    client = MagicMock()
    client.upsert_node = AsyncMock()
    client.write_edge_by_parts = AsyncMock()
    client.node_exists_by_parts = AsyncMock(return_value=True)
    client.query = AsyncMock(return_value=query_return if query_return is not None else [])
    return client


def _test_file_row(repo_path: str) -> list:
    return [repo_path]


# ---------------------------------------------------------------------------
# TCLINK-RESOLVE-001 — candidate generation
# ---------------------------------------------------------------------------


# @spec TCLINK-RESOLVE-001
def test_candidate_paths_most_specific_first():
    from modok.ingestion.ci_ingestion import _candidate_paths_for_classname

    candidates = _candidate_paths_for_classname("tests.test_dependency_models")
    assert candidates == ["tests/test_dependency_models.py", "tests.py"]


# @spec TCLINK-RESOLVE-001
def test_candidate_paths_class_grouped_test():
    from modok.ingestion.ci_ingestion import _candidate_paths_for_classname

    candidates = _candidate_paths_for_classname("test_classy.TestSomething")
    assert candidates == ["test_classy/TestSomething.py", "test_classy.py"]


# @spec TCLINK-RESOLVE-001
def test_candidate_paths_single_segment():
    from modok.ingestion.ci_ingestion import _candidate_paths_for_classname

    assert _candidate_paths_for_classname("test_classy") == ["test_classy.py"]


# ---------------------------------------------------------------------------
# TCLINK-RESOLVE-002/003/004/005/006 — resolution
# ---------------------------------------------------------------------------


# @spec TCLINK-RESOLVE-004
@pytest.mark.asyncio
async def test_resolve_single_exact_match_at_full_specificity():
    from modok.ingestion.ci_ingestion import resolve_test_execution_link

    client = _mock_client(query_return=[_test_file_row("tests/test_dependency_models.py")])
    link_state, path = await resolve_test_execution_link(client, "stagehand", "tests.test_dependency_models")
    assert link_state == "resolved"
    assert path == "tests/test_dependency_models.py"


# @spec TCLINK-RESOLVE-001, TCLINK-RESOLVE-003, TCLINK-RESOLVE-004
@pytest.mark.asyncio
async def test_resolve_class_grouped_falls_back_to_less_specific_candidate():
    """test_classy.TestSomething: the full-specificity candidate
    (test_classy/TestSomething.py) has no match, so the next candidate
    (test_classy.py) is tried and resolves."""
    from modok.ingestion.ci_ingestion import resolve_test_execution_link

    async def mock_query(cypher, params=None):
        params = params or {}
        if params.get("exact") == "test_classy/TestSomething.py":
            return []
        if params.get("exact") == "test_classy.py":
            return [_test_file_row("test_classy.py")]
        return []

    client = _mock_client()
    client.query = AsyncMock(side_effect=mock_query)

    link_state, path = await resolve_test_execution_link(client, "stagehand", "test_classy.TestSomething")
    assert link_state == "resolved"
    assert path == "test_classy.py"


# @spec TCLINK-RESOLVE-005
@pytest.mark.asyncio
async def test_resolve_ambiguous_stops_does_not_try_less_specific_candidate():
    from modok.ingestion.ci_ingestion import resolve_test_execution_link

    call_log = []

    async def mock_query(cypher, params=None):
        params = params or {}
        call_log.append(params.get("exact"))
        if params.get("exact") == "tests/test_output_consistency.py":
            return [
                _test_file_row("client/tests/test_output_consistency.py"),
                _test_file_row("agent/tests/test_output_consistency.py"),
            ]
        return []

    client = _mock_client()
    client.query = AsyncMock(side_effect=mock_query)

    link_state, path = await resolve_test_execution_link(client, "stagehand", "tests.test_output_consistency")
    assert link_state == "ambiguous"
    assert path is None
    # Only the full-specificity candidate should have been queried — no
    # fallback to a shorter candidate once a match (even an ambiguous one)
    # was found.
    assert call_log == ["tests/test_output_consistency.py"]


# @spec TCLINK-RESOLVE-006
@pytest.mark.asyncio
async def test_resolve_no_candidate_matches_is_unresolved():
    from modok.ingestion.ci_ingestion import resolve_test_execution_link

    client = _mock_client(query_return=[])
    link_state, path = await resolve_test_execution_link(client, "stagehand", "agent.tests.SomeCppSuite")
    assert link_state == "unresolved"
    assert path is None


# @spec TCLINK-RESOLVE-002
@pytest.mark.asyncio
async def test_single_segment_candidate_is_exact_match_only_no_suffix():
    """A single-segment classname (e.g. bare 'conftest') must not be checked
    via suffix match — only exact match — to avoid a false-positive match
    against an unrelated, deeply-nested file of the same name."""
    from modok.ingestion.ci_ingestion import resolve_test_execution_link

    client = _mock_client(query_return=[])
    await resolve_test_execution_link(client, "stagehand", "conftest")

    call = client.query.call_args
    cypher_text = call[0][0]
    assert "ENDS WITH" not in cypher_text.upper()


# @spec TCLINK-RESOLVE-002
@pytest.mark.asyncio
async def test_multi_segment_candidate_checked_by_exact_and_suffix():
    """"tests.test_output_consistency" -> first (most specific) candidate is
    "tests/test_output_consistency.py" (2 segments) — checked via the first
    call specifically, since with an empty mock a second, single-segment
    candidate ("tests.py") is also tried and correctly has no ENDS WITH."""
    from modok.ingestion.ci_ingestion import resolve_test_execution_link

    client = _mock_client(query_return=[])
    await resolve_test_execution_link(client, "stagehand", "tests.test_output_consistency")

    first_call = client.query.call_args_list[0]
    cypher_text = first_call[0][0]
    assert "ENDS WITH" in cypher_text.upper()


# ---------------------------------------------------------------------------
# TCLINK-EDGE-001/003 — EXECUTES edge + link_state
# ---------------------------------------------------------------------------


# @spec TCLINK-EDGE-001, TCLINK-EDGE-003
@pytest.mark.asyncio
async def test_link_test_execution_to_file_writes_executes_when_resolved():
    from modok.ingestion.ci_ingestion import link_test_execution_to_file

    client = _mock_client(query_return=[_test_file_row("tests/test_foo.py")])
    await link_test_execution_to_file(
        client,
        "stagehand",
        run_id="100",
        run_attempt=1,
        execution={"classname": "tests.test_foo", "test_name": "test_bar", "status": "passed"},
    )

    calls = [str(c) for c in client.write_edge_by_parts.call_args_list]
    assert any("EXECUTES" in c for c in calls)
    node = client.upsert_node.call_args[0][0]
    assert node.link_state == "resolved"


# @spec TCLINK-EDGE-001, TCLINK-EDGE-003, TCLINK-ERR-001
@pytest.mark.asyncio
async def test_link_test_execution_to_file_no_edge_when_unresolved():
    from modok.ingestion.ci_ingestion import link_test_execution_to_file

    client = _mock_client(query_return=[])
    await link_test_execution_to_file(
        client,
        "stagehand",
        run_id="100",
        run_attempt=1,
        execution={"classname": "agent.tests.SomeCppSuite", "test_name": "test_bar", "status": "passed"},
    )

    calls = [str(c) for c in client.write_edge_by_parts.call_args_list]
    assert not any("EXECUTES" in c for c in calls)
    node = client.upsert_node.call_args[0][0]
    # A no-match result is never persisted as "unresolved" — link_state stays
    # unset so the reconciliation sweep keeps retrying it indefinitely
    # (TCLINK-POLL-002; a bounded-attempts exclusion was drafted and
    # rejected during the Phase 2 edge-case probe as itself incorrect).
    assert node.link_state is None


# @spec TCLINK-EDGE-002
@pytest.mark.asyncio
async def test_link_test_execution_to_file_never_addresses_test_failure():
    """EXECUTES is only ever written from TestExecution, never TestFailure —
    a TestFailure's file is reached via OCCURRED_IN -> TestExecution ->
    EXECUTES, not a direct edge of its own."""
    from modok.ingestion.ci_ingestion import link_test_execution_to_file

    client = _mock_client(query_return=[_test_file_row("tests/test_foo.py")])
    await link_test_execution_to_file(
        client,
        "stagehand",
        run_id="100",
        run_attempt=1,
        execution={"classname": "tests.test_foo", "test_name": "test_bar", "status": "failed"},
    )

    for call in client.write_edge_by_parts.call_args_list:
        from_parts = call[0][0]
        assert from_parts[0] != "test-failure"


# ---------------------------------------------------------------------------
# TCLINK-POLL-001 — inline resolution wiring
# ---------------------------------------------------------------------------


# @spec TCLINK-POLL-001
@pytest.mark.asyncio
async def test_expand_workflow_run_calls_link_test_execution_to_file():
    from modok.ingestion.ci_ingestion import expand_workflow_run

    client = _mock_client()
    calls = []

    async def _fake_link(client, project_slug, **kwargs):
        calls.append(kwargs)

    with patch(
        "modok.ingestion.ci_ingestion._fetch_jobs", new=AsyncMock(return_value=[])
    ), patch(
        "modok.ingestion.ci_ingestion._fetch_artifact", new=AsyncMock(return_value=b"fake-zip-bytes")
    ), patch(
        "modok.ingestion.ci_ingestion._parse_junit",
        return_value=[{"classname": "tests.test_foo", "test_name": "test_bar", "status": "passed"}],
    ), patch(
        "modok.ingestion.ci_ingestion.link_test_execution_to_file", new=_fake_link
    ):
        await expand_workflow_run(client, "stagehand", run_id="100", token="tok")

    assert len(calls) == 1
    assert calls[0]["run_id"] == "100"
    assert calls[0]["execution"]["classname"] == "tests.test_foo"


# ---------------------------------------------------------------------------
# TCLINK-POLL-002/003/004 — reconciliation sweep
# ---------------------------------------------------------------------------


# @spec TCLINK-POLL-002
@pytest.mark.asyncio
async def test_reconcile_sweep_query_includes_unset_and_ambiguous():
    """Inclusion-style filter: the sweep asks for link_state unset or
    'ambiguous' — a no-match ('unresolved') result is retried indefinitely
    simply by never being persisted as anything other than unset
    (TCLINK-POLL-002), not via an explicit exclusion clause naming it."""
    from modok.ingestion.ci_ingestion import reconcile_test_execution_links

    client = _mock_client(query_return=[])
    await reconcile_test_execution_links(client, "stagehand")

    query_text = client.query.call_args_list[0][0][0]
    assert "ambiguous" in query_text
    assert "TestExecution" in query_text
    assert "link_state" in query_text


# @spec TCLINK-POLL-003
@pytest.mark.asyncio
async def test_reconcile_sweep_resolves_and_writes_edge():
    from modok.ingestion.ci_ingestion import reconcile_test_execution_links

    te_row = [
        {
            "properties": {
                "node_type": "TestExecution",
                "project_slug": "stagehand",
                "run_id": "100",
                "run_attempt": 1,
                "classname": "tests.test_foo",
                "test_name": "test_bar",
                "status": "passed",
                "suite_name": "",
            }
        }
    ]

    call_count = 0

    async def mock_query(cypher, params=None):
        nonlocal call_count
        call_count += 1
        if "TestExecution" in cypher and "ambiguous" in cypher:
            return [te_row]
        return [_test_file_row("tests/test_foo.py")]

    client = _mock_client()
    client.query = AsyncMock(side_effect=mock_query)

    await reconcile_test_execution_links(client, "stagehand")

    calls = [str(c) for c in client.write_edge_by_parts.call_args_list]
    assert any("EXECUTES" in c for c in calls)
    node = client.upsert_node.call_args[0][0]
    assert node.link_state == "resolved"


# @spec TCLINK-POLL-003
@pytest.mark.asyncio
async def test_reconcile_sweep_sets_ambiguous_not_unresolved_on_multi_match():
    from modok.ingestion.ci_ingestion import reconcile_test_execution_links

    te_row = [
        {
            "properties": {
                "node_type": "TestExecution",
                "project_slug": "stagehand",
                "run_id": "100",
                "run_attempt": 1,
                "classname": "tests.test_foo",
                "test_name": "test_bar",
                "status": "passed",
                "suite_name": "",
            }
        }
    ]

    async def mock_query(cypher, params=None):
        if "TestExecution" in cypher and "ambiguous" in cypher:
            return [te_row]
        return [_test_file_row("client/tests/test_foo.py"), _test_file_row("agent/tests/test_foo.py")]

    client = _mock_client()
    client.query = AsyncMock(side_effect=mock_query)

    await reconcile_test_execution_links(client, "stagehand")

    node = client.upsert_node.call_args[0][0]
    assert node.link_state == "ambiguous"


# @spec TCLINK-POLL-004
@pytest.mark.asyncio
async def test_inline_and_reconcile_both_call_the_same_patched_resolution_function():
    """Patching resolve_test_execution_link at the module level must affect
    both call sites — proof they call the same function object, not two
    independently-implemented copies of the resolution logic."""
    import modok.ingestion.ci_ingestion as ci_mod

    fake_resolve = AsyncMock(return_value=("unresolved", None))

    client = _mock_client(query_return=[])
    with patch.object(ci_mod, "resolve_test_execution_link", new=fake_resolve):
        await ci_mod.link_test_execution_to_file(
            client, "stagehand", run_id="1", run_attempt=1,
            execution={"classname": "a.b", "test_name": "t", "status": "passed"},
        )
    assert fake_resolve.await_count == 1
    assert fake_resolve.await_args.args[-1] == "a.b"

    te_row = [
        {
            "properties": {
                "node_type": "TestExecution",
                "project_slug": "stagehand",
                "run_id": "100",
                "run_attempt": 1,
                "classname": "c.d",
                "test_name": "t2",
                "status": "passed",
                "suite_name": "",
            }
        }
    ]

    async def sweep_query(cypher, params=None):
        if "TestExecution" in cypher and "ambiguous" in cypher:
            return [te_row]
        return []

    client2 = _mock_client()
    client2.query = AsyncMock(side_effect=sweep_query)
    with patch.object(ci_mod, "resolve_test_execution_link", new=fake_resolve):
        await ci_mod.reconcile_test_execution_links(client2, "stagehand")

    assert fake_resolve.await_count == 2
    assert fake_resolve.await_args.args[-1] == "c.d"


# @spec TCLINK-POLL-005
@pytest.mark.asyncio
async def test_reconcile_sweep_one_bad_node_does_not_raise_or_block_others():
    from modok.ingestion.ci_ingestion import reconcile_test_execution_links

    good_row = [
        {
            "properties": {
                "node_type": "TestExecution",
                "project_slug": "stagehand",
                "run_id": "100",
                "run_attempt": 1,
                "classname": "tests.test_good",
                "test_name": "test_x",
                "status": "passed",
                "suite_name": "",
            }
        }
    ]
    bad_row = [
        {
            "properties": {
                "node_type": "TestExecution",
                "project_slug": "stagehand",
                "run_id": "101",
                "run_attempt": 1,
                "classname": "tests.test_bad",
                "test_name": "test_y",
                "status": "passed",
                "suite_name": "",
            }
        }
    ]

    async def mock_query(cypher, params=None):
        if "TestExecution" in cypher and "ambiguous" in cypher:
            return [bad_row, good_row]
        raise RuntimeError("Quine unreachable for this lookup")

    client = _mock_client()
    client.query = AsyncMock(side_effect=mock_query)

    # Must not raise despite the resolution lookup failing for the bad row.
    await reconcile_test_execution_links(client, "stagehand")


# ---------------------------------------------------------------------------
# TCLINK-SCOPE-001 — no Investigation/comment side effects
# ---------------------------------------------------------------------------


# @spec TCLINK-SCOPE-001
@pytest.mark.asyncio
async def test_link_test_execution_to_file_never_writes_investigation_nodes():
    from modok.ingestion.ci_ingestion import link_test_execution_to_file

    client = _mock_client(query_return=[_test_file_row("tests/test_foo.py")])
    await link_test_execution_to_file(
        client,
        "stagehand",
        run_id="100",
        run_attempt=1,
        execution={"classname": "tests.test_foo", "test_name": "test_bar", "status": "passed"},
    )

    for call in client.upsert_node.call_args_list:
        assert call.args[0].node_type not in ("Investigation", "InvestigationMilestone")


# ---------------------------------------------------------------------------
# TCLINK-ERR-002 — ambiguous vs. unresolved logged distinctly
# ---------------------------------------------------------------------------


# @spec TCLINK-ERR-002
@pytest.mark.asyncio
async def test_ambiguous_and_unresolved_are_logged_distinctly(capsys):
    from modok.ingestion.ci_ingestion import link_test_execution_to_file

    client_ambiguous = _mock_client(
        query_return=[
            _test_file_row("client/tests/test_output_consistency.py"),
            _test_file_row("agent/tests/test_output_consistency.py"),
        ]
    )
    await link_test_execution_to_file(
        client_ambiguous,
        "stagehand",
        run_id="100",
        run_attempt=1,
        execution={"classname": "tests.test_output_consistency", "test_name": "test_x", "status": "passed"},
    )
    ambiguous_log = capsys.readouterr().err

    client_unresolved = _mock_client(query_return=[])
    await link_test_execution_to_file(
        client_unresolved,
        "stagehand",
        run_id="101",
        run_attempt=1,
        execution={"classname": "agent.tests.SomeCppSuite", "test_name": "test_y", "status": "passed"},
    )
    unresolved_log = capsys.readouterr().err

    assert ambiguous_log != unresolved_log


# ---------------------------------------------------------------------------
# TCLINK-SCOPE-003 — no reconciliation of EXECUTES on TestFile rename/deletion
# ---------------------------------------------------------------------------


# @spec TCLINK-SCOPE-003
@pytest.mark.asyncio
async def test_resolved_test_execution_is_excluded_from_reconciliation_sweep():
    """An already-resolved TestExecution (link_state == 'resolved') must not
    be re-examined by the sweep at all — even if the TestFile it pointed at
    was later renamed or deleted, this component does not re-resolve or
    modify the existing EXECUTES edge (docs/llds/test-coverage-ci-linking.md
    § Open Questions, item 5). The sweep's inclusion-style filter (link_state
    unset or 'ambiguous') excludes 'resolved' by simply never naming it as
    an allowed value — asserted here by checking the quoted literal
    'resolved' (leading quote distinguishes it from 'unresolved', which this
    design also never persists — see TCLINK-POLL-002/003) never appears in
    the sweep query at all."""
    from modok.ingestion.ci_ingestion import reconcile_test_execution_links

    client = _mock_client(query_return=[])
    await reconcile_test_execution_links(client, "stagehand")

    query_text = client.query.call_args_list[0][0][0].lower()
    assert "'resolved'" not in query_text
