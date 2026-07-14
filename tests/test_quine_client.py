"""
Tests for modok.quine — Quine client, ID scheme, models, and errors.
All tests are written before implementation (Phase 5). Every test cites
the EARS spec it verifies via @spec annotation.
"""

from __future__ import annotations

import pytest
import httpx
from unittest.mock import patch, AsyncMock
from hypothesis import given, assume, settings
from hypothesis import strategies as st

from modok.quine.ids import idFrom
from modok.quine.errors import (
    QuineNodeNotFoundError,
    QuineDeserializationError,
)
from modok.quine.models import (
    Project,
    Feature,
)
from modok.quine.client import QuineClient, TraversalStep


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

slugs = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=32)

ALL_NODE_TYPE_NAMES = [
    "project",
    "product-area",
    "feature",
    "module",
    "file",
    "doc",
    "doc-section",
    "test-plan",
    "test-case",
    "known-issue",
    "customer-issue",
    "error",
    "fix",
    "resolution",
    "decision",
    "risk",
    "failure-mode",
    "observation",
    "similarity-match",
    "diagnostic-note",
    "deployment",
]


def make_project(slug: str = "proj-a") -> Project:
    return Project(node_type="Project", project_slug=slug, name="Project A")


def make_feature(project_slug: str = "proj-a", feature_slug: str = "feat-x") -> Feature:
    return Feature(
        node_type="Feature",
        project_slug=project_slug,
        feature_slug=feature_slug,
        name="Feature X",
    )


# ---------------------------------------------------------------------------
# HiFi harness idFrom helper — determinism and collision properties.
# These tests verify modok.quine.ids.idFrom, which is used exclusively by
# the HiFi test harness (DummyQuine, ReferenceModok, scenario loader).
# Production MODOK uses Quine's Cypher-native idFrom() function instead.
# ---------------------------------------------------------------------------


def test_idFrom_is_deterministic():
    assert idFrom("feature", "proj-a", "feat-x") == idFrom("feature", "proj-a", "feat-x")


def test_idFrom_returns_int():
    result = idFrom("feature", "proj-a", "feat-x")
    assert isinstance(result, int)


def test_idFrom_fits_signed_int64():
    result = idFrom("feature", "proj-a", "feat-x")
    assert -(2**63) <= result < 2**63


@given(parts=st.lists(slugs, min_size=1, max_size=5))
def test_idFrom_always_deterministic(parts):
    assert idFrom(*parts) == idFrom(*parts)


@given(parts=st.lists(slugs, min_size=1, max_size=5))
def test_idFrom_always_fits_signed_int64(parts):
    result = idFrom(*parts)
    assert -(2**63) <= result < 2**63


def test_idFrom_null_byte_separator_prevents_collision():
    assert idFrom("a", "bc") != idFrom("ab", "c")


@given(
    type_a=st.sampled_from(ALL_NODE_TYPE_NAMES),
    type_b=st.sampled_from(ALL_NODE_TYPE_NAMES),
    rest=st.lists(slugs, min_size=1, max_size=3),
)
def test_different_node_types_produce_different_ids(type_a, type_b, rest):
    assume(type_a != type_b)
    assert idFrom(type_a, *rest) != idFrom(type_b, *rest)


@given(slug_a=slugs, slug_b=slugs, rest=st.lists(slugs, min_size=1, max_size=3))
def test_different_project_slugs_produce_different_ids(slug_a, slug_b, rest):
    assume(slug_a != slug_b)
    assert idFrom("feature", slug_a, *rest) != idFrom("feature", slug_b, *rest)


def test_customer_issue_id_is_deterministic():
    id1 = idFrom("customer-issue", "stagehand", "zendesk", "1842")
    id2 = idFrom("customer-issue", "stagehand", "zendesk", "1842")
    assert id1 == id2


def test_customer_issue_id_differs_by_project():
    id_a = idFrom("customer-issue", "stagehand", "zendesk", "1842")
    id_b = idFrom("customer-issue", "other-project", "zendesk", "1842")
    assert id_a != id_b


def test_customer_issue_id_differs_by_source_system():
    id_zendesk = idFrom("customer-issue", "stagehand", "zendesk", "1842")
    id_github = idFrom("customer-issue", "stagehand", "github", "1842")
    assert id_zendesk != id_github


def test_resolution_event_id_includes_source_system():
    id_zendesk = idFrom("resolution", "proj-a", "zendesk", "1842", "fix-001")
    id_github = idFrom("resolution", "proj-a", "github", "1842", "fix-001")
    assert id_zendesk != id_github


@given(slug_a=slugs, slug_b=slugs)
def test_similarity_match_id_includes_project_slug(slug_a, slug_b):
    assume(slug_a != slug_b)
    ci_id = str(idFrom("customer-issue", slug_a, "zendesk", "1842"))
    ki_id = str(idFrom("known-issue", slug_a, "issue-001"))
    id_a = idFrom("similarity-match", slug_a, ci_id, ki_id, "graph-anchor")
    id_b = idFrom("similarity-match", slug_b, ci_id, ki_id, "graph-anchor")
    assert id_a != id_b


# ---------------------------------------------------------------------------
# Unit tests for QuineClient — use httpx.MockTransport
# ---------------------------------------------------------------------------


def make_client(transport: httpx.MockTransport) -> QuineClient:
    return QuineClient(base_url="http://localhost:8080", transport=transport)


def quine_upsert_response() -> httpx.Response:
    return httpx.Response(200, json={})


def quine_node_response(node_id: int, properties: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "columns": ["n"],
            "results": [[{"id": node_id, "labels": [], "properties": properties}]],
        },
    )


def quine_empty_response() -> httpx.Response:
    return httpx.Response(200, json={"columns": ["n"], "results": []})


def quine_error_response(status: int) -> httpx.Response:
    return httpx.Response(status, json={"error": "quine error"})


# ---------------------------------------------------------------------------
# QC-NW-001 — upsert creates node when missing
# ---------------------------------------------------------------------------


# @spec QC-NW-001
@pytest.mark.asyncio
async def test_upsert_node_creates_new_node():
    responses = [quine_upsert_response()]
    transport = httpx.MockTransport(lambda r: responses.pop(0))
    client = make_client(transport)
    node = make_project()
    await client.upsert_node(node)  # must not raise


# ---------------------------------------------------------------------------
# QC-NW-002 — upsert replaces all properties on existing node
# ---------------------------------------------------------------------------


# @spec QC-NW-002
@pytest.mark.asyncio
async def test_upsert_node_replaces_properties():
    call_bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        call_bodies.append(request.content)
        return quine_upsert_response()

    client = make_client(httpx.MockTransport(handler))
    node = make_project()
    await client.upsert_node(node)
    await client.upsert_node(node)
    assert len(call_bodies) == 2


# @spec QC-NW-002
@given(
    name_a=st.text(
        alphabet=st.characters(whitelist_categories=("L", "N")), min_size=1, max_size=50
    ),
    name_b=st.text(
        alphabet=st.characters(whitelist_categories=("L", "N")), min_size=1, max_size=50
    ),
)
@pytest.mark.asyncio
async def test_upsert_node_sends_full_property_set(name_a, name_b):
    assume(name_a != name_b)
    sent_params = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        sent_params.append(_json.loads(request.content)["parameters"])
        return quine_upsert_response()

    client = make_client(httpx.MockTransport(handler))
    await client.upsert_node(Project(node_type="Project", project_slug="p", name=name_a))
    await client.upsert_node(Project(node_type="Project", project_slug="p", name=name_b))
    assert sent_params[1]["name"] == name_b
    assert sent_params[0]["name"] == name_a


# ---------------------------------------------------------------------------
# QC-NW-003 — upsert never modifies edges
# ---------------------------------------------------------------------------


# @spec QC-NW-003
@pytest.mark.asyncio
async def test_upsert_node_does_not_send_edge_mutations():
    # All Quine operations go through POST /api/v1/query/cypher.
    # upsert_node must only send SET/MERGE statements — never CREATE relationship.
    cypher_bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        cypher_bodies.append(json.loads(request.content)["text"])
        return quine_upsert_response()

    client = make_client(httpx.MockTransport(handler))
    await client.upsert_node(make_feature())
    assert len(cypher_bodies) == 1
    cypher = cypher_bodies[0].upper()
    assert "CREATE" not in cypher or "-[" not in cypher  # no relationship CREATE
    assert "SET" in cypher or "MERGE" in cypher  # must be a property write


# ---------------------------------------------------------------------------
# QC-NW-004 — edge lifecycle is exclusively via write_edge
# ---------------------------------------------------------------------------


# @spec QC-NW-004
@pytest.mark.asyncio
async def test_removing_property_does_not_trigger_edge_change():
    # All Quine operations go through POST /api/v1/query/cypher.
    # Dropping product_area_slug from a Feature must not produce a
    # relationship CREATE/DELETE in either Cypher call.
    import json

    cypher_bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        cypher_bodies.append(json.loads(request.content)["text"])
        return quine_upsert_response()

    client = make_client(httpx.MockTransport(handler))
    full = Feature(
        node_type="Feature",
        project_slug="p",
        feature_slug="f",
        name="F",
        product_area_slug="billing",
    )
    sparse = Feature(
        node_type="Feature", project_slug="p", feature_slug="f", name="F", product_area_slug=None
    )
    await client.upsert_node(full)
    await client.upsert_node(sparse)
    assert len(cypher_bodies) == 2
    for cypher in cypher_bodies:
        upper = cypher.upper()
        # Neither call may contain a relationship CREATE or DELETE
        assert "-[" not in upper, f"Unexpected relationship operation in: {cypher}"
        assert "DELETE" not in upper, f"Unexpected DELETE in: {cypher}"


# ---------------------------------------------------------------------------
# QC-NR-001 — get_node returns deserialized node
# ---------------------------------------------------------------------------


# @spec QC-NR-001
@pytest.mark.asyncio
async def test_get_node_returns_deserialized_node():
    from modok.quine.ids import idFrom as _idFrom

    node_id = _idFrom("project", "proj-a")
    transport = httpx.MockTransport(
        lambda r: quine_node_response(
            node_id, {"node_type": "Project", "project_slug": "proj-a", "name": "Project A"}
        )
    )
    client = make_client(transport)
    result = await client.get_node(node_id, Project)
    assert isinstance(result, Project)
    assert result.project_slug == "proj-a"


# ---------------------------------------------------------------------------
# QC-NR-002 — get_node raises QuineNodeNotFoundError when missing
# ---------------------------------------------------------------------------


# @spec QC-NR-002
@pytest.mark.asyncio
async def test_get_node_raises_on_missing():
    transport = httpx.MockTransport(lambda r: quine_empty_response())
    client = make_client(transport)
    with pytest.raises(QuineNodeNotFoundError):
        await client.get_node(999999, Project)


# ---------------------------------------------------------------------------
# QC-NR-003 — node_exists returns bool without raising
# ---------------------------------------------------------------------------


# @spec QC-NR-003
@pytest.mark.asyncio
async def test_node_exists_returns_true_when_present():
    from modok.quine.ids import idFrom as _idFrom

    node_id = _idFrom("project", "proj-a")
    transport = httpx.MockTransport(
        lambda r: quine_node_response(
            node_id, {"node_type": "Project", "project_slug": "proj-a", "name": "Project A"}
        )
    )
    client = make_client(transport)
    assert await client.node_exists(node_id) is True


# @spec QC-NR-003
@pytest.mark.asyncio
async def test_node_exists_returns_false_when_absent():
    transport = httpx.MockTransport(lambda r: quine_empty_response())
    client = make_client(transport)
    assert await client.node_exists(999999) is False


# @spec QC-NR-003
@pytest.mark.asyncio
async def test_node_exists_returns_false_for_empty_property_shell():
    """MATCH (n) WHERE id(n) = X RETURN n always returns a row in Quine, even
    for an address nothing was ever written to — an empty-property shell,
    not a real node (found live). bool(results) alone would incorrectly
    report True here; only a present node_type property counts as real."""
    transport = httpx.MockTransport(lambda r: quine_node_response("some-uuid", {}))
    client = make_client(transport)
    assert await client.node_exists("some-uuid") is False


# ---------------------------------------------------------------------------
# QC-EW-001 — write_edge creates edge when missing
# ---------------------------------------------------------------------------


# @spec QC-EW-001
@pytest.mark.asyncio
async def test_write_edge_creates_edge():
    from modok.quine.ids import idFrom as _idFrom

    requests_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return quine_upsert_response()

    client = make_client(httpx.MockTransport(handler))
    from_id = _idFrom("project", "proj-a")
    to_id = _idFrom("feature", "proj-a", "feat-x")
    await client.write_edge(from_id, "HAS_FEATURE", to_id)
    assert len(requests_seen) == 1


# ---------------------------------------------------------------------------
# QC-EW-002 — write_edge is no-op on duplicate
# ---------------------------------------------------------------------------


# @spec QC-EW-002
@pytest.mark.asyncio
async def test_write_edge_duplicate_does_not_raise():
    from modok.quine.ids import idFrom as _idFrom

    transport = httpx.MockTransport(lambda r: quine_upsert_response())
    client = make_client(transport)
    from_id = _idFrom("project", "proj-a")
    to_id = _idFrom("feature", "proj-a", "feat-x")
    await client.write_edge(from_id, "HAS_FEATURE", to_id)
    await client.write_edge(from_id, "HAS_FEATURE", to_id)  # must not raise


# @spec QC-EW-002
@given(n=st.integers(min_value=2, max_value=10))
@pytest.mark.asyncio
async def test_write_edge_idempotent_under_repetition(n):
    from modok.quine.ids import idFrom as _idFrom

    transport = httpx.MockTransport(lambda r: quine_upsert_response())
    client = make_client(transport)
    from_id = _idFrom("project", "proj-a")
    to_id = _idFrom("feature", "proj-a", "feat-x")
    for _ in range(n):
        await client.write_edge(from_id, "HAS_FEATURE", to_id)


# ---------------------------------------------------------------------------
# QC-EW-003 — edge-before-node writes are permitted (shell nodes)
# ---------------------------------------------------------------------------


# @spec QC-EW-003
@pytest.mark.asyncio
async def test_write_edge_before_upsert_does_not_raise():
    from modok.quine.ids import idFrom as _idFrom

    transport = httpx.MockTransport(lambda r: quine_upsert_response())
    client = make_client(transport)
    from_id = _idFrom("project", "proj-a")
    to_id = _idFrom("feature", "proj-a", "feat-not-yet-written")
    await client.write_edge(from_id, "HAS_FEATURE", to_id)  # must not raise


# ---------------------------------------------------------------------------
# QC-EW-004 — replace_edges deletes stale edges then recreates
# ---------------------------------------------------------------------------


# @spec QC-EW-004
@pytest.mark.asyncio
async def test_replace_edges_deletes_then_recreates():
    from modok.quine.ids import idFrom as _idFrom

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        calls.append(body["text"])
        return quine_upsert_response()

    transport = httpx.MockTransport(handler)
    client = make_client(transport)
    from_id = _idFrom("project", "proj-a")
    to_id = _idFrom("feature", "proj-a", "feat-x")

    await client.replace_edges(from_id, "HAS_FEATURE", [to_id])

    # First call must be a DELETE, second must be a MERGE (write_edge)
    assert any("DELETE" in q for q in calls), "replace_edges must DELETE stale edges"
    assert any("MERGE" in q for q in calls), "replace_edges must re-create specified edges"


# @spec QC-EW-004
@pytest.mark.asyncio
async def test_replace_edges_with_empty_list_only_deletes():
    from modok.quine.ids import idFrom as _idFrom

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        calls.append(body["text"])
        return quine_upsert_response()

    transport = httpx.MockTransport(handler)
    client = make_client(transport)
    from_id = _idFrom("project", "proj-a")

    await client.replace_edges(from_id, "HAS_FEATURE", [])

    assert len(calls) == 1
    assert "DELETE" in calls[0]


# ---------------------------------------------------------------------------
# QC-EW-005 — write_edge_by_parts accepts optional properties dict
# ---------------------------------------------------------------------------


# @spec QC-EW-005
@pytest.mark.asyncio
async def test_write_edge_by_parts_with_properties_includes_set_clause():
    import json

    queries_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        queries_seen.append(body["text"])
        return quine_upsert_response()

    client = make_client(httpx.MockTransport(handler))
    await client.write_edge_by_parts(
        ("project", "proj-a"),
        "TOUCHES",
        ("file", "proj-a", "src/main.c"),
        properties={"change_type": "M"},
    )

    assert len(queries_seen) == 1
    assert "SET" in queries_seen[0]
    assert "props" in queries_seen[0] or "change_type" in queries_seen[0]


# @spec QC-EW-005
@pytest.mark.asyncio
async def test_write_edge_by_parts_without_properties_omits_set_clause():
    import json

    queries_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        queries_seen.append(body["text"])
        return quine_upsert_response()

    client = make_client(httpx.MockTransport(handler))
    await client.write_edge_by_parts(
        ("project", "proj-a"),
        "HAS_FEATURE",
        ("feature", "proj-a", "feat-x"),
    )

    assert len(queries_seen) == 1
    assert "SET r" not in queries_seen[0]


# @spec QC-EW-005
@pytest.mark.asyncio
async def test_write_edge_by_parts_with_empty_properties_omits_set_clause():
    import json

    queries_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        queries_seen.append(body["text"])
        return quine_upsert_response()

    client = make_client(httpx.MockTransport(handler))
    await client.write_edge_by_parts(
        ("project", "proj-a"),
        "HAS_FEATURE",
        ("feature", "proj-a", "feat-x"),
        properties={},
    )

    assert len(queries_seen) == 1
    assert "SET r" not in queries_seen[0]


# ---------------------------------------------------------------------------
# QC-NR-004 — node_exists_by_parts embeds idFrom() in the query text
# ---------------------------------------------------------------------------


# @spec QC-NR-004
@pytest.mark.asyncio
async def test_node_exists_by_parts_returns_true_when_present():
    transport = httpx.MockTransport(
        lambda r: quine_node_response(
            "1b26161f-898a-3aa0-aa63-fc1489ed339d",
            {"node_type": "Feature", "project_slug": "proj-a", "feature_slug": "feat-x"},
        )
    )
    client = make_client(transport)
    assert await client.node_exists_by_parts(("feature", "proj-a", "feat-x")) is True


# @spec QC-NR-004
@pytest.mark.asyncio
async def test_node_exists_by_parts_returns_false_when_absent():
    transport = httpx.MockTransport(lambda r: quine_empty_response())
    client = make_client(transport)
    assert await client.node_exists_by_parts(("feature", "proj-a", "does-not-exist")) is False


# @spec QC-NR-004
@pytest.mark.asyncio
async def test_node_exists_by_parts_returns_false_for_empty_property_shell():
    """Same shell-node finding as node_exists (found live against a real
    Quine instance): id(n) = idFrom(...) always returns a row, even for an
    address never written to. Must not be mistaken for a real node."""
    transport = httpx.MockTransport(lambda r: quine_node_response("some-uuid", {}))
    client = make_client(transport)
    assert await client.node_exists_by_parts(("feature", "proj-a", "totally-made-up")) is False


# @spec QC-NR-004
@pytest.mark.asyncio
async def test_node_exists_by_parts_embeds_idfrom_and_never_sends_precomputed_id():
    """The whole point of this method: it must let Quine's idFrom() Cypher
    function compute the address from raw string parts, never pre-compute
    an ID in Python and send it as a literal $node_id (the bug this method
    exists to fix — see docs/llds/quine-client.md)."""
    import json

    queries_seen = []
    params_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        queries_seen.append(body["text"])
        params_seen.append(body.get("parameters", {}))
        return quine_empty_response()

    client = make_client(httpx.MockTransport(handler))
    await client.node_exists_by_parts(("feature", "proj-a", "feat-x"))

    assert "idFrom(" in queries_seen[0]
    assert "feat-x" in params_seen[0].values()


# ---------------------------------------------------------------------------
# QC-EW-006 — replace_edges_by_parts deletes stale edges then recreates,
# addressing both endpoints via idFrom() rather than a precomputed ID
# ---------------------------------------------------------------------------


# @spec QC-EW-006
@pytest.mark.asyncio
async def test_replace_edges_by_parts_deletes_then_recreates():
    import json

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["text"])
        return quine_upsert_response()

    client = make_client(httpx.MockTransport(handler))
    await client.replace_edges_by_parts(
        ("customer-issue", "proj-a", "github", "1"),
        "AFFECTS",
        [("feature", "proj-a", "feat-x")],
    )

    assert any("DELETE" in q and "idFrom(" in q for q in calls)
    assert any("MERGE" in q and "idFrom(" in q for q in calls)


# @spec QC-EW-006
@pytest.mark.asyncio
async def test_replace_edges_by_parts_with_empty_list_only_deletes():
    import json

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["text"])
        return quine_upsert_response()

    client = make_client(httpx.MockTransport(handler))
    await client.replace_edges_by_parts(("customer-issue", "proj-a", "github", "1"), "AFFECTS", [])

    assert len(calls) == 1
    assert "DELETE" in calls[0]
    assert "idFrom(" in calls[0]


# ---------------------------------------------------------------------------
# QC-TR-001 — traverse returns hydrated nodes in one round-trip
# ---------------------------------------------------------------------------


# @spec QC-TR-001
@pytest.mark.asyncio
async def test_traverse_returns_hydrated_nodes():
    from modok.quine.ids import idFrom as _idFrom

    start_id = _idFrom("project", "proj-a")
    feat_id = _idFrom("feature", "proj-a", "feat-x")

    responses = [
        httpx.Response(
            200,
            json={
                "columns": ["n"],
                "results": [
                    [
                        {
                            "id": feat_id,
                            "labels": [],
                            "properties": {
                                "node_type": "Feature",
                                "project_slug": "proj-a",
                                "feature_slug": "feat-x",
                                "name": "Feature X",
                            },
                        }
                    ]
                ],
            },
        )
    ]
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return responses[0]

    client = make_client(httpx.MockTransport(handler))
    steps = [TraversalStep("HAS_FEATURE", "out")]
    results = await client.traverse(start_id, steps)
    assert len(results) == 1
    assert isinstance(results[0], Feature)
    assert request_count == 1  # one round-trip only


# ---------------------------------------------------------------------------
# QC-TR-002 — raw query() not exposed via MCP
# ---------------------------------------------------------------------------


# @spec QC-TR-002
def test_query_method_exists_on_client():
    import inspect

    assert hasattr(QuineClient, "query")
    assert inspect.iscoroutinefunction(QuineClient.query)


# @spec QC-TR-002
# Deferred: MCP tool registration list is verified once the MCP server module
# exists (component 5). At that point, assert that `query` does not appear in
# the registered tool names. This test is intentionally omitted until then
# rather than written in a vacuous form that always passes.


# ---------------------------------------------------------------------------
# QC-TR-003 — traverse raises QuineDeserializationError on malformed node
# ---------------------------------------------------------------------------


# @spec QC-TR-003
@pytest.mark.asyncio
async def test_traverse_raises_on_malformed_node():
    from modok.quine.ids import idFrom as _idFrom

    start_id = _idFrom("project", "proj-a")

    transport = httpx.MockTransport(
        lambda r: httpx.Response(
            200,
            json={
                "columns": ["n"],
                "results": [
                    [
                        {
                            "id": 99999,
                            "labels": [],
                            "properties": {
                                "node_type": "Feature",
                                # missing required fields: project_slug, feature_slug, name
                            },
                        }
                    ]
                ],
            },
        )
    )
    client = make_client(transport)
    with pytest.raises(QuineDeserializationError):
        await client.traverse(start_id, [TraversalStep("HAS_FEATURE", "out")])


# ---------------------------------------------------------------------------
# QC-CN-001 — retry up to 3x on 5xx and timeout
# ---------------------------------------------------------------------------


# @spec QC-CN-001
@pytest.mark.asyncio
async def test_retries_on_5xx():
    attempt_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count < 3:
            return quine_error_response(500)
        return quine_upsert_response()

    with patch("modok.quine.client.asyncio.sleep", new_callable=AsyncMock):
        client = make_client(httpx.MockTransport(handler))
        await client.upsert_node(make_project())
    assert attempt_count == 3


# @spec QC-CN-001
@pytest.mark.asyncio
async def test_raises_after_3_failed_attempts():
    transport = httpx.MockTransport(lambda r: quine_error_response(500))
    client = make_client(transport)
    with patch("modok.quine.client.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(Exception):
            await client.upsert_node(make_project())


# @spec QC-CN-001
@given(n_failures=st.integers(min_value=1, max_value=3))
@settings(deadline=None)
@pytest.mark.asyncio
async def test_retries_up_to_3_times_property(n_failures):
    attempt_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count <= n_failures:
            return quine_error_response(500)
        return quine_upsert_response()

    with patch("modok.quine.client.asyncio.sleep", new_callable=AsyncMock):
        client = make_client(httpx.MockTransport(handler))
        if n_failures < 3:
            await client.upsert_node(make_project())
            assert attempt_count == n_failures + 1
        else:
            with pytest.raises(Exception):
                await client.upsert_node(make_project())


# ---------------------------------------------------------------------------
# QC-CN-002 — no retry on 4xx
# ---------------------------------------------------------------------------


# @spec QC-CN-002
@given(status=st.integers(min_value=400, max_value=499))
@pytest.mark.asyncio
async def test_no_retry_on_4xx(status):
    attempt_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempt_count
        attempt_count += 1
        return quine_error_response(status)

    client = make_client(httpx.MockTransport(handler))
    with pytest.raises(Exception):
        await client.upsert_node(make_project())
    assert attempt_count == 1


# ---------------------------------------------------------------------------
# QC-CN-003 — 10s timeout per attempt
# ---------------------------------------------------------------------------


# @spec QC-CN-003
def test_client_default_timeout_is_per_attempt():
    client = QuineClient(base_url="http://localhost:8080")
    assert client.timeout_per_attempt == 10.0


# ---------------------------------------------------------------------------
# QC-CN-004 — ping returns True/False without raising
# ---------------------------------------------------------------------------


# @spec QC-CN-004
@pytest.mark.asyncio
async def test_ping_returns_true_when_reachable():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))
    client = make_client(transport)
    assert await client.ping() is True


# @spec QC-CN-004
@pytest.mark.asyncio
async def test_ping_returns_false_when_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = make_client(httpx.MockTransport(handler))
    assert await client.ping() is False


# @spec QC-CN-004
@pytest.mark.asyncio
async def test_ping_never_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        raise Exception("unexpected")

    client = make_client(httpx.MockTransport(handler))
    result = await client.ping()
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# QC-MP-001 — project_slug in IDs prevents cross-project collision
# ---------------------------------------------------------------------------


# @spec QC-MP-001
@given(
    type_name=st.sampled_from([t for t in ALL_NODE_TYPE_NAMES if t != "customer-issue"]),
    slug_a=slugs,
    slug_b=slugs,
    rest=st.lists(slugs, min_size=1, max_size=3),
)
def test_cross_project_ids_never_collide(type_name, slug_a, slug_b, rest):
    assume(slug_a != slug_b)
    assert idFrom(type_name, slug_a, *rest) != idFrom(type_name, slug_b, *rest)
