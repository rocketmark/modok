"""
Tests for modok.quine.standing_queries — the standing-query definition
loader and QuineClient's standing-query REST methods.
All tests are written before implementation (Phase 5). Every test cites
the EARS spec it verifies via @spec annotation.

Specs verified:
  SQ-DEF-001, SQ-DEF-002, SQ-DEF-003,
  SQ-CLIENT-001, SQ-CLIENT-002, SQ-CLIENT-003, SQ-CLIENT-004, SQ-CLIENT-005.
"""

from __future__ import annotations

import json

import httpx
import pytest

from modok.quine.client import QuineClient
from modok.quine.standing_queries.loader import (
    StandingQueryDefinition,
    load_definition,
    all_definitions,
)


def make_client(transport: httpx.MockTransport) -> QuineClient:
    return QuineClient(base_url="http://localhost:8080", transport=transport)


# ---------------------------------------------------------------------------
# SQ-DEF-001 — definitions load from checked-in YAML, not inline strings
# ---------------------------------------------------------------------------


# @spec SQ-DEF-001
def test_load_definition_returns_standing_query_definition():
    definition = load_definition("actionable-issue-pattern")
    assert isinstance(definition, StandingQueryDefinition)
    assert definition.name == "actionable-issue-pattern"


# @spec SQ-DEF-001
def test_all_definitions_includes_actionable_issue_pattern():
    names = [d.name for d in all_definitions()]
    assert "actionable-issue-pattern" in names


# ---------------------------------------------------------------------------
# SQ-DEF-002 — DistinctId mode, RETURN DISTINCT id(ci)
# ---------------------------------------------------------------------------


# @spec SQ-DEF-002
def test_actionable_issue_pattern_uses_distinct_id_mode():
    definition = load_definition("actionable-issue-pattern")
    assert definition.mode == "DistinctId"


# @spec SQ-DEF-002
def test_actionable_issue_pattern_returns_distinct_customer_issue_id():
    definition = load_definition("actionable-issue-pattern")
    pattern = definition.pattern.upper()
    assert "RETURN DISTINCT ID(CI)" in pattern.replace("\n", " ")
    assert "CUSTOMERISSUE" in pattern.replace("\n", " ") or "CUSTOMER_ISSUE" in pattern


# @spec SQ-DEF-002
def test_actionable_issue_pattern_matches_full_evidence_chain():
    definition = load_definition("actionable-issue-pattern")
    pattern = definition.pattern
    assert "HAS_ERROR" in pattern
    assert "RESOLVED_BY" in pattern
    assert "KnownIssue" in pattern
    assert "Fix" in pattern


# @spec SQ-DEF-002
def test_actionable_issue_pattern_filters_by_node_type_property_not_label():
    # Confirmed via live verification against Quine 1.10.0: node_type is
    # stored as a property, not a real Quine label — `(n:CustomerIssue)`
    # matches nothing even when n.node_type == "CustomerIssue". The pattern
    # must not rely on `:Label` syntax for its four node types.
    definition = load_definition("actionable-issue-pattern")
    pattern = definition.pattern
    assert "(ci:" not in pattern
    assert "(e:" not in pattern
    assert "(ki:" not in pattern
    assert "(fix:" not in pattern
    assert "ci.node_type = 'CustomerIssue'" in pattern
    assert "e.node_type = 'ErrorSignature'" in pattern
    assert "ki.node_type = 'KnownIssue'" in pattern
    assert "fix.node_type = 'Fix'" in pattern


# ---------------------------------------------------------------------------
# SQ-DEF-003 — no project_slug filter; topology provides isolation
# ---------------------------------------------------------------------------


# @spec SQ-DEF-003
def test_actionable_issue_pattern_has_no_project_slug_filter():
    # The pattern's WHERE clause exists for node_type property filtering
    # (real Quine stores node_type as a property, not a queryable label — see
    # docs/llds/standing-queries.md § Live Verification Findings), not for
    # project isolation. project_slug must not appear anywhere in the pattern.
    definition = load_definition("actionable-issue-pattern")
    assert "project_slug" not in definition.pattern


# ---------------------------------------------------------------------------
# SQ-CLIENT-001 — standing_query_exists
# ---------------------------------------------------------------------------


# @spec SQ-CLIENT-001
@pytest.mark.asyncio
async def test_standing_query_exists_true_on_200():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"name": "x"}))
    client = make_client(transport)
    assert await client.standing_query_exists("actionable-issue-pattern") is True


# @spec SQ-CLIENT-001
@pytest.mark.asyncio
async def test_standing_query_exists_false_on_404():
    transport = httpx.MockTransport(lambda r: httpx.Response(404, json={"error": "not found"}))
    client = make_client(transport)
    assert await client.standing_query_exists("actionable-issue-pattern") is False


# ---------------------------------------------------------------------------
# SQ-CLIENT-002 — install registers a new standing query
# ---------------------------------------------------------------------------


# @spec SQ-CLIENT-002
@pytest.mark.asyncio
async def test_install_standing_query_registers_when_absent():
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.method == "GET":
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json={})

    client = make_client(httpx.MockTransport(handler))
    definition = load_definition("actionable-issue-pattern")
    result = await client.install_standing_query(definition, "http://127.0.0.1:4242/standing-query/result")

    assert result is True
    post_requests = [r for r in requests_seen if r.method == "POST"]
    assert len(post_requests) == 1
    body = json.loads(post_requests[0].content)
    assert body["pattern"]["type"] == "Cypher"
    assert body["pattern"]["mode"] == "DistinctId"
    assert "outputs" in body


# @spec SQ-CLIENT-002
@pytest.mark.asyncio
async def test_install_standing_query_includes_callback_url_in_outputs():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404, json={})
        return httpx.Response(200, json={})

    client = make_client(httpx.MockTransport(handler))
    definition = load_definition("actionable-issue-pattern")
    calls = []

    def capturing_handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    client = make_client(httpx.MockTransport(capturing_handler))
    await client.install_standing_query(definition, "http://127.0.0.1:4242/standing-query/result")
    post_body = json.loads([r for r in calls if r.method == "POST"][0].content)
    assert "127.0.0.1:4242/standing-query/result" in json.dumps(post_body)


# ---------------------------------------------------------------------------
# SQ-CLIENT-003 — install is idempotent, no request when already present
# ---------------------------------------------------------------------------


# @spec SQ-CLIENT-003
@pytest.mark.asyncio
async def test_install_standing_query_noop_when_already_present():
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(200, json={"name": "actionable-issue-pattern"})

    client = make_client(httpx.MockTransport(handler))
    definition = load_definition("actionable-issue-pattern")
    result = await client.install_standing_query(definition, "http://127.0.0.1:4242/standing-query/result")

    assert result is False
    assert all(r.method == "GET" for r in requests_seen)
    assert not any(r.method == "POST" for r in requests_seen)


# ---------------------------------------------------------------------------
# SQ-CLIENT-004 — list_standing_queries
# ---------------------------------------------------------------------------


# @spec SQ-CLIENT-004
@pytest.mark.asyncio
async def test_list_standing_queries_returns_names():
    transport = httpx.MockTransport(
        lambda r: httpx.Response(
            200, json=[{"name": "actionable-issue-pattern"}, {"name": "other-query"}]
        )
    )
    client = make_client(transport)
    names = await client.list_standing_queries()
    assert "actionable-issue-pattern" in names
    assert "other-query" in names


# @spec SQ-CLIENT-004
@pytest.mark.asyncio
async def test_list_standing_queries_empty_when_none_installed():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
    client = make_client(transport)
    assert await client.list_standing_queries() == []


# ---------------------------------------------------------------------------
# SQ-CLIENT-005 — remove is idempotent
# ---------------------------------------------------------------------------


# @spec SQ-CLIENT-005
@pytest.mark.asyncio
async def test_remove_standing_query_deletes_when_present():
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"name": "actionable-issue-pattern"})
        return httpx.Response(200, json={})

    client = make_client(httpx.MockTransport(handler))
    result = await client.remove_standing_query("actionable-issue-pattern")

    assert result is True
    assert any(r.method == "DELETE" for r in requests_seen)


# @spec SQ-CLIENT-005
@pytest.mark.asyncio
async def test_remove_standing_query_noop_when_absent():
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(404, json={"error": "not found"})

    client = make_client(httpx.MockTransport(handler))
    result = await client.remove_standing_query("actionable-issue-pattern")

    assert result is False
    assert not any(r.method == "DELETE" for r in requests_seen)
