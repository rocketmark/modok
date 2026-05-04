"""
ReferenceModok — independent in-memory reference implementation.

No LLM, no Quine HTTP. Ingests nodes and edges directly, then traverses
them with the same logic as the DRE to produce a DebugPacket.
"""
from __future__ import annotations

from typing import Any

from modok.quine.models import QuineNode
from tests.hifi.dummy_quine.client import _node_id_from_model_hifi as _node_id_from_model
from modok.retrieval.errors import DRENotFoundError
from modok.retrieval.models import (
    AffectedArea,
    DebugPacket,
    IssueAnchors,
    IssueSummary,
    KnownIssueRef,
    PriorFix,
)

_KI_CAP = 10
_FIX_CAP = 10
_FILE_CAP = 20


class ReferenceModok:
    def __init__(self) -> None:
        self._nodes: dict[int, QuineNode] = {}
        self._edges: list[tuple[int, str, int]] = []

    # @spec REF-ING-001, REF-ING-002, REF-ING-003, REF-ING-004
    def ingest(self, nodes: list[QuineNode], edges: list[tuple[int, str, int]]) -> None:
        for node in nodes:
            node_id = _node_id_from_model(node)
            self._nodes[node_id] = node
        for edge in edges:
            if edge not in self._edges:
                self._edges.append(edge)

    # @spec REF-RET-001, REF-RET-002, REF-RET-003, REF-RET-004,
    #       REF-RET-005, REF-RET-006, REF-RET-007, REF-RET-008, REF-RET-009
    def retrieve(self, issue_id: int, project_slug: str) -> DebugPacket:
        node = self._nodes.get(issue_id)
        if node is None or node.node_type != "CustomerIssue":
            raise DRENotFoundError(f"CustomerIssue id={issue_id} not found")
        if node.project_slug != project_slug:
            raise DRENotFoundError(
                f"CustomerIssue id={issue_id} belongs to project '{node.project_slug}', "
                f"not '{project_slug}'"
            )

        # Extract anchors from direct edges on the CustomerIssue
        feature_slugs: list[str] = []
        error_sigs: list[str] = []

        for (f, et, t) in self._edges:
            if f != issue_id:
                continue
            target = self._nodes.get(t)
            if target is None:
                continue
            if et == "AFFECTS" and target.node_type == "Feature" and target.project_slug == project_slug:
                feature_slugs.append(target.feature_slug)
            elif et == "HAS_ERROR" and target.node_type == "ErrorSignature" and target.project_slug == project_slug:
                error_sigs.append(target.normalized_error)

        # Accumulators
        ki_counts: dict[str, int] = {}
        ki_meta: dict[str, dict[str, Any]] = {}
        fix_counts: dict[str, int] = {}
        fix_meta: dict[str, dict[str, Any]] = {}
        file_counts: dict[str, int] = {}
        matched_feature_slugs: list[str] = []

        # Feature → Module → File
        for slug in feature_slugs:
            feat_id = None
            for node_id, n in self._nodes.items():
                if n.node_type == "Feature" and n.feature_slug == slug and n.project_slug == project_slug:
                    feat_id = node_id
                    break
            if feat_id is None:
                continue
            paths: list[str] = []
            for (f1, et1, mod_id) in self._edges:
                if f1 != feat_id or et1 != "IMPLEMENTED_BY":
                    continue
                for (f2, et2, file_id) in self._edges:
                    if f2 != mod_id or et2 != "DEFINED_IN":
                        continue
                    file_node = self._nodes.get(file_id)
                    if file_node is not None and file_node.node_type == "File":
                        file_counts[file_node.repo_path] = file_counts.get(file_node.repo_path, 0) + 1
                        paths.append(file_node.repo_path)
            if paths:
                matched_feature_slugs.append(slug)

        # Error → KnownIssue → Fix
        for err in error_sigs:
            err_id = None
            for node_id, n in self._nodes.items():
                if n.node_type == "ErrorSignature" and n.normalized_error == err and n.project_slug == project_slug:
                    err_id = node_id
                    break
            if err_id is None:
                continue
            for (f, et, t) in self._edges:
                if t != err_id or et != "HAS_ERROR":
                    continue
                ki_node = self._nodes.get(f)
                if ki_node is None or ki_node.node_type != "KnownIssue":
                    continue
                ki_id_str = ki_node.issue_id
                ki_counts[ki_id_str] = ki_counts.get(ki_id_str, 0) + 1
                ki_meta[ki_id_str] = ki_node.model_dump()

                # KnownIssue → Fix
                ki_node_id = f
                for (f2, et2, fix_id) in self._edges:
                    if f2 != ki_node_id or et2 != "RESOLVED_BY":
                        continue
                    fix_node = self._nodes.get(fix_id)
                    if fix_node is not None and fix_node.node_type == "Fix" and fix_node.project_slug == project_slug:
                        fix_counts[fix_node.fix_id] = fix_counts.get(fix_node.fix_id, 0) + 1
                        fix_meta[fix_node.fix_id] = fix_node.model_dump()

        # Sort and cap
        ki_items = sorted(ki_counts.items(), key=lambda x: x[1], reverse=True)[:_KI_CAP]
        fix_items = sorted(fix_counts.items(), key=lambda x: x[1], reverse=True)[:_FIX_CAP]
        file_items = sorted(file_counts.items(), key=lambda x: x[1], reverse=True)[:_FILE_CAP]

        known_issues = [
            KnownIssueRef(id=ki_id, summary=ki_meta[ki_id].get("summary", ""))
            for ki_id, _ in ki_items
        ]
        prior_fixes = [
            PriorFix(id=fix_id, commit="", summary=fix_meta[fix_id].get("summary", ""))
            for fix_id, _ in fix_items
        ]
        relevant_files = [path for path, _ in file_items]

        affected_areas = [
            AffectedArea(type="feature", id=f"feature:{slug}", name=slug)
            for slug in matched_feature_slugs
        ]

        return DebugPacket(
            issue=IssueSummary(
                summary=node.summary,
                anchors=IssueAnchors(
                    features=feature_slugs,
                    errors=error_sigs,
                    symptoms=[],
                ),
            ),
            affected_areas=affected_areas,
            relevant_files=relevant_files,
            relevant_tests=[],
            known_issues=known_issues,
            prior_fixes=prior_fixes,
            next_steps=[],
        )
