"""
Metamorphic tests — Layer 3.
All tests written before implementation (Phase 5).
"""
from __future__ import annotations

import pytest

from modok.quine.ids import idFrom
from modok.quine.models import (
    CustomerIssue,
    ErrorSignature,
    Feature,
    File,
    Fix,
    KnownIssue,
    Module,
)

from tests.hifi.harness.runner import run_scenario_from_parts


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _base_nodes_and_edges(project: str = "stagehand"):
    """
    Minimal graph: CI --HAS_ERROR--> Err <--HAS_ERROR-- KI
    Returns (nodes, edges, ci_id).
    """
    ci = CustomerIssue(
        node_type="CustomerIssue", project_slug=project,
        source_system="zendesk", ticket_id="MT001", summary="x", status="open",
    )
    err = ErrorSignature(
        node_type="ErrorSignature", project_slug=project,
        normalized_error="mt_err", display_text="mt_err",
    )
    ki = KnownIssue(
        node_type="KnownIssue", project_slug=project,
        issue_id="KI-MT", summary="mt known issue", status="open",
    )
    ci_id = idFrom("customer-issue", project, "zendesk", "MT001")
    err_id = idFrom("error", project, "mt_err")
    ki_id = idFrom("known-issue", project, "KI-MT")

    nodes = [ci, err, ki]
    edges = [
        (ci_id, "HAS_ERROR", err_id),
        (ki_id, "HAS_ERROR", err_id),
    ]
    return nodes, edges, ci_id


# ---------------------------------------------------------------------------
# Metamorphic Tests
# ---------------------------------------------------------------------------

# @spec MT-001
@pytest.mark.asyncio
async def test_shuffled_input_order_same_packet():
    """Shuffling node and edge order must not change packet IDs."""
    project = "stagehand"
    nodes, edges, ci_id = _base_nodes_and_edges(project)

    _, actual_orig, _ = await run_scenario_from_parts(nodes, edges, ci_id, project)

    shuffled_nodes = list(reversed(nodes))
    shuffled_edges = list(reversed(edges))
    _, actual_shuf, _ = await run_scenario_from_parts(shuffled_nodes, shuffled_edges, ci_id, project)

    assert set(k.id for k in actual_orig.known_issues) == \
           set(k.id for k in actual_shuf.known_issues)
    assert set(f.id for f in actual_orig.prior_fixes) == \
           set(f.id for f in actual_shuf.prior_fixes)
    assert set(actual_orig.relevant_files) == set(actual_shuf.relevant_files)


# @spec MT-002
@pytest.mark.asyncio
async def test_duplicate_node_edge_same_packet():
    """Adding a duplicate node and edge entry must not change the packet."""
    project = "stagehand"
    nodes, edges, ci_id = _base_nodes_and_edges(project)

    _, actual_orig, _ = await run_scenario_from_parts(nodes, edges, ci_id, project)

    dup_nodes = nodes + [nodes[0]]
    dup_edges = edges + [edges[0]]
    _, actual_dup, _ = await run_scenario_from_parts(dup_nodes, dup_edges, ci_id, project)

    assert set(k.id for k in actual_orig.known_issues) == \
           set(k.id for k in actual_dup.known_issues)
    assert set(f.id for f in actual_orig.prior_fixes) == \
           set(f.id for f in actual_dup.prior_fixes)
    assert set(actual_orig.relevant_files) == set(actual_dup.relevant_files)


# @spec MT-003
@pytest.mark.asyncio
async def test_unrelated_project_node_does_not_change_packet():
    """Adding nodes and edges from an unrelated project must not change the packet."""
    project = "stagehand"
    other = "other-project"
    nodes, edges, ci_id = _base_nodes_and_edges(project)

    _, actual_orig, _ = await run_scenario_from_parts(nodes, edges, ci_id, project)

    # Add an unrelated project's CI, error, and KI with their own edges
    other_ci = CustomerIssue(
        node_type="CustomerIssue", project_slug=other,
        source_system="zendesk", ticket_id="MT003", summary="other", status="open",
    )
    other_err = ErrorSignature(
        node_type="ErrorSignature", project_slug=other,
        normalized_error="other_err", display_text="other_err",
    )
    other_ki = KnownIssue(
        node_type="KnownIssue", project_slug=other,
        issue_id="KI-OTHER", summary="other ki", status="open",
    )
    other_ci_id = idFrom("customer-issue", other, "zendesk", "MT003")
    other_err_id = idFrom("error", other, "other_err")
    other_ki_id = idFrom("known-issue", other, "KI-OTHER")

    extended_nodes = nodes + [other_ci, other_err, other_ki]
    extended_edges = edges + [
        (other_ci_id, "HAS_ERROR", other_err_id),
        (other_ki_id, "HAS_ERROR", other_err_id),
    ]

    _, actual_ext, _ = await run_scenario_from_parts(extended_nodes, extended_edges, ci_id, project)

    assert set(k.id for k in actual_orig.known_issues) == \
           set(k.id for k in actual_ext.known_issues)
    assert set(f.id for f in actual_orig.prior_fixes) == \
           set(f.id for f in actual_ext.prior_fixes)
    assert set(actual_orig.relevant_files) == set(actual_ext.relevant_files)


# @spec MT-004
@pytest.mark.asyncio
async def test_adding_fix_appears_in_prior_fixes():
    """Adding a Fix with a RESOLVED_BY edge must cause fix_id to appear in prior_fixes."""
    project = "stagehand"
    nodes, edges, ci_id = _base_nodes_and_edges(project)

    _, actual_orig, _ = await run_scenario_from_parts(nodes, edges, ci_id, project)
    assert "FIX-MT" not in [f.id for f in actual_orig.prior_fixes]

    fix = Fix(
        node_type="Fix", project_slug=project,
        fix_id="FIX-MT", summary="mt fix", kind="patch",
    )
    ki_id = idFrom("known-issue", project, "KI-MT")
    fix_id = idFrom("fix", project, "FIX-MT")

    extended_nodes = nodes + [fix]
    extended_edges = edges + [(ki_id, "RESOLVED_BY", fix_id)]

    _, actual_ext, _ = await run_scenario_from_parts(extended_nodes, extended_edges, ci_id, project)
    assert "FIX-MT" in [f.id for f in actual_ext.prior_fixes]


# @spec MT-005
@pytest.mark.asyncio
async def test_adding_affects_edge_brings_in_files():
    """Adding an AFFECTS edge to a new Feature must cause its files to appear in relevant_files."""
    project = "stagehand"
    nodes, edges, ci_id = _base_nodes_and_edges(project)

    _, actual_orig, _ = await run_scenario_from_parts(nodes, edges, ci_id, project)
    assert "agent/src/mt_feature.c" not in actual_orig.relevant_files

    new_feat = Feature(
        node_type="Feature", project_slug=project,
        feature_slug="mt-feature", name="MT Feature",
    )
    new_mod = Module(
        node_type="Module", project_slug=project,
        module_slug="mt-mod", name="MT Module",
    )
    new_file = File(
        node_type="File", project_slug=project,
        repo_path="agent/src/mt_feature.c",
    )
    feat_id = idFrom("feature", project, "mt-feature")
    mod_id = idFrom("module", project, "mt-mod")
    file_id = idFrom("file", project, "agent/src/mt_feature.c")

    extended_nodes = nodes + [new_feat, new_mod, new_file]
    extended_edges = edges + [
        (ci_id, "AFFECTS", feat_id),
        (feat_id, "IMPLEMENTED_BY", mod_id),
        (mod_id, "DEFINED_IN", file_id),
    ]

    _, actual_ext, _ = await run_scenario_from_parts(extended_nodes, extended_edges, ci_id, project)
    assert "agent/src/mt_feature.c" in actual_ext.relevant_files
