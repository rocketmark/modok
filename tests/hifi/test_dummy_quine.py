"""
Tests for DummyQuine — the in-memory QuineClient replacement.
All tests written before implementation (Phase 5).
"""

from __future__ import annotations


import pytest

from modok.quine.errors import QuineNodeNotFoundError
from modok.quine.ids import idFrom
from modok.quine.models import (
    CustomerIssue,
    ErrorSignature,
    Feature,
    File,
    Fix,
    KnownIssue,
    Module,
    SimilarityMatch,
)

from tests.hifi.dummy_quine.client import DummyQuine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_feature(slug: str = "shtp", project: str = "stagehand") -> Feature:
    return Feature(node_type="Feature", project_slug=project, feature_slug=slug, name=slug.upper())


def make_module(slug: str = "shtp-mod", project: str = "stagehand") -> Module:
    return Module(node_type="Module", project_slug=project, module_slug=slug, name=slug)


def make_file(path: str = "agent/src/shtp.c", project: str = "stagehand") -> File:
    return File(node_type="File", project_slug=project, repo_path=path)


def make_customer_issue(ticket: str = "1001", project: str = "stagehand") -> CustomerIssue:
    return CustomerIssue(
        node_type="CustomerIssue",
        project_slug=project,
        source_system="zendesk",
        ticket_id=ticket,
        summary="tracker dropout",
        status="open",
    )


def make_error_sig(err: str = "shtp_mismatch", project: str = "stagehand") -> ErrorSignature:
    return ErrorSignature(
        node_type="ErrorSignature",
        project_slug=project,
        normalized_error=err,
        display_text=err,
    )


def make_known_issue(ki_id: str = "KI-001", project: str = "stagehand") -> KnownIssue:
    return KnownIssue(
        node_type="KnownIssue",
        project_slug=project,
        issue_id=ki_id,
        summary="SHTP version mismatch",
        status="open",
    )


def make_fix(fix_id: str = "FIX-001", project: str = "stagehand") -> Fix:
    return Fix(
        node_type="Fix",
        project_slug=project,
        fix_id=fix_id,
        summary="Upgrade to SHTP v2",
        kind="patch",
    )


def make_similarity_match(
    ci_id: str, ki_id: str, status: str = "candidate", project: str = "stagehand"
) -> SimilarityMatch:
    return SimilarityMatch(
        node_type="SimilarityMatch",
        project_slug=project,
        customer_issue_id=str(ci_id),
        known_issue_id=str(ki_id),
        method="graph-anchor",
        score=0.8,
        evidence_anchors=[],
        review_status=status,
    )


# ---------------------------------------------------------------------------
# DummyQuine — Node Writes
# ---------------------------------------------------------------------------


# @spec DQ-NW-001
@pytest.mark.asyncio
async def test_upsert_node_stores_new_node():
    dq = DummyQuine()
    feat = make_feature()
    await dq.upsert_node(feat)
    node_id = idFrom("feature", "stagehand", "shtp")
    assert node_id in dq._nodes
    assert dq._nodes[node_id] == feat


# @spec DQ-NW-002
@pytest.mark.asyncio
async def test_upsert_node_overwrites_existing():
    dq = DummyQuine()
    feat1 = make_feature(slug="shtp")
    feat2 = Feature(
        node_type="Feature", project_slug="stagehand", feature_slug="shtp", name="UPDATED"
    )
    await dq.upsert_node(feat1)
    await dq.upsert_node(feat2)
    node_id = idFrom("feature", "stagehand", "shtp")
    assert dq._nodes[node_id].name == "UPDATED"


# ---------------------------------------------------------------------------
# DummyQuine — Node Reads
# ---------------------------------------------------------------------------


# @spec DQ-NR-001
@pytest.mark.asyncio
async def test_get_node_returns_stored_node():
    dq = DummyQuine()
    feat = make_feature()
    await dq.upsert_node(feat)
    node_id = idFrom("feature", "stagehand", "shtp")
    result = await dq.get_node(node_id, Feature)
    assert result == feat


# @spec DQ-NR-002
@pytest.mark.asyncio
async def test_get_node_missing_raises():
    dq = DummyQuine()
    with pytest.raises(QuineNodeNotFoundError):
        await dq.get_node(99999999, Feature)


# @spec DQ-NR-003
@pytest.mark.asyncio
async def test_node_exists_true_when_present():
    dq = DummyQuine()
    feat = make_feature()
    await dq.upsert_node(feat)
    node_id = idFrom("feature", "stagehand", "shtp")
    assert await dq.node_exists(node_id) is True


# @spec DQ-NR-004
@pytest.mark.asyncio
async def test_node_exists_false_when_absent():
    dq = DummyQuine()
    assert await dq.node_exists(99999999) is False


# ---------------------------------------------------------------------------
# DummyQuine — Edge Writes
# ---------------------------------------------------------------------------


# @spec DQ-EW-001
@pytest.mark.asyncio
async def test_write_edge_appends_new_edge():
    dq = DummyQuine()
    await dq.write_edge(1, "AFFECTS", 2)
    assert (1, "AFFECTS", 2) in dq._edges


# @spec DQ-EW-002
@pytest.mark.asyncio
async def test_write_edge_no_duplicate():
    dq = DummyQuine()
    await dq.write_edge(1, "AFFECTS", 2)
    await dq.write_edge(1, "AFFECTS", 2)
    assert dq._edges.count((1, "AFFECTS", 2)) == 1


# @spec DQ-EW-003
@pytest.mark.asyncio
async def test_replace_edges_removes_and_recreates():
    dq = DummyQuine()
    await dq.write_edge(1, "AFFECTS", 2)
    await dq.write_edge(1, "AFFECTS", 3)
    await dq.replace_edges(1, "AFFECTS", [4, 5])
    assert (1, "AFFECTS", 2) not in dq._edges
    assert (1, "AFFECTS", 3) not in dq._edges
    assert (1, "AFFECTS", 4) in dq._edges
    assert (1, "AFFECTS", 5) in dq._edges


# @spec DQ-EW-004
@pytest.mark.asyncio
async def test_replace_edges_empty_to_ids_removes_all():
    dq = DummyQuine()
    await dq.write_edge(1, "AFFECTS", 2)
    await dq.write_edge(1, "AFFECTS", 3)
    await dq.replace_edges(1, "AFFECTS", [])
    assert all(e[0] != 1 or e[1] != "AFFECTS" for e in dq._edges)


# @spec DQ-EW-005
@pytest.mark.asyncio
async def test_edge_exists_true_when_present():
    dq = DummyQuine()
    await dq.write_edge(1, "AFFECTS", 2)
    assert await dq.edge_exists(1, "AFFECTS", 2) is True


# @spec DQ-EW-006
@pytest.mark.asyncio
async def test_edge_exists_false_when_absent():
    dq = DummyQuine()
    assert await dq.edge_exists(1, "AFFECTS", 2) is False


# ---------------------------------------------------------------------------
# DummyQuine — Query Dispatch
# ---------------------------------------------------------------------------


# @spec DQ-QD-001
@pytest.mark.asyncio
async def test_query_affects_feature_returns_feature_rows():
    dq = DummyQuine()
    ci = make_customer_issue("1001")
    feat = make_feature("shtp")
    ci_id = idFrom("customer-issue", "stagehand", "zendesk", "1001")
    feat_id = idFrom("feature", "stagehand", "shtp")
    await dq.upsert_node(ci)
    await dq.upsert_node(feat)
    await dq.write_edge(ci_id, "AFFECTS", feat_id)

    rows = await dq.query(
        "MATCH (ci:CustomerIssue) WHERE id(ci) = $issue_id "
        "MATCH (ci)-[:AFFECTS]->(f:Feature {project_slug: $project_slug}) "
        "RETURN f.feature_slug",
        {"issue_id": ci_id, "project_slug": "stagehand"},
    )
    assert len(rows) == 1
    assert rows[0][0]["properties"]["feature_slug"] == "shtp"


# @spec DQ-QD-002
@pytest.mark.asyncio
async def test_query_has_error_returns_error_sig_rows():
    dq = DummyQuine()
    ci = make_customer_issue("1001")
    err = make_error_sig("shtp_mismatch")
    ci_id = idFrom("customer-issue", "stagehand", "zendesk", "1001")
    err_id = idFrom("error", "stagehand", "shtp_mismatch")
    await dq.upsert_node(ci)
    await dq.upsert_node(err)
    await dq.write_edge(ci_id, "HAS_ERROR", err_id)

    rows = await dq.query(
        "MATCH (ci:CustomerIssue) WHERE id(ci) = $issue_id "
        "MATCH (ci)-[:HAS_ERROR]->(e:ErrorSignature {project_slug: $project_slug}) "
        "RETURN e.normalized_error",
        {"issue_id": ci_id, "project_slug": "stagehand"},
    )
    assert len(rows) == 1
    assert rows[0][0]["properties"]["normalized_error"] == "shtp_mismatch"


# @spec DQ-QD-003
@pytest.mark.asyncio
async def test_query_implemented_by_returns_file_rows():
    dq = DummyQuine()
    feat = make_feature("shtp")
    mod = make_module("shtp-mod")
    fil = make_file("agent/src/shtp.c")
    feat_id = idFrom("feature", "stagehand", "shtp")
    mod_id = idFrom("module", "stagehand", "shtp-mod")
    fil_id = idFrom("file", "stagehand", "agent/src/shtp.c")
    await dq.upsert_node(feat)
    await dq.upsert_node(mod)
    await dq.upsert_node(fil)
    await dq.write_edge(feat_id, "IMPLEMENTED_BY", mod_id)
    await dq.write_edge(mod_id, "DEFINED_IN", fil_id)

    rows = await dq.query(
        "MATCH (f) WHERE id(f) = idFrom('feature', $project_slug, $feature_slug) "
        "OPTIONAL MATCH (f)-[:IMPLEMENTED_BY]->(m) "
        "OPTIONAL MATCH (m)-[:DEFINED_IN]->(file) "
        "RETURN f, m, file",
        {"project_slug": "stagehand", "feature_slug": "shtp"},
    )
    assert len(rows) == 1
    # Row format: [feature_dict, module_dict, file_dict]
    assert rows[0][2]["properties"]["repo_path"] == "agent/src/shtp.c"


# @spec DQ-QD-004
@pytest.mark.asyncio
async def test_query_known_issue_reverse_walk():
    dq = DummyQuine()
    err = make_error_sig("shtp_mismatch")
    ki = make_known_issue("KI-001")
    err_id = idFrom("error", "stagehand", "shtp_mismatch")
    ki_id = idFrom("known-issue", "stagehand", "KI-001")
    await dq.upsert_node(err)
    await dq.upsert_node(ki)
    # Edge direction: KnownIssue → ErrorSignature
    await dq.write_edge(ki_id, "HAS_ERROR", err_id)

    rows = await dq.query(
        "MATCH (e:ErrorSignature {project_slug: $project_slug, normalized_error: $normalized_error}) "
        "MATCH (e)<-[:HAS_ERROR]-(ki:KnownIssue) "
        "RETURN ki",
        {"project_slug": "stagehand", "normalized_error": "shtp_mismatch"},
    )
    assert len(rows) == 1
    assert rows[0][0]["properties"]["issue_id"] == "KI-001"


# @spec DQ-QD-005
@pytest.mark.asyncio
async def test_query_resolved_by_returns_fix_rows():
    dq = DummyQuine()
    ki = make_known_issue("KI-001")
    fix = make_fix("FIX-001")
    ki_id = idFrom("known-issue", "stagehand", "KI-001")
    fix_id = idFrom("fix", "stagehand", "FIX-001")
    await dq.upsert_node(ki)
    await dq.upsert_node(fix)
    await dq.write_edge(ki_id, "RESOLVED_BY", fix_id)

    rows = await dq.query(
        "MATCH (ki:KnownIssue) WHERE id(ki) = $ki_node_id "
        "MATCH (ki)-[:RESOLVED_BY]->(fix:Fix {project_slug: $project_slug}) "
        "RETURN fix",
        {"ki_node_id": ki_id, "project_slug": "stagehand"},
    )
    assert len(rows) == 1
    assert rows[0][0]["properties"]["fix_id"] == "FIX-001"


# @spec DQ-QD-006
@pytest.mark.asyncio
async def test_query_similarity_match_returns_ki_with_status():
    dq = DummyQuine()
    ci = make_customer_issue("1001")
    ki = make_known_issue("KI-001")
    ci_id = idFrom("customer-issue", "stagehand", "zendesk", "1001")
    ki_id = idFrom("known-issue", "stagehand", "KI-001")
    sm = make_similarity_match(str(ci_id), str(ki_id), status="candidate")
    sm_id = idFrom("similarity-match", "stagehand", str(ci_id), str(ki_id), "graph-anchor")
    await dq.upsert_node(ci)
    await dq.upsert_node(ki)
    await dq.upsert_node(sm)
    await dq.write_edge(ci_id, "HAS_SIMILARITY_MATCH", sm_id)
    await dq.write_edge(sm_id, "MATCHES", ki_id)

    rows = await dq.query(
        "MATCH (ci:CustomerIssue) WHERE id(ci) = $issue_id "
        "MATCH (ci)-[:HAS_SIMILARITY_MATCH]->(sm:SimilarityMatch)-[:MATCHES]->(ki:KnownIssue {project_slug: $project_slug}) "
        "WHERE sm.review_status IN ['candidate', 'confirmed'] "
        "RETURN ki, sm.review_status",
        {"issue_id": ci_id, "project_slug": "stagehand"},
    )
    assert len(rows) == 1
    ki_props = rows[0][0]["properties"]
    assert ki_props["issue_id"] == "KI-001"
    review_status = rows[0][1]
    assert review_status == "candidate"


# @spec DQ-QD-006 — rejected status excluded
@pytest.mark.asyncio
async def test_query_similarity_match_excludes_rejected():
    dq = DummyQuine()
    ci = make_customer_issue("1001")
    ki = make_known_issue("KI-001")
    ci_id = idFrom("customer-issue", "stagehand", "zendesk", "1001")
    ki_id = idFrom("known-issue", "stagehand", "KI-001")
    sm = make_similarity_match(str(ci_id), str(ki_id), status="rejected")
    sm_id = idFrom("similarity-match", "stagehand", str(ci_id), str(ki_id), "graph-anchor")
    await dq.upsert_node(ci)
    await dq.upsert_node(ki)
    await dq.upsert_node(sm)
    await dq.write_edge(ci_id, "HAS_SIMILARITY_MATCH", sm_id)
    await dq.write_edge(sm_id, "MATCHES", ki_id)

    rows = await dq.query(
        "MATCH (ci:CustomerIssue) WHERE id(ci) = $issue_id "
        "MATCH (ci)-[:HAS_SIMILARITY_MATCH]->(sm:SimilarityMatch)-[:MATCHES]->(ki:KnownIssue {project_slug: $project_slug}) "
        "WHERE sm.review_status IN ['candidate', 'confirmed'] "
        "RETURN ki, sm.review_status",
        {"issue_id": ci_id, "project_slug": "stagehand"},
    )
    assert rows == []


# @spec DQ-QD-007
@pytest.mark.asyncio
async def test_query_unrecognized_fingerprint_raises():
    dq = DummyQuine()
    with pytest.raises(NotImplementedError, match="unrecognized"):
        await dq.query("MATCH (n) RETURN n", {})


# @spec DQ-QD-008
@pytest.mark.asyncio
async def test_query_row_format():
    dq = DummyQuine()
    feat = make_feature("shtp")
    ci = make_customer_issue("1001")
    ci_id = idFrom("customer-issue", "stagehand", "zendesk", "1001")
    feat_id = idFrom("feature", "stagehand", "shtp")
    await dq.upsert_node(ci)
    await dq.upsert_node(feat)
    await dq.write_edge(ci_id, "AFFECTS", feat_id)

    rows = await dq.query(
        "MATCH (ci:CustomerIssue) WHERE id(ci) = $issue_id "
        "MATCH (ci)-[:AFFECTS]->(f:Feature {project_slug: $project_slug}) "
        "RETURN f.feature_slug",
        {"issue_id": ci_id, "project_slug": "stagehand"},
    )
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, list)
    assert len(row) == 1
    item = row[0]
    assert "id" in item
    assert "properties" in item
    assert isinstance(item["id"], int)
    assert isinstance(item["properties"], dict)


# ---------------------------------------------------------------------------
# DummyQuine — Lifecycle
# ---------------------------------------------------------------------------


# @spec DQ-LC-001
@pytest.mark.asyncio
async def test_ping_always_true():
    dq = DummyQuine()
    assert await dq.ping() is True


# @spec DQ-QD-001 — project_slug filter excludes wrong project
@pytest.mark.asyncio
async def test_query_affects_filters_by_project():
    dq = DummyQuine()
    ci = make_customer_issue("1001", project="stagehand")
    feat_a = make_feature("shtp", project="stagehand")
    feat_b = make_feature("shtp", project="other-project")
    ci_id = idFrom("customer-issue", "stagehand", "zendesk", "1001")
    feat_a_id = idFrom("feature", "stagehand", "shtp")
    feat_b_id = idFrom("feature", "other-project", "shtp")
    await dq.upsert_node(ci)
    await dq.upsert_node(feat_a)
    await dq.upsert_node(feat_b)
    await dq.write_edge(ci_id, "AFFECTS", feat_a_id)
    await dq.write_edge(ci_id, "AFFECTS", feat_b_id)

    rows = await dq.query(
        "MATCH (ci:CustomerIssue) WHERE id(ci) = $issue_id "
        "MATCH (ci)-[:AFFECTS]->(f:Feature {project_slug: $project_slug}) "
        "RETURN f.feature_slug",
        {"issue_id": ci_id, "project_slug": "stagehand"},
    )
    assert len(rows) == 1
    assert rows[0][0]["properties"]["project_slug"] == "stagehand"
