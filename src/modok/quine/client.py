from __future__ import annotations

import json
import asyncio
from typing import Any, Literal, TypeVar

import httpx

from modok.quine.errors import QuineNodeNotFoundError, QuineDeserializationError
from modok.quine.models import QuineNode, _NODE_TYPE_MAP

T = TypeVar("T", bound=QuineNode)

QuineNodeId = int

_RETRY_STATUSES = {500, 502, 503, 504}
_MAX_RETRIES = 3
_DEFAULT_TIMEOUT = 10.0
_BACKOFF_BASE = 0.5  # seconds; doubles each retry


class TraversalStep:
    def __init__(
        self,
        edge_type: str,
        direction: Literal["out", "in", "both"] = "out",
        node_type_filter: str | None = None,
    ) -> None:
        self.edge_type = edge_type
        self.direction = direction
        self.node_type_filter = node_type_filter


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

    async def _cypher(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        # @spec QC-CN-001, QC-CN-002, QC-CN-003
        payload = {"query": query, "parameters": params or {}}
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
                            await asyncio.sleep(_BACKOFF_BASE * (2 ** attempt))
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    return data.get("results", [])
                except httpx.TimeoutException as exc:
                    last_exc = exc
                    if attempt < _MAX_RETRIES - 1:
                        await asyncio.sleep(_BACKOFF_BASE * (2 ** attempt))

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
            raise QuineDeserializationError(
                f"Unknown node_type '{node_type}' on node id={node_id}"
            )
        try:
            return model_cls(**props)
        except Exception as exc:
            raise QuineDeserializationError(
                f"Failed to deserialize node id={node_id} type={node_type}: {exc}"
            ) from exc

    # @spec QC-NW-001, QC-NW-002, QC-NW-003, QC-NW-004
    async def upsert_node(self, node: QuineNode) -> None:
        props = node.model_dump()
        node_id = _node_id_from_model(node)
        # MERGE on id, then SET replaces all properties (full replace, not merge).
        # No relationship clause — edges are never touched here.
        set_clause = ", ".join(f"n.{k} = ${k}" for k in props)
        query = f"MERGE (n) WHERE id(n) = $node_id SET {set_clause}"
        await self._cypher(query, {"node_id": node_id, **props})

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
        return bool(results)

    # @spec QC-EW-001, QC-EW-002, QC-EW-003
    async def write_edge(
        self, from_id: QuineNodeId, edge_type: str, to_id: QuineNodeId
    ) -> None:
        # MERGE on both endpoints and the relationship — idempotent by construction.
        # Edge-before-node writes are permitted; Quine creates shell nodes for
        # referenced IDs that don't yet have properties.
        query = (
            "MERGE (a) WHERE id(a) = $from_id "
            "MERGE (b) WHERE id(b) = $to_id "
            f"MERGE (a)-[:{edge_type}]->(b)"
        )
        await self._cypher(query, {"from_id": from_id, "to_id": to_id})

    async def edge_exists(
        self, from_id: QuineNodeId, edge_type: str, to_id: QuineNodeId
    ) -> bool:
        results = await self._cypher(
            f"MATCH (a)-[r:{edge_type}]->(b) "
            "WHERE id(a) = $from_id AND id(b) = $to_id RETURN r",
            {"from_id": from_id, "to_id": to_id},
        )
        return bool(results)

    # @spec QC-TR-001
    async def traverse(
        self, start_id: QuineNodeId, steps: list[TraversalStep]
    ) -> list[QuineNode]:
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


def _node_id_from_model(node: QuineNode) -> QuineNodeId:
    from modok.quine.ids import idFrom
    from modok.quine.models import (
        Project, Feature, Module, File, DocSection, ErrorSignature,
        KnownIssue, CustomerIssue, SimilarityMatch, Fix, ResolutionEvent,
        DiagnosticNote,
    )
    if isinstance(node, Project):
        return idFrom("project", node.project_slug)
    if isinstance(node, Feature):
        return idFrom("feature", node.project_slug, node.feature_slug)
    if isinstance(node, Module):
        return idFrom("module", node.project_slug, node.module_slug)
    if isinstance(node, File):
        return idFrom("file", node.project_slug, node.repo_path)
    if isinstance(node, DocSection):
        return idFrom("doc-section", node.project_slug, node.doc_path, node.heading_slug)
    if isinstance(node, ErrorSignature):
        return idFrom("error", node.project_slug, node.normalized_error)
    if isinstance(node, KnownIssue):
        return idFrom("known-issue", node.project_slug, node.issue_id)
    if isinstance(node, CustomerIssue):
        return idFrom("customer-issue", node.source_system, node.ticket_id)
    if isinstance(node, SimilarityMatch):
        return idFrom("similarity-match", node.project_slug, node.customer_issue_id,
                      node.known_issue_id, node.method)
    if isinstance(node, Fix):
        return idFrom("fix", node.project_slug, node.fix_id)
    if isinstance(node, ResolutionEvent):
        return idFrom("resolution", node.project_slug, node.source_system,
                      node.ticket_id, node.fix_id)
    if isinstance(node, DiagnosticNote):
        return idFrom("diagnostic-note", node.project_slug, node.note_id)
    raise ValueError(f"No ID scheme for node type {type(node).__name__}")
