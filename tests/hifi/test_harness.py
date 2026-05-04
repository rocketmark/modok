"""
Tests for the HiFi harness — loader, runner, and comparison utilities.
All tests written before implementation (Phase 5).
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from modok.quine.ids import idFrom
from modok.retrieval.models import (
    AffectedArea,
    DebugPacket,
    IssueAnchors,
    IssueSummary,
    KnownIssueRef,
    PriorFix,
)

from tests.hifi.dummy_quine.client import DummyQuine
from tests.hifi.harness.loader import load_scenario
from tests.hifi.harness.runner import run_scenario
from tests.hifi.harness.compare import assert_packets_equivalent


# ---------------------------------------------------------------------------
# Harness — Scenario Loading
# ---------------------------------------------------------------------------

# @spec HFI-LOAD-001
def test_load_scenario_parses_yaml(tmp_path):
    yaml = textwrap.dedent("""\
        name: simple

        nodes:
          - type: CustomerIssue
            project_slug: stagehand
            source_system: zendesk
            ticket_id: "1001"
            summary: tracker dropout
            status: open

        edges:
          []

        query:
          issue: [CustomerIssue, stagehand, zendesk, "1001"]
          project_slug: stagehand

        expected:
          known_issues:
            must_include: []
          recent_fixes:
            must_include: []
    """)
    f = tmp_path / "simple.yaml"
    f.write_text(yaml)
    scenario = load_scenario(f)
    assert scenario.name == "simple"
    assert len(scenario.nodes) == 1
    assert scenario.query.project_slug == "stagehand"


# @spec HFI-LOAD-002
def test_load_scenario_computes_integer_node_ids(tmp_path):
    yaml = textwrap.dedent("""\
        name: id_check

        nodes:
          - type: Feature
            project_slug: stagehand
            feature_slug: shtp
            name: SHTP

        edges: []

        query:
          issue: [CustomerIssue, stagehand, zendesk, "1001"]
          project_slug: stagehand

        expected:
          known_issues:
            must_include: []
          recent_fixes:
            must_include: []
    """)
    f = tmp_path / "id_check.yaml"
    f.write_text(yaml)
    scenario = load_scenario(f)
    expected_id = idFrom("feature", "stagehand", "shtp")
    node_ids = [n.node_id for n in scenario.nodes]
    assert expected_id in node_ids


# @spec HFI-LOAD-003
def test_load_scenario_edge_refs_match_node_ids(tmp_path):
    yaml = textwrap.dedent("""\
        name: edge_ref

        nodes:
          - type: CustomerIssue
            project_slug: stagehand
            source_system: zendesk
            ticket_id: "1001"
            summary: x
            status: open

          - type: Feature
            project_slug: stagehand
            feature_slug: shtp
            name: SHTP

        edges:
          - from: [CustomerIssue, stagehand, zendesk, "1001"]
            type: AFFECTS
            to: [Feature, stagehand, shtp]

        query:
          issue: [CustomerIssue, stagehand, zendesk, "1001"]
          project_slug: stagehand

        expected:
          known_issues:
            must_include: []
          recent_fixes:
            must_include: []
    """)
    f = tmp_path / "edge_ref.yaml"
    f.write_text(yaml)
    scenario = load_scenario(f)
    ci_id = idFrom("customer-issue", "stagehand", "zendesk", "1001")
    feat_id = idFrom("feature", "stagehand", "shtp")
    assert len(scenario.edges) == 1
    edge = scenario.edges[0]
    assert edge.from_id == ci_id
    assert edge.to_id == feat_id


# ---------------------------------------------------------------------------
# Harness — Scenario Execution
# ---------------------------------------------------------------------------

# @spec HFI-RUN-001
@pytest.mark.asyncio
async def test_run_scenario_constructs_fresh_dummy_quine(tmp_path):
    yaml = textwrap.dedent("""\
        name: isolation

        nodes:
          - type: CustomerIssue
            project_slug: stagehand
            source_system: zendesk
            ticket_id: "9001"
            summary: x
            status: open

        edges: []

        query:
          issue: [CustomerIssue, stagehand, zendesk, "9001"]
          project_slug: stagehand

        expected:
          known_issues:
            must_include: []
          recent_fixes:
            must_include: []
    """)
    f = tmp_path / "isolation.yaml"
    f.write_text(yaml)
    scenario = load_scenario(f)
    # run_scenario should not accept a DummyQuine argument
    import inspect
    sig = inspect.signature(run_scenario)
    assert "dummy_quine" not in sig.parameters


# @spec HFI-RUN-002
@pytest.mark.asyncio
async def test_run_scenario_writes_nodes_before_edges(tmp_path, monkeypatch):
    """Verify that when run_scenario writes to DummyQuine, nodes are present
    before any edge write is attempted."""
    yaml = textwrap.dedent("""\
        name: order_check

        nodes:
          - type: CustomerIssue
            project_slug: stagehand
            source_system: zendesk
            ticket_id: "2001"
            summary: x
            status: open

          - type: Feature
            project_slug: stagehand
            feature_slug: shtp
            name: SHTP

        edges:
          - from: [CustomerIssue, stagehand, zendesk, "2001"]
            type: AFFECTS
            to: [Feature, stagehand, shtp]

        query:
          issue: [CustomerIssue, stagehand, zendesk, "2001"]
          project_slug: stagehand

        expected:
          known_issues:
            must_include: []
          recent_fixes:
            must_include: []
    """)
    f = tmp_path / "order_check.yaml"
    f.write_text(yaml)
    scenario = load_scenario(f)

    write_order: list[str] = []
    original_upsert = DummyQuine.upsert_node
    original_write_edge = DummyQuine.write_edge

    async def tracking_upsert(self, node):
        write_order.append("node")
        return await original_upsert(self, node)

    async def tracking_write_edge(self, from_id, edge_type, to_id):
        write_order.append("edge")
        return await original_write_edge(self, from_id, edge_type, to_id)

    monkeypatch.setattr(DummyQuine, "upsert_node", tracking_upsert)
    monkeypatch.setattr(DummyQuine, "write_edge", tracking_write_edge)

    await run_scenario(scenario)

    # All node writes must precede any edge write
    last_node = max((i for i, v in enumerate(write_order) if v == "node"), default=-1)
    first_edge = min((i for i, v in enumerate(write_order) if v == "edge"), default=len(write_order))
    assert last_node < first_edge


# @spec HFI-RUN-003
@pytest.mark.asyncio
async def test_run_scenario_uses_precomputed_ids(tmp_path):
    """Both actual and expected packets should reflect the same nodes,
    implying the runner passes precomputed IDs rather than recomputing them."""
    yaml = textwrap.dedent("""\
        name: id_pass

        nodes:
          - type: CustomerIssue
            project_slug: stagehand
            source_system: zendesk
            ticket_id: "3001"
            summary: x
            status: open

          - type: ErrorSignature
            project_slug: stagehand
            normalized_error: shtp_mismatch
            display_text: shtp_mismatch

          - type: KnownIssue
            project_slug: stagehand
            issue_id: KI-001
            summary: SHTP mismatch
            status: open

        edges:
          - from: [CustomerIssue, stagehand, zendesk, "3001"]
            type: HAS_ERROR
            to: [ErrorSignature, stagehand, shtp_mismatch]

          - from: [KnownIssue, stagehand, KI-001]
            type: HAS_ERROR
            to: [ErrorSignature, stagehand, shtp_mismatch]

        query:
          issue: [CustomerIssue, stagehand, zendesk, "3001"]
          project_slug: stagehand

        expected:
          known_issues:
            must_include: [KI-001]
          recent_fixes:
            must_include: []
    """)
    f = tmp_path / "id_pass.yaml"
    f.write_text(yaml)
    scenario = load_scenario(f)
    expected_packet, actual_packet = await run_scenario(scenario)
    # Both systems should agree on KI-001 being present
    assert_packets_equivalent(expected_packet, actual_packet)


# @spec HFI-RUN-004
@pytest.mark.asyncio
async def test_run_scenario_returns_actual_packet(tmp_path):
    yaml = textwrap.dedent("""\
        name: returns_actual

        nodes:
          - type: CustomerIssue
            project_slug: stagehand
            source_system: zendesk
            ticket_id: "4001"
            summary: x
            status: open

        edges: []

        query:
          issue: [CustomerIssue, stagehand, zendesk, "4001"]
          project_slug: stagehand

        expected:
          known_issues:
            must_include: []
          recent_fixes:
            must_include: []
    """)
    f = tmp_path / "returns_actual.yaml"
    f.write_text(yaml)
    scenario = load_scenario(f)
    expected_packet, actual_packet = await run_scenario(scenario)
    assert isinstance(actual_packet, DebugPacket)


# @spec HFI-RUN-005
@pytest.mark.asyncio
async def test_run_scenario_returns_expected_packet(tmp_path):
    yaml = textwrap.dedent("""\
        name: returns_expected

        nodes:
          - type: CustomerIssue
            project_slug: stagehand
            source_system: zendesk
            ticket_id: "5001"
            summary: x
            status: open

        edges: []

        query:
          issue: [CustomerIssue, stagehand, zendesk, "5001"]
          project_slug: stagehand

        expected:
          known_issues:
            must_include: []
          recent_fixes:
            must_include: []
    """)
    f = tmp_path / "returns_expected.yaml"
    f.write_text(yaml)
    scenario = load_scenario(f)
    expected_packet, actual_packet = await run_scenario(scenario)
    assert isinstance(expected_packet, DebugPacket)


# @spec HFI-RUN-006
@pytest.mark.asyncio
async def test_run_scenario_isolates_state_between_runs(tmp_path):
    yaml = textwrap.dedent("""\
        name: isolation2

        nodes:
          - type: CustomerIssue
            project_slug: stagehand
            source_system: zendesk
            ticket_id: "6001"
            summary: x
            status: open

          - type: KnownIssue
            project_slug: stagehand
            issue_id: KI-ISOLATED
            summary: only in run 1
            status: open

          - type: ErrorSignature
            project_slug: stagehand
            normalized_error: iso_err
            display_text: iso_err

        edges:
          - from: [CustomerIssue, stagehand, zendesk, "6001"]
            type: HAS_ERROR
            to: [ErrorSignature, stagehand, iso_err]

          - from: [KnownIssue, stagehand, KI-ISOLATED]
            type: HAS_ERROR
            to: [ErrorSignature, stagehand, iso_err]

        query:
          issue: [CustomerIssue, stagehand, zendesk, "6001"]
          project_slug: stagehand

        expected:
          known_issues:
            must_include: [KI-ISOLATED]
          recent_fixes:
            must_include: []
    """)
    yaml2 = textwrap.dedent("""\
        name: isolation2b

        nodes:
          - type: CustomerIssue
            project_slug: stagehand
            source_system: zendesk
            ticket_id: "6002"
            summary: x
            status: open

        edges: []

        query:
          issue: [CustomerIssue, stagehand, zendesk, "6002"]
          project_slug: stagehand

        expected:
          known_issues:
            must_include: []
          recent_fixes:
            must_include: []
    """)
    f1 = tmp_path / "isolation2.yaml"
    f2 = tmp_path / "isolation2b.yaml"
    f1.write_text(yaml)
    f2.write_text(yaml2)
    # Run first scenario (has KI-ISOLATED)
    await run_scenario(load_scenario(f1))
    # Run second scenario (empty) — KI-ISOLATED must not bleed in
    _, actual2 = await run_scenario(load_scenario(f2))
    ki_ids = [k.id for k in actual2.known_issues]
    assert "KI-ISOLATED" not in ki_ids


# ---------------------------------------------------------------------------
# Harness — Comparison
# ---------------------------------------------------------------------------

# @spec HFI-CMP-001
def test_assert_packets_equivalent_passes_when_ids_match():
    def _make(ki_ids, fix_ids, paths):
        return DebugPacket(
            issue=IssueSummary(summary="x", anchors=IssueAnchors()),
            affected_areas=[],
            relevant_files=paths,
            relevant_tests=[],
            known_issues=[KnownIssueRef(id=k, summary="") for k in ki_ids],
            prior_fixes=[PriorFix(id=f, commit="", summary="") for f in fix_ids],
        )

    expected = _make(["KI-001", "KI-002"], ["FIX-001"], ["agent/src/shtp.c"])
    # actual has a superset — equivalent because expected items are all present
    actual = _make(["KI-001", "KI-002", "KI-003"], ["FIX-001"], ["agent/src/shtp.c", "extra.c"])
    assert_packets_equivalent(expected, actual)  # should not raise


def test_assert_packets_equivalent_fails_when_ki_missing():
    def _make(ki_ids):
        return DebugPacket(
            issue=IssueSummary(summary="x", anchors=IssueAnchors()),
            affected_areas=[],
            relevant_files=[],
            relevant_tests=[],
            known_issues=[KnownIssueRef(id=k, summary="") for k in ki_ids],
            prior_fixes=[],
        )

    expected = _make(["KI-001"])
    actual = _make(["KI-002"])
    with pytest.raises(AssertionError):
        assert_packets_equivalent(expected, actual)
