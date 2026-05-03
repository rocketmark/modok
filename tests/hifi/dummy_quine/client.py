"""
DummyQuine — in-memory duck-type replacement for QuineClient.

Stores nodes in _nodes and edges in _edges, and dispatches query() calls
by matching Cypher fingerprints to Python traversal functions.
"""
from __future__ import annotations

from typing import Any

from modok.quine.errors import QuineNodeNotFoundError
from modok.quine.ids import idFrom as _idFrom
from modok.quine.models import (
    QuineNode, _NODE_TYPE_MAP,
    Project, Feature, Module, File, DocSection, ErrorSignature,
    KnownIssue, CustomerIssue, SimilarityMatch, Fix, ResolutionEvent,
    DiagnosticNote,
)


def _node_id_from_model_hifi(node: QuineNode) -> int:
    """Compute deterministic integer ID for DummyQuine's in-memory store.

    Uses the Python idFrom() helper from modok.quine.ids — this is
    test-harness-internal. Production MODOK uses Quine's Cypher-native idFrom().
    """
    if isinstance(node, Project):
        return _idFrom("project", node.project_slug)
    if isinstance(node, Feature):
        return _idFrom("feature", node.project_slug, node.feature_slug)
    if isinstance(node, Module):
        return _idFrom("module", node.project_slug, node.module_slug)
    if isinstance(node, File):
        return _idFrom("file", node.project_slug, node.repo_path)
    if isinstance(node, DocSection):
        return _idFrom("doc-section", node.project_slug, node.doc_path, node.heading_slug)
    if isinstance(node, ErrorSignature):
        return _idFrom("error", node.project_slug, node.normalized_error)
    if isinstance(node, KnownIssue):
        return _idFrom("known-issue", node.project_slug, node.issue_id)
    if isinstance(node, CustomerIssue):
        return _idFrom("customer-issue", node.project_slug, node.source_system, node.ticket_id)
    if isinstance(node, SimilarityMatch):
        return _idFrom("similarity-match", node.project_slug, node.customer_issue_id,
                       node.known_issue_id, node.method)
    if isinstance(node, Fix):
        return _idFrom("fix", node.project_slug, node.fix_id)
    if isinstance(node, ResolutionEvent):
        return _idFrom("resolution", node.project_slug, node.source_system,
                       node.ticket_id, node.fix_id)
    if isinstance(node, DiagnosticNote):
        return _idFrom("diagnostic-note", node.project_slug, node.note_id)
    raise ValueError(f"No ID scheme for node type {type(node).__name__}")


class DummyQuine:
    def __init__(self) -> None:
        self._nodes: dict[int, QuineNode] = {}
        self._edges: list[tuple[int, str, int]] = []

    # ------------------------------------------------------------------
    # Node writes
    # ------------------------------------------------------------------

    # @spec DQ-NW-001, DQ-NW-002
    async def upsert_node(self, node: QuineNode) -> None:
        node_id = _node_id_from_model_hifi(node)
        self._nodes[node_id] = node

    # ------------------------------------------------------------------
    # Node reads
    # ------------------------------------------------------------------

    # @spec DQ-NR-001, DQ-NR-002
    async def get_node(self, node_id: int, node_type: type) -> QuineNode:
        node = self._nodes.get(node_id)
        if node is None:
            raise QuineNodeNotFoundError(f"Node id={node_id} not found")
        return node

    # @spec DQ-NR-003, DQ-NR-004
    async def node_exists(self, node_id: int) -> bool:
        return node_id in self._nodes

    # ------------------------------------------------------------------
    # Edge writes
    # ------------------------------------------------------------------

    # @spec DQ-EW-001, DQ-EW-002
    async def write_edge(self, from_id: int, edge_type: str, to_id: int) -> None:
        edge = (from_id, edge_type, to_id)
        if edge not in self._edges:
            self._edges.append(edge)

    # @spec DQ-EW-003, DQ-EW-004
    async def replace_edges(self, from_id: int, edge_type: str, to_ids: list[int]) -> None:
        self._edges = [
            (f, et, t) for (f, et, t) in self._edges
            if not (f == from_id and et == edge_type)
        ]
        for to_id in to_ids:
            await self.write_edge(from_id, edge_type, to_id)

    # @spec DQ-EW-005, DQ-EW-006
    async def edge_exists(self, from_id: int, edge_type: str, to_id: int) -> bool:
        return (from_id, edge_type, to_id) in self._edges

    # ------------------------------------------------------------------
    # Query dispatch
    # ------------------------------------------------------------------

    def _node_to_row(self, node_id: int, node: QuineNode) -> list[dict[str, Any]]:
        # @spec DQ-QD-008
        return [{"id": node_id, "properties": node.model_dump()}]

    # @spec DQ-QD-001
    def _dispatch_affects_feature(self, params: dict[str, Any]) -> list[list[dict[str, Any]]]:
        issue_id = params.get("issue_id")
        project_slug = params.get("project_slug")
        results = []
        for (f, et, t) in self._edges:
            if f == issue_id and et == "AFFECTS":
                node = self._nodes.get(t)
                if node is not None and node.node_type == "Feature":
                    if project_slug is None or node.project_slug == project_slug:
                        results.append(self._node_to_row(t, node))
        return results

    # @spec DQ-QD-002
    def _dispatch_has_error_signature(self, params: dict[str, Any]) -> list[list[dict[str, Any]]]:
        issue_id = params.get("issue_id")
        project_slug = params.get("project_slug")
        results = []
        for (f, et, t) in self._edges:
            if f == issue_id and et == "HAS_ERROR":
                node = self._nodes.get(t)
                if node is not None and node.node_type == "ErrorSignature":
                    if project_slug is None or node.project_slug == project_slug:
                        results.append(self._node_to_row(t, node))
        return results

    # @spec DQ-QD-003
    def _dispatch_feature_to_files(self, params: dict[str, Any]) -> list[list[dict[str, Any]]]:
        project_slug = params.get("project_slug")
        feature_slug = params.get("feature_slug")
        # Find the Feature node
        feat_id = None
        for node_id, node in self._nodes.items():
            if (node.node_type == "Feature"
                    and node.feature_slug == feature_slug
                    and (project_slug is None or node.project_slug == project_slug)):
                feat_id = node_id
                break
        if feat_id is None:
            return []
        # Feature -IMPLEMENTED_BY-> Module -DEFINED_IN-> File
        results = []
        for (f1, et1, mod_id) in self._edges:
            if f1 == feat_id and et1 == "IMPLEMENTED_BY":
                for (f2, et2, file_id) in self._edges:
                    if f2 == mod_id and et2 == "DEFINED_IN":
                        node = self._nodes.get(file_id)
                        if node is not None and node.node_type == "File":
                            results.append(self._node_to_row(file_id, node))
        return results

    def _dispatch_files_to_commits(self, params: dict[str, Any]) -> list[list[dict[str, Any]]]:
        project_slug = params.get("project_slug")
        file_paths = set(params.get("file_paths", []))
        limit = params.get("limit", 10)
        # Find File node IDs matching the given paths
        file_ids = {
            node_id
            for node_id, node in self._nodes.items()
            if node.node_type == "File" and node.repo_path in file_paths
            and (project_slug is None or node.project_slug == project_slug)
        }
        # Find Commit nodes that TOUCH any of those files
        seen_commits: set = set()
        results = []
        for (from_id, edge_type, to_id) in self._edges:
            if edge_type == "TOUCHES" and to_id in file_ids and from_id not in seen_commits:
                node = self._nodes.get(from_id)
                if node is not None and node.node_type == "Commit":
                    seen_commits.add(from_id)
                    results.append(self._node_to_row(from_id, node))
        # Sort by timestamp descending (lexicographic on ISO-8601 is correct)
        results.sort(key=lambda r: r[0]["properties"].get("timestamp", ""), reverse=True)
        return results[:limit]

    # @spec DQ-QD-004
    def _dispatch_error_to_known_issues(self, params: dict[str, Any]) -> list[list[dict[str, Any]]]:
        project_slug = params.get("project_slug")
        normalized_error = params.get("normalized_error")
        # Find the ErrorSignature node
        err_id = None
        for node_id, node in self._nodes.items():
            if (node.node_type == "ErrorSignature"
                    and node.normalized_error == normalized_error
                    and (project_slug is None or node.project_slug == project_slug)):
                err_id = node_id
                break
        if err_id is None:
            return []
        # Reverse walk: KnownIssue -HAS_ERROR-> ErrorSignature
        results = []
        for (f, et, t) in self._edges:
            if t == err_id and et == "HAS_ERROR":
                node = self._nodes.get(f)
                if node is not None and node.node_type == "KnownIssue":
                    results.append(self._node_to_row(f, node))
        return results

    # @spec DQ-QD-005
    def _dispatch_ki_to_fixes(self, params: dict[str, Any]) -> list[list[dict[str, Any]]]:
        ki_node_id = params.get("ki_node_id")
        project_slug = params.get("project_slug")
        results = []
        for (f, et, t) in self._edges:
            if f == ki_node_id and et == "RESOLVED_BY":
                node = self._nodes.get(t)
                if node is not None and node.node_type == "Fix":
                    if project_slug is None or node.project_slug == project_slug:
                        results.append(self._node_to_row(t, node))
        return results

    # @spec DQ-QD-006
    def _dispatch_similarity(self, params: dict[str, Any]) -> list[list[dict[str, Any]]]:
        issue_id = params.get("issue_id")
        project_slug = params.get("project_slug")
        results = []
        for (f, et, sm_id) in self._edges:
            if f == issue_id and et == "HAS_SIMILARITY_MATCH":
                sm_node = self._nodes.get(sm_id)
                if sm_node is None or sm_node.node_type != "SimilarityMatch":
                    continue
                if sm_node.review_status not in ("candidate", "confirmed"):
                    continue
                for (f2, et2, ki_id) in self._edges:
                    if f2 == sm_id and et2 == "MATCHES":
                        ki_node = self._nodes.get(ki_id)
                        if ki_node is not None and ki_node.node_type == "KnownIssue":
                            if project_slug is None or ki_node.project_slug == project_slug:
                                ki_row = self._node_to_row(ki_id, ki_node)
                                results.append([ki_row[0], sm_node.review_status])
        return results

    _FINGERPRINTS = [
        ("AFFECTS]->(f:Feature",            "_dispatch_affects_feature"),
        ("HAS_ERROR]->(e:ErrorSignature",   "_dispatch_has_error_signature"),
        ("IMPLEMENTED_BY]->(m:Module)-[:DEFINED_IN]->(file:File)", "_dispatch_feature_to_files"),
        ("TOUCHES]->(f:File)",              "_dispatch_files_to_commits"),
        ("HAS_ERROR]-(ki:KnownIssue)",      "_dispatch_error_to_known_issues"),
        ("RESOLVED_BY]->(fix:Fix",          "_dispatch_ki_to_fixes"),
        ("HAS_SIMILARITY_MATCH]->(sm:SimilarityMatch)-[:MATCHES]->(ki:KnownIssue",
         "_dispatch_similarity"),
    ]

    # @spec DQ-QD-001 through DQ-QD-007
    async def query(self, cypher: str, params: dict[str, Any] | None = None) -> list[list[dict[str, Any]]]:
        params = params or {}
        for fingerprint, method_name in self._FINGERPRINTS:
            if fingerprint in cypher:
                return getattr(self, method_name)(params)
        # @spec DQ-QD-007
        raise NotImplementedError(
            f"DummyQuine: unrecognized Cypher fingerprint — add a dispatch handler. Query: {cypher!r}"
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    # @spec DQ-LC-001
    async def ping(self) -> bool:
        return True
