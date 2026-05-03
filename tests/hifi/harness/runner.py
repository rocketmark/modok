"""
Scenario runner — executes a Scenario against both real MODOK (via DummyQuine)
and the ReferenceModok, returning both DebugPackets.
"""
from __future__ import annotations

from modok.retrieval.engine import retrieve
from modok.retrieval.errors import DREAnchorError
from modok.retrieval.models import AnchorSet, DebugPacket

from tests.hifi.dummy_quine.client import DummyQuine
from tests.hifi.harness.loader import Scenario
from tests.hifi.reference.model import ReferenceModok


def _empty_packet(summary: str) -> DebugPacket:
    return DebugPacket(
        issue_summary=summary,
        anchors=AnchorSet(),
        anchor_count=0,
        known_issues=[],
        recent_fixes=[],
        relevant_files=[],
        recent_commits=[],
        evidence=[],
        confidence=0.0,
    )


# @spec HFI-RUN-001, HFI-RUN-002, HFI-RUN-003, HFI-RUN-004, HFI-RUN-005, HFI-RUN-006
async def run_scenario(scenario: Scenario) -> tuple[DebugPacket, DebugPacket]:
    """
    Returns (expected_packet, actual_packet).

    expected — produced by ReferenceModok (no LLM, no HTTP).
    actual   — produced by the real DRE retrieve() via a fresh DummyQuine.
    """
    dq = DummyQuine()
    ref = ReferenceModok()

    # Write all nodes before any edges (HFI-RUN-002)
    for rec in scenario.nodes:
        await dq.upsert_node(rec.node)

    raw_edges = [(e.from_id, e.edge_type, e.to_id) for e in scenario.edges]

    for e in scenario.edges:
        await dq.write_edge(e.from_id, e.edge_type, e.to_id)

    # Reference model receives precomputed IDs (HFI-RUN-003)
    ref.ingest([rec.node for rec in scenario.nodes], raw_edges)

    expected_packet = ref.retrieve(scenario.query.issue_id, scenario.query.project_slug)
    try:
        actual_packet = await retrieve(scenario.query.issue_id, scenario.query.project_slug, dq)
    except DREAnchorError:
        actual_packet = _empty_packet(expected_packet.issue_summary)

    return expected_packet, actual_packet


async def run_scenario_from_parts(
    nodes: list,
    edges: list[tuple[int, str, int]],
    issue_id: int,
    project_slug: str,
) -> tuple[DebugPacket, DebugPacket, DummyQuine]:
    """
    Like run_scenario but accepts raw parts instead of a Scenario object.
    Returns (expected_packet, actual_packet, dq) so callers can inspect dq._nodes.
    """
    dq = DummyQuine()
    ref = ReferenceModok()

    for node in nodes:
        await dq.upsert_node(node)

    seen: set[tuple[int, str, int]] = set()
    for edge in edges:
        if edge not in seen:
            await dq.write_edge(*edge)
            seen.add(edge)

    ref.ingest(nodes, list(set(edges)))

    expected_packet = ref.retrieve(issue_id, project_slug)
    try:
        actual_packet = await retrieve(issue_id, project_slug, dq)
    except DREAnchorError:
        actual_packet = _empty_packet(expected_packet.issue_summary)

    return expected_packet, actual_packet, dq
