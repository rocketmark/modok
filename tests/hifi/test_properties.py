"""
Property tests — Layer 2.
All tests written before implementation (Phase 5).
"""
from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from modok.quine.ids import idFrom
from modok.quine.models import Feature, KnownIssue, CustomerIssue, ErrorSignature

from tests.hifi.dummy_quine.client import DummyQuine
from tests.hifi.harness.runner import run_scenario_from_parts


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

slugs = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=20)
projects = st.sampled_from(["stagehand", "other-proj"])


@st.composite
def feature_node(draw):
    project = draw(projects)
    slug = draw(slugs)
    return Feature(node_type="Feature", project_slug=project, feature_slug=slug, name=slug)


@st.composite
def known_issue_node(draw):
    project = draw(projects)
    ki_id = draw(slugs)
    return KnownIssue(
        node_type="KnownIssue", project_slug=project,
        issue_id=ki_id, summary="test", status="open",
    )


# ---------------------------------------------------------------------------
# Property Tests
# ---------------------------------------------------------------------------

# @spec PT-001
@given(st.lists(feature_node(), min_size=1, max_size=10, unique_by=lambda f: (f.project_slug, f.feature_slug)))
@settings(max_examples=50)
@pytest.mark.asyncio
async def test_node_exists_after_upsert(features):
    dq = DummyQuine()
    for feat in features:
        await dq.upsert_node(feat)
    for feat in features:
        node_id = idFrom("feature", feat.project_slug, feat.feature_slug)
        assert await dq.node_exists(node_id)


# @spec PT-002
@given(
    st.lists(feature_node(), min_size=1, max_size=5, unique_by=lambda f: (f.project_slug, f.feature_slug)),
    st.lists(st.tuples(st.integers(1, 100), st.just("IMPLEMENTS"), st.integers(1, 100)), min_size=0, max_size=5),
)
@settings(max_examples=50)
@pytest.mark.asyncio
async def test_double_write_is_idempotent(features, edges):
    dq = DummyQuine()
    # Write once
    for feat in features:
        await dq.upsert_node(feat)
    for from_id, etype, to_id in edges:
        await dq.write_edge(from_id, etype, to_id)
    nodes_after_first = dict(dq._nodes)
    edges_after_first = list(dq._edges)

    # Write again
    for feat in features:
        await dq.upsert_node(feat)
    for from_id, etype, to_id in edges:
        await dq.write_edge(from_id, etype, to_id)

    assert set(dq._nodes.keys()) == set(nodes_after_first.keys())
    assert sorted(dq._edges) == sorted(edges_after_first)


# @spec PT-003
@pytest.mark.asyncio
async def test_packet_ki_ids_exist_in_dummy_quine():
    """Every known_issue_id in the packet must resolve to a node in DummyQuine."""
    from modok.quine.models import ErrorSignature
    from tests.hifi.harness.runner import run_scenario_from_parts

    project = "stagehand"
    ci = CustomerIssue(
        node_type="CustomerIssue", project_slug=project,
        source_system="zendesk", ticket_id="PT003", summary="x", status="open",
    )
    err = ErrorSignature(
        node_type="ErrorSignature", project_slug=project,
        normalized_error="pt003_err", display_text="pt003_err",
    )
    ki = KnownIssue(
        node_type="KnownIssue", project_slug=project,
        issue_id="KI-PT003", summary="test", status="open",
    )
    ci_id = idFrom("customer-issue", project, "zendesk", "PT003")
    err_id = idFrom("error", project, "pt003_err")
    ki_id = idFrom("known-issue", project, "KI-PT003")

    nodes = [ci, err, ki]
    edges = [
        (ci_id, "HAS_ERROR", err_id),
        (ki_id, "HAS_ERROR", err_id),
    ]

    _, actual, dq = await run_scenario_from_parts(nodes, edges, ci_id, project)

    for ki_ref in actual.known_issues:
        expected_node_id = idFrom("known-issue", project, ki_ref.known_issue_id)
        assert expected_node_id in dq._nodes, \
            f"known_issue_id={ki_ref.known_issue_id!r} not in DummyQuine._nodes"


# @spec PT-004
@pytest.mark.asyncio
async def test_packet_contains_only_queried_project_nodes():
    """No cross-project node should appear in a packet queried for project-a."""
    from modok.quine.models import ErrorSignature
    from tests.hifi.harness.runner import run_scenario_from_parts

    proj_a = "project-a"
    proj_b = "project-b"

    ci_a = CustomerIssue(
        node_type="CustomerIssue", project_slug=proj_a,
        source_system="zendesk", ticket_id="PT004", summary="x", status="open",
    )
    err_a = ErrorSignature(
        node_type="ErrorSignature", project_slug=proj_a,
        normalized_error="shared_err", display_text="shared_err",
    )
    ki_a = KnownIssue(
        node_type="KnownIssue", project_slug=proj_a,
        issue_id="KI-A", summary="from a", status="open",
    )
    ki_b = KnownIssue(
        node_type="KnownIssue", project_slug=proj_b,
        issue_id="KI-B", summary="from b", status="open",
    )
    err_b = ErrorSignature(
        node_type="ErrorSignature", project_slug=proj_b,
        normalized_error="shared_err", display_text="shared_err",
    )

    ci_a_id = idFrom("customer-issue", proj_a, "zendesk", "PT004")
    err_a_id = idFrom("error", proj_a, "shared_err")
    err_b_id = idFrom("error", proj_b, "shared_err")
    ki_a_id = idFrom("known-issue", proj_a, "KI-A")
    ki_b_id = idFrom("known-issue", proj_b, "KI-B")

    nodes = [ci_a, err_a, err_b, ki_a, ki_b]
    edges = [
        (ci_a_id, "HAS_ERROR", err_a_id),
        (ki_a_id, "HAS_ERROR", err_a_id),
        (ki_b_id, "HAS_ERROR", err_b_id),
    ]

    _, actual, dq = await run_scenario_from_parts(nodes, edges, ci_a_id, proj_a)

    for ki_ref in actual.known_issues:
        node_id = idFrom("known-issue", proj_a, ki_ref.known_issue_id)
        node = dq._nodes.get(node_id)
        assert node is not None
        assert node.project_slug == proj_a, \
            f"Expected project_slug={proj_a!r}, got {node.project_slug!r} for KI={ki_ref.known_issue_id}"
