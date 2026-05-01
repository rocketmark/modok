"""
Tests for ReferenceModok — the independent in-memory reference implementation.
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
from modok.retrieval.errors import DRENotFoundError

from tests.hifi.reference.model import ReferenceModok


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ci(ticket: str = "1001", project: str = "stagehand") -> CustomerIssue:
    return CustomerIssue(
        node_type="CustomerIssue", project_slug=project,
        source_system="zendesk", ticket_id=ticket,
        summary="tracker dropout", status="open",
    )

def _feature(slug: str = "shtp", project: str = "stagehand") -> Feature:
    return Feature(node_type="Feature", project_slug=project, feature_slug=slug, name=slug)

def _module(slug: str = "shtp-mod", project: str = "stagehand") -> Module:
    return Module(node_type="Module", project_slug=project, module_slug=slug, name=slug)

def _file(path: str = "agent/src/shtp.c", project: str = "stagehand") -> File:
    return File(node_type="File", project_slug=project, repo_path=path)

def _error(err: str = "shtp_mismatch", project: str = "stagehand") -> ErrorSignature:
    return ErrorSignature(
        node_type="ErrorSignature", project_slug=project,
        normalized_error=err, display_text=err,
    )

def _ki(ki_id: str = "KI-001", project: str = "stagehand") -> KnownIssue:
    return KnownIssue(
        node_type="KnownIssue", project_slug=project,
        issue_id=ki_id, summary="SHTP mismatch", status="open",
    )

def _fix(fix_id: str = "FIX-001", project: str = "stagehand") -> Fix:
    return Fix(
        node_type="Fix", project_slug=project,
        fix_id=fix_id, summary="Upgrade SHTP", kind="patch",
    )


def _ci_id(ticket: str = "1001", project: str = "stagehand") -> int:
    return idFrom("customer-issue", project, "zendesk", ticket)

def _feat_id(slug: str = "shtp", project: str = "stagehand") -> int:
    return idFrom("feature", project, slug)

def _mod_id(slug: str = "shtp-mod", project: str = "stagehand") -> int:
    return idFrom("module", project, slug)

def _file_id(path: str = "agent/src/shtp.c", project: str = "stagehand") -> int:
    return idFrom("file", project, path)

def _err_id(err: str = "shtp_mismatch", project: str = "stagehand") -> int:
    return idFrom("error", project, err)

def _ki_id(ki_id: str = "KI-001", project: str = "stagehand") -> int:
    return idFrom("known-issue", project, ki_id)

def _fix_id(fix_id: str = "FIX-001", project: str = "stagehand") -> int:
    return idFrom("fix", project, fix_id)


# ---------------------------------------------------------------------------
# Reference Model — Ingest
# ---------------------------------------------------------------------------

# @spec REF-ING-001
def test_ingest_stores_node_by_id():
    ref = ReferenceModok()
    feat = _feature()
    ref.ingest([feat], [])
    assert _feat_id() in ref._nodes


# @spec REF-ING-002
def test_ingest_node_twice_stores_once():
    ref = ReferenceModok()
    feat = _feature()
    ref.ingest([feat], [])
    ref.ingest([feat], [])
    assert sum(1 for k in ref._nodes if k == _feat_id()) == 1


# @spec REF-ING-003
def test_ingest_edge_twice_stores_once():
    ref = ReferenceModok()
    edge = (_feat_id(), "IMPLEMENTED_BY", _mod_id())
    ref.ingest([], [edge])
    ref.ingest([], [edge])
    assert ref._edges.count(edge) == 1


# @spec REF-ING-004
def test_ingest_uses_same_idfrom_as_production():
    ref = ReferenceModok()
    ci = _ci("9999", "stagehand")
    ref.ingest([ci], [])
    expected_id = idFrom("customer-issue", "stagehand", "zendesk", "9999")
    assert expected_id in ref._nodes


# ---------------------------------------------------------------------------
# Reference Model — Retrieval
# ---------------------------------------------------------------------------

# @spec REF-RET-001
def test_retrieve_missing_issue_raises():
    ref = ReferenceModok()
    with pytest.raises(DRENotFoundError):
        ref.retrieve(_ci_id("9999"), "stagehand")


# @spec REF-RET-002
def test_retrieve_wrong_project_raises():
    ref = ReferenceModok()
    ci = _ci("1001", project="other")
    ref.ingest([ci], [])
    with pytest.raises(DRENotFoundError):
        ref.retrieve(_ci_id("1001", "other"), "stagehand")


# @spec REF-RET-003
def test_retrieve_extracts_feature_anchors():
    ref = ReferenceModok()
    ci = _ci()
    feat = _feature()
    ci_id = _ci_id()
    feat_id = _feat_id()
    ref.ingest([ci, feat], [(ci_id, "AFFECTS", feat_id)])
    packet = ref.retrieve(ci_id, "stagehand")
    assert "shtp" in packet.anchors.feature_slugs


# @spec REF-RET-003
def test_retrieve_extracts_error_anchors():
    ref = ReferenceModok()
    ci = _ci()
    err = _error()
    ci_id = _ci_id()
    err_id = _err_id()
    ref.ingest([ci, err], [(ci_id, "HAS_ERROR", err_id)])
    packet = ref.retrieve(ci_id, "stagehand")
    assert "shtp_mismatch" in packet.anchors.error_signatures


# @spec REF-RET-004
def test_retrieve_follows_feature_to_files():
    ref = ReferenceModok()
    ci = _ci()
    feat = _feature()
    mod = _module()
    fil = _file()
    ci_id = _ci_id()
    feat_id = _feat_id()
    mod_id = _mod_id()
    file_id = _file_id()
    ref.ingest(
        [ci, feat, mod, fil],
        [
            (ci_id, "AFFECTS", feat_id),
            (feat_id, "IMPLEMENTED_BY", mod_id),
            (mod_id, "DEFINED_IN", file_id),
        ],
    )
    packet = ref.retrieve(ci_id, "stagehand")
    paths = [f.repo_path for f in packet.relevant_files]
    assert "agent/src/shtp.c" in paths


# @spec REF-RET-005
def test_retrieve_follows_error_to_known_issues():
    ref = ReferenceModok()
    ci = _ci()
    err = _error()
    ki = _ki()
    ci_id = _ci_id()
    err_id = _err_id()
    ki_id = _ki_id()
    ref.ingest(
        [ci, err, ki],
        [
            (ci_id, "HAS_ERROR", err_id),
            (ki_id, "HAS_ERROR", err_id),
        ],
    )
    packet = ref.retrieve(ci_id, "stagehand")
    ki_ids = [k.known_issue_id for k in packet.known_issues]
    assert "KI-001" in ki_ids


# @spec REF-RET-006
def test_retrieve_follows_ki_to_fixes():
    ref = ReferenceModok()
    ci = _ci()
    err = _error()
    ki = _ki()
    fix = _fix()
    ci_id = _ci_id()
    err_id = _err_id()
    ki_id = _ki_id()
    fix_id = _fix_id()
    ref.ingest(
        [ci, err, ki, fix],
        [
            (ci_id, "HAS_ERROR", err_id),
            (ki_id, "HAS_ERROR", err_id),
            (ki_id, "RESOLVED_BY", fix_id),
        ],
    )
    packet = ref.retrieve(ci_id, "stagehand")
    fix_ids = [f.fix_id for f in packet.recent_fixes]
    assert "FIX-001" in fix_ids


# @spec REF-RET-007
def test_retrieve_ranks_by_match_count_descending():
    ref = ReferenceModok()
    ci = _ci()
    err1 = _error("err_a")
    err2 = _error("err_b")
    ki1 = _ki("KI-001")
    ki2 = _ki("KI-002")
    ci_id = _ci_id()
    err1_id = idFrom("error", "stagehand", "err_a")
    err2_id = idFrom("error", "stagehand", "err_b")
    ki1_id = idFrom("known-issue", "stagehand", "KI-001")
    ki2_id = idFrom("known-issue", "stagehand", "KI-002")
    # KI-001 matched by both errors; KI-002 matched by one
    ref.ingest(
        [ci, err1, err2, ki1, ki2],
        [
            (ci_id, "HAS_ERROR", err1_id),
            (ci_id, "HAS_ERROR", err2_id),
            (ki1_id, "HAS_ERROR", err1_id),
            (ki1_id, "HAS_ERROR", err2_id),
            (ki2_id, "HAS_ERROR", err1_id),
        ],
    )
    packet = ref.retrieve(ci_id, "stagehand")
    ki_ids = [k.known_issue_id for k in packet.known_issues]
    assert ki_ids.index("KI-001") < ki_ids.index("KI-002")


# @spec REF-RET-008
def test_retrieve_confidence_positive_when_anchor_matched():
    ref = ReferenceModok()
    ci = _ci()
    feat = _feature()
    mod = _module()
    fil = _file()
    ci_id = _ci_id()
    feat_id = _feat_id()
    mod_id = _mod_id()
    file_id = _file_id()
    ref.ingest(
        [ci, feat, mod, fil],
        [
            (ci_id, "AFFECTS", feat_id),
            (feat_id, "IMPLEMENTED_BY", mod_id),
            (mod_id, "DEFINED_IN", file_id),
        ],
    )
    packet = ref.retrieve(ci_id, "stagehand")
    assert packet.confidence > 0


# @spec REF-RET-009
def test_retrieve_excludes_other_project_nodes():
    ref = ReferenceModok()
    ci_a = _ci("1001", "project-a")
    ci_b = _ci("1001", "project-b")
    ki_a = _ki("KI-A", "project-a")
    ki_b = _ki("KI-B", "project-b")
    err_a = _error("err_x", "project-a")
    err_b = _error("err_x", "project-b")
    ci_a_id = idFrom("customer-issue", "project-a", "zendesk", "1001")
    ci_b_id = idFrom("customer-issue", "project-b", "zendesk", "1001")
    ki_a_id = idFrom("known-issue", "project-a", "KI-A")
    ki_b_id = idFrom("known-issue", "project-b", "KI-B")
    err_a_id = idFrom("error", "project-a", "err_x")
    err_b_id = idFrom("error", "project-b", "err_x")
    ref.ingest(
        [ci_a, ci_b, ki_a, ki_b, err_a, err_b],
        [
            (ci_a_id, "HAS_ERROR", err_a_id),
            (ki_a_id, "HAS_ERROR", err_a_id),
            (ci_b_id, "HAS_ERROR", err_b_id),
            (ki_b_id, "HAS_ERROR", err_b_id),
        ],
    )
    packet = ref.retrieve(ci_a_id, "project-a")
    ki_ids = [k.known_issue_id for k in packet.known_issues]
    assert "KI-A" in ki_ids
    assert "KI-B" not in ki_ids
