"""
Tests for the ci-corroboration-pattern standing query definition
(docs/llds/standing-queries.md § ci_corroboration_pattern.yaml). Written
before implementation (Phase 5) — the YAML definition does not exist yet, so
every test here fails until Phase 6 adds
src/modok/quine/standing_queries/ci_corroboration_pattern.yaml.

Specs verified: SQ-DEF-008, SQ-DEF-009, SQ-DEF-010, SQ-DEF-011.
"""

from __future__ import annotations

from modok.quine.standing_queries.loader import all_definitions, load_definition


# @spec SQ-DEF-008
def test_all_definitions_includes_ci_corroboration_pattern():
    names = [d.name for d in all_definitions()]
    assert "ci-corroboration-pattern" in names


# @spec SQ-DEF-008
def test_ci_corroboration_pattern_uses_distinct_id_mode():
    definition = load_definition("ci-corroboration-pattern")
    assert definition.mode == "DistinctId"


# @spec SQ-DEF-008
def test_ci_corroboration_pattern_matches_shared_error_signature():
    definition = load_definition("ci-corroboration-pattern")
    pattern = definition.pattern
    assert "tf.node_type = 'TestFailure'" in pattern
    assert "e.node_type = 'ErrorSignature'" in pattern
    assert "ci.node_type = 'CustomerIssue'" in pattern
    assert "HAS_ERROR" in pattern


# @spec SQ-DEF-009 — the load-bearing choice: DistinctId keyed on the
# TestFailure, not the CustomerIssue, so every distinct corroborating failure
# fires independently instead of only the first one ever mattering.
def test_ci_corroboration_pattern_distinct_id_keyed_on_test_failure_not_customer_issue():
    definition = load_definition("ci-corroboration-pattern")
    pattern = definition.pattern
    assert "RETURN DISTINCT id(tf) AS id" in pattern
    assert "RETURN DISTINCT id(ci) AS id" not in pattern


# @spec SQ-DEF-010
def test_ci_corroboration_pattern_enrichment_traverses_to_workflow_run():
    definition = load_definition("ci-corroboration-pattern")
    enrichment = definition.enrichment_query
    assert "id(tf) = $that.data.id" in enrichment
    assert "OCCURRED_IN" in enrichment
    assert "RAN_IN" in enrichment
    assert "te.node_type = 'TestExecution'" in enrichment
    assert "wr.node_type = 'WorkflowRun'" in enrichment


# @spec SQ-DEF-010
def test_ci_corroboration_pattern_enrichment_returns_expected_fields():
    definition = load_definition("ci-corroboration-pattern")
    enrichment = definition.enrichment_query
    for field in (
        "project_slug", "source_system", "ticket_id",
        "workflow_run_id", "test_failure_id", "error_signature",
        "milestone_kind", "standing_query_name",
    ):
        assert field in enrichment, f"enrichment_query must return {field}"
    assert "'ci-corroboration-pattern' AS standing_query_name" in enrichment


# @spec SQ-DEF-010
def test_ci_corroboration_pattern_output_name_is_milestone_trigger():
    definition = load_definition("ci-corroboration-pattern")
    assert definition.output_name == "milestone-trigger"


# @spec SQ-DEF-011 — LIMIT, if present, is defensive only; correctness of
# "every qualifying TestFailure produces its own milestone" comes from
# SQ-DEF-009's id(tf) keying, not from row-limiting this query. This test
# only documents that the enrichment doesn't rely on an unbounded row count
# to avoid duplicate PostToEndpoint deliveries for a single tf firing.
def test_ci_corroboration_pattern_enrichment_scoped_to_single_test_failure():
    definition = load_definition("ci-corroboration-pattern")
    enrichment = definition.enrichment_query
    # The enrichment re-matches by the bound tf id — structurally at most one
    # tf per firing, regardless of whether a LIMIT clause is also present.
    assert "MATCH (tf) WHERE id(tf) = $that.data.id" in enrichment
