from __future__ import annotations
from typing import Any, Literal, TypeVar
from modok.quine.models import QuineNode

T = TypeVar("T", bound=QuineNode)

QuineNodeId = int


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
    def __init__(self, base_url: str) -> None:
        raise NotImplementedError

    async def upsert_node(self, node: QuineNode) -> None:
        raise NotImplementedError

    async def get_node(self, node_id: QuineNodeId, node_type: type[T]) -> T:
        raise NotImplementedError

    async def node_exists(self, node_id: QuineNodeId) -> bool:
        raise NotImplementedError

    async def write_edge(self, from_id: QuineNodeId, edge_type: str, to_id: QuineNodeId) -> None:
        raise NotImplementedError

    async def edge_exists(self, from_id: QuineNodeId, edge_type: str, to_id: QuineNodeId) -> bool:
        raise NotImplementedError

    async def traverse(self, start_id: QuineNodeId, steps: list[TraversalStep]) -> list[QuineNode]:
        raise NotImplementedError

    async def query(self, cypher: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def ping(self) -> bool:
        raise NotImplementedError
