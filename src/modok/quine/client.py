from __future__ import annotations

import asyncio
from typing import Any, Literal, TypeVar

import httpx

from modok.quine.errors import QuineNodeNotFoundError, QuineDeserializationError
from modok.quine.models import QuineNode, _NODE_TYPE_MAP

T = TypeVar("T", bound=QuineNode)

# Quine node IDs are UUIDs returned as strings in Quine query responses.
# MODOK never computes node addresses in Python — idFrom() is called inside Cypher strings.
QuineNodeId = str

_RETRY_STATUSES = {500, 502, 503, 504}
_MAX_RETRIES = 3
_DEFAULT_TIMEOUT = 10.0
_BACKOFF_BASE = 0.5  # seconds; doubles each retry


# @spec QC-NR-003, QC-NR-004
def _row_is_real_node(results: list) -> bool:
    """`MATCH (n) WHERE id(n) = <any address> RETURN n` always returns a row
    in Quine, even for an address nothing was ever written to — an
    empty-property shell, not a real node (confirmed live: the same address
    queried twice, never referenced before, returns identical empty
    properties both times — this is Quine's addressing model, not
    first-touch vivification). `bool(results)` alone can never distinguish a
    real node from this shell. A node only "exists" in MODOK's sense once it
    has a node_type property — the same discipline collect_nodes() already
    applies (CLI-REC-009, src/modok/cli/commands/_output.py)."""
    if not results or not results[0]:
        return False
    node = results[0][0]
    return bool(isinstance(node, dict) and node.get("properties", {}).get("node_type"))


class TraversalStep:
    def __init__(
        self,
        edge_type: str,
        direction: Literal["out", "in", "both"] = "out",
    ) -> None:
        self.edge_type = edge_type
        self.direction = direction


class QuineClient:
    # @spec QC-CN-003
    timeout_per_attempt: float = _DEFAULT_TIMEOUT

    def __init__(
        self,
        base_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    def _make_http_client(self) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "base_url": self._base_url,
            "timeout": self.timeout_per_attempt,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    async def _cypher(
        self, query: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        # @spec QC-CN-001, QC-CN-002, QC-CN-003
        payload = {"text": query, "parameters": params or {}}
        last_exc: Exception | None = None

        async with self._make_http_client() as http:
            for attempt in range(_MAX_RETRIES):
                try:
                    resp = await http.post("/api/v1/query/cypher", json=payload)
                    if resp.status_code in _RETRY_STATUSES:
                        last_exc = httpx.HTTPStatusError(
                            f"HTTP {resp.status_code}", request=resp.request, response=resp
                        )
                        if attempt < _MAX_RETRIES - 1:
                            await asyncio.sleep(_BACKOFF_BASE * (2**attempt))
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    return data.get("results", [])
                except httpx.TimeoutException as exc:
                    last_exc = exc
                    if attempt < _MAX_RETRIES - 1:
                        await asyncio.sleep(_BACKOFF_BASE * (2**attempt))

        raise last_exc or RuntimeError("Quine request failed")

    def _deserialize(self, row: list[Any]) -> QuineNode:
        # @spec QC-TR-003
        if not row:
            raise QuineDeserializationError("Empty result row")
        raw = row[0]
        props = raw.get("properties", {})
        node_id = raw.get("id")
        node_type = props.get("node_type")
        model_cls = _NODE_TYPE_MAP.get(node_type)
        if model_cls is None:
            raise QuineDeserializationError(f"Unknown node_type '{node_type}' on node id={node_id}")
        try:
            return model_cls(**props)
        except Exception as exc:
            raise QuineDeserializationError(
                f"Failed to deserialize node id={node_id} type={node_type}: {exc}"
            ) from exc

    # @spec QC-NW-001, QC-NW-002, QC-NW-003, QC-NW-004
    async def upsert_node(self, node: QuineNode) -> None:
        props = node.model_dump()
        id_expr, id_params = _idFrom_cypher_args(node)
        set_clause = ", ".join(f"n.{k} = ${k}" for k in props)
        # MATCH on idFrom(...) address, SET replaces all properties.
        # No relationship clause — edges are never touched here.
        query = f"MATCH (n) WHERE id(n) = idFrom({id_expr}) SET {set_clause}"
        await self._cypher(query, {**id_params, **props})

    # @spec QC-NR-001, QC-NR-002
    async def get_node(self, node_id: QuineNodeId, node_type: type[T]) -> T:
        results = await self._cypher(
            "MATCH (n) WHERE id(n) = $node_id RETURN n",
            {"node_id": node_id},
        )
        if not results:
            raise QuineNodeNotFoundError(f"Node id={node_id} not found")
        node = self._deserialize(results[0])
        if not isinstance(node, node_type):
            raise QuineDeserializationError(
                f"Expected {node_type.__name__}, got {type(node).__name__} for id={node_id}"
            )
        return node  # type: ignore[return-value]

    # @spec QC-NR-003
    async def node_exists(self, node_id: QuineNodeId) -> bool:
        results = await self._cypher(
            "MATCH (n) WHERE id(n) = $node_id RETURN n",
            {"node_id": node_id},
        )
        return _row_is_real_node(results)

    # @spec QC-NR-004
    async def node_exists_by_parts(self, parts: tuple[str, ...]) -> bool:
        """Like node_exists, but embeds Quine's idFrom() in the query text
        instead of requiring the caller to already have a real Quine node ID.
        Use this whenever only the logical idFrom() parts are known — never
        pass a modok.quine.ids.idFrom() Python value to node_exists(); it is
        not a valid Quine ID (see docs/llds/quine-client.md)."""
        args = ", ".join(f"$p{i}" for i in range(len(parts)))
        params = {f"p{i}": p for i, p in enumerate(parts)}
        results = await self._cypher(f"MATCH (n) WHERE id(n) = idFrom({args}) RETURN n", params)
        return _row_is_real_node(results)

    # @spec QC-EW-001, QC-EW-002, QC-EW-003
    async def write_edge(self, from_id: QuineNodeId, edge_type: str, to_id: QuineNodeId) -> None:
        # MERGE on both endpoints and the relationship — idempotent by construction.
        query = (
            "MATCH (a) WHERE id(a) = $from_id "
            "MATCH (b) WHERE id(b) = $to_id "
            f"MERGE (a)-[:{edge_type}]->(b)"
        )
        await self._cypher(query, {"from_id": from_id, "to_id": to_id})

    # Ingestion path: write an edge using idFrom() argument tuples instead of UUIDs.
    # Used when the caller knows the logical parts but has no UUID from a prior query.
    # @spec QC-EW-001, QC-EW-002, QC-EW-003, QC-EW-005
    async def write_edge_by_parts(
        self,
        from_parts: tuple[str, ...],
        edge_type: str,
        to_parts: tuple[str, ...],
        properties: dict[str, Any] | None = None,
    ) -> None:
        from_args = ", ".join(f"$fp{i}" for i in range(len(from_parts)))
        to_args = ", ".join(f"$tp{i}" for i in range(len(to_parts)))
        params: dict[str, Any] = {}
        for i, p in enumerate(from_parts):
            params[f"fp{i}"] = p
        for i, p in enumerate(to_parts):
            params[f"tp{i}"] = p
        if properties:
            params["props"] = properties
            query = (
                f"MATCH (a) WHERE id(a) = idFrom({from_args}) "
                f"MATCH (b) WHERE id(b) = idFrom({to_args}) "
                f"MERGE (a)-[r:{edge_type}]->(b) SET r += $props"
            )
        else:
            query = (
                f"MATCH (a) WHERE id(a) = idFrom({from_args}) "
                f"MATCH (b) WHERE id(b) = idFrom({to_args}) "
                f"MERGE (a)-[:{edge_type}]->(b)"
            )
        await self._cypher(query, params)

    # @spec QC-EW-004
    async def replace_edges(
        self, from_id: QuineNodeId, edge_type: str, to_ids: list[QuineNodeId]
    ) -> None:
        await self._cypher(
            f"MATCH (a)-[r:{edge_type}]->() WHERE id(a) = $from_id DELETE r",
            {"from_id": from_id},
        )
        for to_id in to_ids:
            await self.write_edge(from_id, edge_type, to_id)

    # @spec QC-EW-006
    async def replace_edges_by_parts(
        self,
        from_parts: tuple[str, ...],
        edge_type: str,
        to_parts_list: list[tuple[str, ...]],
    ) -> None:
        """By-parts equivalent of replace_edges — addresses the source node via
        idFrom() embedded in the query text rather than a precomputed ID (see
        node_exists_by_parts). Deletes all edges of edge_type from the node
        addressed by from_parts, then writes one edge per address in
        to_parts_list via write_edge_by_parts."""
        from_args = ", ".join(f"$fp{i}" for i in range(len(from_parts)))
        from_params = {f"fp{i}": p for i, p in enumerate(from_parts)}
        await self._cypher(
            f"MATCH (a)-[r:{edge_type}]->() WHERE id(a) = idFrom({from_args}) DELETE r",
            from_params,
        )
        for to_parts in to_parts_list:
            await self.write_edge_by_parts(from_parts, edge_type, to_parts)

    async def edge_exists(self, from_id: QuineNodeId, edge_type: str, to_id: QuineNodeId) -> bool:
        results = await self._cypher(
            f"MATCH (a)-[r:{edge_type}]->(b) WHERE id(a) = $from_id AND id(b) = $to_id RETURN r",
            {"from_id": from_id, "to_id": to_id},
        )
        return bool(results)

    # @spec QC-TR-001
    async def traverse(self, start_id: QuineNodeId, steps: list[TraversalStep]) -> list[QuineNode]:
        if not steps:
            return []
        match_clauses = ["MATCH (n0) WHERE id(n0) = $start_id"]
        for i, step in enumerate(steps):
            src = f"n{i}"
            dst = f"n{i + 1}"
            if step.direction == "out":
                rel = f"({src})-[:{step.edge_type}]->({dst})"
            elif step.direction == "in":
                rel = f"({src})<-[:{step.edge_type}]-({dst})"
            else:
                rel = f"({src})-[:{step.edge_type}]-({dst})"
            match_clauses.append(f"MATCH {rel}")
        last = f"n{len(steps)}"
        query = " ".join(match_clauses) + f" RETURN {last}"
        results = await self._cypher(query, {"start_id": start_id})
        return [self._deserialize(row) for row in results]

    # @spec QC-TR-002
    async def query(
        self, cypher: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return await self._cypher(cypher, params)

    # @spec QC-CN-004
    async def ping(self) -> bool:
        try:
            async with self._make_http_client() as http:
                resp = await http.get("/api/v1/query/cypher")
                return resp.status_code < 500
        except Exception:
            return False

    # -----------------------------------------------------------------
    # Standing queries
    # -----------------------------------------------------------------

    # @spec SQ-CLIENT-001
    async def standing_query_exists(self, name: str) -> bool:
        async with self._make_http_client() as http:
            resp = await http.get(f"/api/v1/query/standing/{name}")
            return resp.status_code == 200

    # @spec SQ-CLIENT-002, SQ-CLIENT-003
    async def install_standing_query(self, definition: Any, callback_url: str) -> bool:
        if await self.standing_query_exists(definition.name):
            return False
        body = {
            "pattern": {
                "query": definition.pattern,
                "type": "Cypher",
                "mode": definition.mode,
            },
            "outputs": {
                definition.output_name: {
                    "type": "CypherQuery",
                    "query": definition.enrichment_query,
                    "andThen": {"type": "PostToEndpoint", "url": callback_url},
                }
            },
        }
        async with self._make_http_client() as http:
            resp = await http.post(f"/api/v1/query/standing/{definition.name}", json=body)
            resp.raise_for_status()
        return True

    # @spec SQ-CLIENT-004
    async def list_standing_queries(self) -> list[str]:
        async with self._make_http_client() as http:
            resp = await http.get("/api/v1/query/standing")
            resp.raise_for_status()
            data = resp.json()
        return [entry["name"] for entry in data]

    # @spec SQ-CLIENT-005
    async def remove_standing_query(self, name: str) -> bool:
        if not await self.standing_query_exists(name):
            return False
        async with self._make_http_client() as http:
            resp = await http.delete(f"/api/v1/query/standing/{name}")
            resp.raise_for_status()
        return True


def _idFrom_cypher_args(node: QuineNode) -> tuple[str, dict[str, Any]]:
    """Return (cypher_idFrom_arg_list, params) for this node's idFrom() address.

    The returned cypher string is the argument list for Quine's idFrom() Cypher function,
    e.g. "'feature', $idf_project_slug, $idf_feature_slug".
    Param keys are prefixed with 'idf_' to avoid collision with node property params.
    """
    from modok.quine.models import (
        Project,
        Feature,
        Module,
        File,
        TestFile,
        Doc,
        DocSection,
        ErrorSignature,
        KnownIssue,
        CustomerIssue,
        FileEscalation,
        RootCauseEscalation,
        SimilarityMatch,
        Fix,
        ResolutionEvent,
        DiagnosticNote,
        Commit,
        Investigation,
        WorkflowRun,
        WorkflowJob,
        WorkflowJobStep,
        TestExecution,
        TestFailure,
        InvestigationMilestone,
        DependencyPackage,
        DependencyVersion,
        DependencyManifest,
        DependencySnapshot,
        DependencyChange,
    )

    if isinstance(node, Doc):
        return (
            "'doc', $idf_project_slug, $idf_doc_path",
            {"idf_project_slug": node.project_slug, "idf_doc_path": node.doc_path},
        )
    if isinstance(node, Commit):
        return (
            "'commit', $idf_project_slug, $idf_sha",
            {"idf_project_slug": node.project_slug, "idf_sha": node.sha},
        )
    if isinstance(node, Project):
        return ("'project', $idf_project_slug", {"idf_project_slug": node.project_slug})
    if isinstance(node, Feature):
        return (
            "'feature', $idf_project_slug, $idf_feature_slug",
            {"idf_project_slug": node.project_slug, "idf_feature_slug": node.feature_slug},
        )
    if isinstance(node, Module):
        return (
            "'module', $idf_project_slug, $idf_module_slug",
            {"idf_project_slug": node.project_slug, "idf_module_slug": node.module_slug},
        )
    if isinstance(node, File):
        return (
            "'file', $idf_project_slug, $idf_repo_path",
            {"idf_project_slug": node.project_slug, "idf_repo_path": node.repo_path},
        )
    if isinstance(node, TestFile):
        return (
            "'test-file', $idf_project_slug, $idf_repo_path",
            {"idf_project_slug": node.project_slug, "idf_repo_path": node.repo_path},
        )
    if isinstance(node, DocSection):
        return (
            "'doc-section', $idf_project_slug, $idf_doc_path, $idf_heading_slug",
            {
                "idf_project_slug": node.project_slug,
                "idf_doc_path": node.doc_path,
                "idf_heading_slug": node.heading_slug,
            },
        )
    if isinstance(node, ErrorSignature):
        return (
            "'error', $idf_project_slug, $idf_normalized_error",
            {"idf_project_slug": node.project_slug, "idf_normalized_error": node.normalized_error},
        )
    if isinstance(node, KnownIssue):
        return (
            "'known-issue', $idf_project_slug, $idf_issue_id",
            {"idf_project_slug": node.project_slug, "idf_issue_id": node.issue_id},
        )
    if isinstance(node, CustomerIssue):
        return (
            "'customer-issue', $idf_project_slug, $idf_source_system, $idf_ticket_id",
            {
                "idf_project_slug": node.project_slug,
                "idf_source_system": node.source_system,
                "idf_ticket_id": node.ticket_id,
            },
        )
    if isinstance(node, FileEscalation):
        return (
            "'file-escalation', $idf_project_slug, $idf_file_path, $idf_since_commit",
            {
                "idf_project_slug": node.project_slug,
                "idf_file_path": node.file_path,
                "idf_since_commit": node.since_commit,
            },
        )
    if isinstance(node, RootCauseEscalation):
        return (
            "'root-cause-escalation', $idf_project_slug, $idf_feature_slug, $idf_sequence",
            {
                "idf_project_slug": node.project_slug,
                "idf_feature_slug": node.feature_slug,
                "idf_sequence": node.sequence,
            },
        )
    if isinstance(node, SimilarityMatch):
        return (
            "'similarity-match', $idf_project_slug, $idf_customer_issue_id, $idf_known_issue_id, $idf_method",
            {
                "idf_project_slug": node.project_slug,
                "idf_customer_issue_id": node.customer_issue_id,
                "idf_known_issue_id": node.known_issue_id,
                "idf_method": node.method,
            },
        )
    if isinstance(node, Fix):
        return (
            "'fix', $idf_project_slug, $idf_fix_id",
            {"idf_project_slug": node.project_slug, "idf_fix_id": node.fix_id},
        )
    if isinstance(node, ResolutionEvent):
        return (
            "'resolution', $idf_project_slug, $idf_source_system, $idf_ticket_id, $idf_fix_id",
            {
                "idf_project_slug": node.project_slug,
                "idf_source_system": node.source_system,
                "idf_ticket_id": node.ticket_id,
                "idf_fix_id": node.fix_id,
            },
        )
    if isinstance(node, DiagnosticNote):
        return (
            "'diagnostic-note', $idf_project_slug, $idf_note_id",
            {"idf_project_slug": node.project_slug, "idf_note_id": node.note_id},
        )
    if isinstance(node, Investigation):
        return (
            "'investigation', $idf_project_slug, $idf_investigation_id",
            {
                "idf_project_slug": node.project_slug,
                "idf_investigation_id": node.investigation_id,
            },
        )
    if isinstance(node, WorkflowRun):
        return (
            "'workflow-run', $idf_project_slug, $idf_run_id",
            {"idf_project_slug": node.project_slug, "idf_run_id": node.run_id},
        )
    if isinstance(node, WorkflowJob):
        # run_attempt is part of the natural key — see § Workflow Job Identity.
        return (
            "'workflow-job', $idf_project_slug, $idf_run_id, $idf_run_attempt, $idf_github_job_id",
            {
                "idf_project_slug": node.project_slug,
                "idf_run_id": node.run_id,
                "idf_run_attempt": node.run_attempt,
                "idf_github_job_id": node.github_job_id,
            },
        )
    if isinstance(node, WorkflowJobStep):
        return (
            "'workflow-job-step', $idf_project_slug, $idf_run_id, $idf_run_attempt, "
            "$idf_github_job_id, $idf_step_number",
            {
                "idf_project_slug": node.project_slug,
                "idf_run_id": node.run_id,
                "idf_run_attempt": node.run_attempt,
                "idf_github_job_id": node.github_job_id,
                "idf_step_number": node.step_number,
            },
        )
    if isinstance(node, TestExecution):
        return (
            "'test-execution', $idf_project_slug, $idf_run_id, $idf_run_attempt, "
            "$idf_classname, $idf_test_name",
            {
                "idf_project_slug": node.project_slug,
                "idf_run_id": node.run_id,
                "idf_run_attempt": node.run_attempt,
                "idf_classname": node.classname,
                "idf_test_name": node.test_name,
            },
        )
    if isinstance(node, TestFailure):
        return (
            "'test-failure', $idf_project_slug, $idf_run_id, $idf_run_attempt, "
            "$idf_classname, $idf_test_name",
            {
                "idf_project_slug": node.project_slug,
                "idf_run_id": node.run_id,
                "idf_run_attempt": node.run_attempt,
                "idf_classname": node.classname,
                "idf_test_name": node.test_name,
            },
        )
    if isinstance(node, InvestigationMilestone):
        # Distinct, evidence-free identity scheme (not the Investigation
        # scheme above) — see docs/llds/standing-queries.md § Investigation
        # and Milestone Model.
        return (
            "'investigation-milestone', $idf_project_slug, $idf_investigation_id, "
            "$idf_milestone_kind, $idf_test_failure_id, $idf_error_signature",
            {
                "idf_project_slug": node.project_slug,
                "idf_investigation_id": node.investigation_id,
                "idf_milestone_kind": node.milestone_kind,
                "idf_test_failure_id": node.test_failure_id or "",
                "idf_error_signature": node.error_signature or "",
            },
        )
    if isinstance(node, DependencyPackage):
        return (
            "'dependency-package', $idf_project_slug, $idf_purl",
            {"idf_project_slug": node.project_slug, "idf_purl": node.purl},
        )
    if isinstance(node, DependencyVersion):
        return (
            "'dependency-version', $idf_project_slug, $idf_package_purl, $idf_version",
            {
                "idf_project_slug": node.project_slug,
                "idf_package_purl": node.package_purl,
                "idf_version": node.version,
            },
        )
    if isinstance(node, DependencyManifest):
        return (
            "'dependency-manifest', $idf_project_slug, $idf_manifest_path",
            {"idf_project_slug": node.project_slug, "idf_manifest_path": node.manifest_path},
        )
    if isinstance(node, DependencySnapshot):
        return (
            "'dependency-snapshot', $idf_project_slug, $idf_manifest_path, $idf_commit_sha",
            {
                "idf_project_slug": node.project_slug,
                "idf_manifest_path": node.manifest_path,
                "idf_commit_sha": node.commit_sha,
            },
        )
    if isinstance(node, DependencyChange):
        # version_source/change_kind/observed_at are deliberately excluded from
        # identity — re-ingesting the same (manifest, package, commit) must
        # always address the same node regardless of which enrichment source
        # resolved it or when it was (re)observed (DEPG-DIFF-005).
        return (
            "'dependency-change', $idf_project_slug, $idf_manifest_path, "
            "$idf_package_purl, $idf_commit_sha",
            {
                "idf_project_slug": node.project_slug,
                "idf_manifest_path": node.manifest_path,
                "idf_package_purl": node.package_purl,
                "idf_commit_sha": node.commit_sha,
            },
        )
    raise ValueError(f"No idFrom() scheme for node type {type(node).__name__}")
