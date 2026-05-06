"""
Scenario loader — parses YAML scenario files into Scenario dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from modok.quine.ids import idFrom
from modok.quine.models import QuineNode, _NODE_TYPE_MAP


@dataclass
class NodeRecord:
    node_id: int
    node: QuineNode


@dataclass
class EdgeRecord:
    from_id: int
    edge_type: str
    to_id: int


@dataclass
class QuerySpec:
    issue_id: int
    project_slug: str


@dataclass
class ExpectedSpec:
    known_issues_must_include: list[str] = field(default_factory=list)
    recent_fixes_must_include: list[str] = field(default_factory=list)
    relevant_files_must_include: list[str] = field(default_factory=list)


@dataclass
class Scenario:
    name: str
    nodes: list[NodeRecord]
    edges: list[EdgeRecord]
    query: QuerySpec
    expected: ExpectedSpec


def _id_tuple_to_int(parts: list[str]) -> int:
    """Convert a YAML id tuple like [Feature, stagehand, shtp] to an idFrom int."""
    node_type_key = parts[0]
    rest = [str(p) for p in parts[1:]]
    type_to_prefix = {
        "CustomerIssue": "customer-issue",
        "Feature": "feature",
        "Module": "module",
        "File": "file",
        "DocSection": "doc-section",
        "ErrorSignature": "error",
        "KnownIssue": "known-issue",
        "SimilarityMatch": "similarity-match",
        "Fix": "fix",
        "ResolutionEvent": "resolution",
        "DiagnosticNote": "diagnostic-note",
    }
    prefix = type_to_prefix.get(node_type_key, node_type_key.lower())
    return idFrom(prefix, *rest)


def _build_node(raw: dict[str, Any]) -> tuple[int, QuineNode]:
    """Build a QuineNode from a raw YAML dict, return (node_id, node)."""
    node_type = raw["type"]
    fields = {k: v for k, v in raw.items() if k != "type"}
    fields["node_type"] = node_type
    model_cls = _NODE_TYPE_MAP[node_type]
    node = model_cls(**fields)
    from tests.hifi.dummy_quine.client import _node_id_from_model_hifi

    node_id = _node_id_from_model_hifi(node)
    return node_id, node


# @spec HFI-LOAD-001, HFI-LOAD-002, HFI-LOAD-003
def load_scenario(yaml_path: Path) -> Scenario:
    data = yaml.safe_load(yaml_path.read_text())

    # Parse nodes — compute IDs once at load time
    nodes: list[NodeRecord] = []
    for raw in data.get("nodes", []):
        node_id, node = _build_node(raw)
        nodes.append(NodeRecord(node_id=node_id, node=node))

    # Parse edges — resolve symbolic tuples to precomputed integer IDs
    edges: list[EdgeRecord] = []
    for raw in data.get("edges", []) or []:
        from_id = _id_tuple_to_int(raw["from"])
        to_id = _id_tuple_to_int(raw["to"])
        edges.append(EdgeRecord(from_id=from_id, edge_type=raw["type"], to_id=to_id))

    # Parse query
    issue_parts = data["query"]["issue"]
    issue_id = _id_tuple_to_int(issue_parts)
    query = QuerySpec(issue_id=issue_id, project_slug=data["query"]["project_slug"])

    # Parse expected
    exp_raw = data.get("expected", {})
    ki_must = exp_raw.get("known_issues", {}).get("must_include", []) or []
    fix_must = exp_raw.get("recent_fixes", {}).get("must_include", []) or []
    file_must = exp_raw.get("relevant_files", {}).get("must_include", []) or []
    expected = ExpectedSpec(
        known_issues_must_include=ki_must,
        recent_fixes_must_include=fix_must,
        relevant_files_must_include=file_must,
    )

    return Scenario(
        name=data["name"],
        nodes=nodes,
        edges=edges,
        query=query,
        expected=expected,
    )
