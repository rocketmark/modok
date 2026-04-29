# Quine Client

## Context and Design Philosophy

The Quine client is the lowest layer of MODOK. Every other component — ingestion, retrieval, MCP, CLI — writes and reads the graph through this layer. Nothing talks to Quine directly except the Quine client.

Its job is narrow: provide a typed, multi-project-aware interface to Quine's HTTP/WebSocket API, hiding Quine's wire format behind MODOK's node and edge vocabulary. It does not know about docs, tickets, or debug packets. It knows about nodes, edges, IDs, and Cypher queries.

Guiding principles:
- **Deterministic IDs everywhere.** Node identity is computed, not generated. The same logical entity always maps to the same Quine node ID, so ingestion is idempotent.
- **No broad property scans.** All reads follow explicit edge traversals or ID lookups. Full-graph scans are not exposed.
- **Multi-project namespace from day one.** `project_slug` is a required argument in every ID-generating call. There is no global namespace.
- **Fail loudly.** A write that references an ID that doesn't exist, or a read that returns an unexpected shape, raises — it does not silently return None or an empty result.

## ID Scheme

All Quine node IDs are deterministic, derived by hashing a tuple of typed string components. The `idFrom()` function is the sole ID-generation mechanism in MODOK.

```python
def idFrom(*parts: str) -> QuineNodeId:
    """
    Compute a deterministic Quine node ID from an ordered tuple of strings.
    Parts are joined with a null byte separator before hashing to prevent
    collisions between ('a', 'bc') and ('ab', 'c').
    """
```

ID tuples by node type:

| Node type | ID tuple |
|---|---|
| `Project` | `('project', project_slug)` |
| `ProductArea` | `('product-area', project_slug, area_slug)` |
| `Feature` | `('feature', project_slug, feature_slug)` |
| `Module` | `('module', project_slug, module_slug)` |
| `File` | `('file', project_slug, repo_path)` |
| `Doc` | `('doc', project_slug, doc_path)` |
| `DocSection` | `('doc-section', project_slug, doc_path, heading_slug)` |
| `TestPlan` | `('test-plan', project_slug, plan_slug)` |
| `TestCase` | `('test-case', project_slug, plan_slug, case_slug)` |
| `KnownIssue` | `('known-issue', project_slug, issue_id)` |
| `CustomerIssue` | `('customer-issue', source_system, ticket_id)` |
| `ErrorSignature` | `('error', project_slug, normalized_error)` |
| `Fix` | `('fix', project_slug, fix_id)` |
| `ResolutionEvent` | `('resolution', project_slug, ticket_id, fix_id)` |
| `Decision` | `('decision', project_slug, decision_id)` |
| `Risk` | `('risk', project_slug, risk_id)` |
| `FailureMode` | `('failure-mode', project_slug, feature_slug, mode_id)` |
| `ObservationEvent` | `('observation', project_slug, source, event_id)` |
| `SimilarityMatch` | `('similarity-match', customer_issue_id, known_issue_id, method)` |
| `DiagnosticNote` | `('diagnostic-note', project_slug, note_id)` |
| `DeploymentEvent` | `('deployment', project_slug, service_name, version, deployed_at)` |

`CustomerIssue` does not carry `project_slug` in its ID because tickets arrive from external systems (Zendesk, GitHub Issues, etc.) before they are linked to a project. The `source_system` + `ticket_id` pair is globally unique.

## Node Schema

Each node type is a pydantic model. The client serializes to/from Quine's property map format.

Core node types and their required properties:

```python
class Project(QuineNode):
    node_type: Literal["Project"]
    project_slug: str
    name: str

class Feature(QuineNode):
    node_type: Literal["Feature"]
    project_slug: str
    feature_slug: str
    name: str
    product_area_slug: str | None = None

class Module(QuineNode):
    node_type: Literal["Module"]
    project_slug: str
    module_slug: str
    name: str

class File(QuineNode):
    node_type: Literal["File"]
    project_slug: str
    repo_path: str

class DocSection(QuineNode):
    node_type: Literal["DocSection"]
    project_slug: str
    doc_path: str
    heading_slug: str
    heading_text: str
    doc_type: str          # "hld" | "lld" | "testing" | "runbook" | "known-issue" | ...
    line_start: int | None = None
    line_end: int | None = None

class ErrorSignature(QuineNode):
    node_type: Literal["ErrorSignature"]
    project_slug: str
    normalized_error: str
    display_text: str

class KnownIssue(QuineNode):
    node_type: Literal["KnownIssue"]
    project_slug: str
    issue_id: str
    summary: str
    status: str            # "open" | "resolved" | "wont-fix"

class CustomerIssue(QuineNode):
    node_type: Literal["CustomerIssue"]
    source_system: str
    ticket_id: str
    summary: str
    raw_text: str | None = None   # freeform, not indexed in Quine
    status: str            # "open" | "resolved" | "duplicate"

class SimilarityMatch(QuineNode):
    node_type: Literal["SimilarityMatch"]
    method: str            # "graph-anchor" | "vector" | "hybrid" | "manual"
    score: float
    evidence_anchors: list[str]   # list of node IDs that form the evidence
    review_status: str     # "candidate" | "confirmed" | "rejected"

class Fix(QuineNode):
    node_type: Literal["Fix"]
    project_slug: str
    fix_id: str
    summary: str
    kind: str              # "code-fix" | "config-change" | "workaround" | "patch"

class ResolutionEvent(QuineNode):
    node_type: Literal["ResolutionEvent"]
    project_slug: str
    ticket_id: str
    fix_id: str
    resolved_at: str       # ISO 8601

class DiagnosticNote(QuineNode):
    node_type: Literal["DiagnosticNote"]
    project_slug: str
    note_id: str
    body: str
    source: str            # "agent" | "human"
    created_at: str        # ISO 8601
```

## Edge Types

Edges are typed string labels. The client exposes `write_edge(from_id, edge_type, to_id)` and `get_neighbors(node_id, edge_type, direction)`. No edge carries properties — if a relationship needs metadata, it uses an intermediate node (e.g., `SimilarityMatch`).

Canonical edge vocabulary:

```
Project         -[:HAS_PRODUCT_AREA]->  ProductArea
Project         -[:HAS_FEATURE]->       Feature
Project         -[:HAS_DOC]->           Doc
Project         -[:HAS_FILE]->          File

ProductArea     -[:HAS_FEATURE]->       Feature

Feature         -[:PART_OF]->           ProductArea
Feature         -[:IMPLEMENTED_BY]->    Module
Feature         -[:DESCRIBED_BY]->      DocSection
Feature         -[:TESTED_BY]->         TestPlan
Feature         -[:HAS_RISK]->          Risk
Feature         -[:HAS_FAILURE_MODE]->  FailureMode

Module          -[:DEFINED_IN]->        File
Module          -[:COVERED_BY]->        TestPlan

KnownIssue      -[:AFFECTS]->           Feature
KnownIssue      -[:HAS_ERROR]->         ErrorSignature
KnownIssue      -[:RESOLVED_BY]->       Fix
KnownIssue      -[:DESCRIBED_BY]->      DocSection

CustomerIssue   -[:HAS_ERROR]->         ErrorSignature
CustomerIssue   -[:AFFECTS]->           Feature
CustomerIssue   -[:HAS_SIMILARITY_MATCH]-> SimilarityMatch
CustomerIssue   -[:INSTANCE_OF]->       KnownIssue
CustomerIssue   -[:RESOLVED_BY]->       ResolutionEvent

SimilarityMatch -[:MATCHES]->           KnownIssue

Fix             -[:CHANGED]->           File
Fix             -[:VERIFIED_BY]->       TestCase

FailureMode     -[:AFFECTS]->           Feature
FailureMode     -[:HAS_ERROR]->         ErrorSignature
FailureMode     -[:RELEVANT_FILE]->     File
FailureMode     -[:RELEVANT_TEST]->     TestCase

ResolutionEvent -[:APPLIED_FIX]->       Fix

ObservationEvent -[:OBSERVED]->         ErrorSignature
ObservationEvent -[:AFFECTS_FEATURE]->  Feature
```

## Client Interface

The client is a single class, `QuineClient`, instantiated with a base URL and optional auth config. All methods are async.

```python
class QuineClient:
    def __init__(self, base_url: str, auth: QuineAuth | None = None): ...

    # Node operations
    async def upsert_node(self, node: QuineNode) -> None: ...
    async def get_node(self, node_id: QuineNodeId, node_type: type[T]) -> T: ...
    async def node_exists(self, node_id: QuineNodeId) -> bool: ...

    # Edge operations
    async def write_edge(self, from_id: QuineNodeId, edge_type: str, to_id: QuineNodeId) -> None: ...
    async def get_neighbors(
        self,
        node_id: QuineNodeId,
        edge_type: str,
        direction: Literal["out", "in", "both"] = "out",
    ) -> list[QuineNodeId]: ...
    async def edge_exists(self, from_id: QuineNodeId, edge_type: str, to_id: QuineNodeId) -> bool: ...

    # Traversal
    async def traverse(self, start_id: QuineNodeId, steps: list[TraversalStep]) -> list[QuineNodeId]: ...

    # Cypher escape hatch (for retrieval engine use only; not exposed via MCP)
    async def query(self, cypher: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...

    # Health
    async def ping(self) -> bool: ...
```

`upsert_node` writes the node if it doesn't exist and updates properties if it does. It never deletes existing edges. This makes ingestion idempotent — re-running the doc ingestor over the same doc updates stale properties without creating duplicate nodes or orphaned edges.

`traverse` is a structured alternative to raw Cypher for common multi-hop patterns used by the retrieval engine. A `TraversalStep` is `(edge_type, direction, optional_node_type_filter)`.

The raw `query` escape hatch is available for the retrieval engine's complex traversals. It is not exposed via MCP to agents.

## Wire Format

Quine's HTTP API accepts and returns JSON. Node properties are a flat `{ key: value }` map. Node IDs in MODOK are 64-bit integers derived by taking the first 8 bytes of the SHA-256 hash of the null-byte-joined parts tuple.

```python
def idFrom(*parts: str) -> int:
    digest = hashlib.sha256("\x00".join(parts).encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)
```

Quine uses signed 64-bit integers for node IDs. The conversion uses `signed=True` to stay within Quine's accepted range.

Property serialization: pydantic models are serialized via `.model_dump()`, then the `node_type` field is stored as a Quine property alongside all other fields. On read, `node_type` is used to dispatch to the correct pydantic model for deserialization.

## Connection and Retry

The client wraps `httpx.AsyncClient`. Connection parameters:

- `base_url`: Quine HTTP endpoint (e.g., `http://localhost:8080`)
- `timeout`: default 10s per request
- `retries`: 3 attempts with exponential backoff for transient HTTP errors (5xx, timeout)
- No retry on 4xx (client errors are bugs, not transients)

The client does not manage Quine's lifecycle. Quine is started externally (Docker or JAR) before MODOK runs.

## Decisions & Alternatives

| Decision | Chosen | Alternatives Considered | Rationale |
|---|---|---|---|
| ID derivation | SHA-256, first 8 bytes, signed int64 | UUID v5 (namespace+name), sequential int, Quine auto-ID | Deterministic, collision-resistant, idempotent ingestion, fits Quine's signed int64 ID space |
| No edge properties | Intermediate nodes for metadata | Edge properties in Quine | Quine's edge model does not support rich properties; intermediate nodes (e.g., `SimilarityMatch`) make metadata queryable |
| Upsert semantics | Update properties, never delete edges | Replace node, merge | Idempotent ingestion without destructive side effects on partial re-runs |
| Async client | `httpx.AsyncClient` | `requests` (sync), `aiohttp` | `httpx` supports both sync and async, has a clean test-double story (`httpx.MockTransport`), and is the modern choice for Python async HTTP |
| Raw Cypher escape hatch | Exposed internally, not via MCP | Fully abstracted, no raw Cypher | Retrieval engine needs complex traversals; hiding Cypher from agents prevents injection risk while preserving internal power |

## Open Questions & Future Decisions

### Resolved
1. ✅ Multi-project namespace — `project_slug` in every ID tuple from day one.
2. ✅ `CustomerIssue` ID excludes `project_slug` — tickets arrive before project linkage is known.
3. ✅ No edge properties — use intermediate nodes for relationship metadata.

### Deferred
1. **Quine authentication** — Quine's auth model in production (shared Mac mini). Currently `QuineAuth | None`; implementation deferred until shared deployment.
2. **Batch write API** — ingestion of large doc trees may benefit from batched node/edge writes. Quine's batch endpoint needs evaluation against the upsert-per-node approach for throughput.
3. **Standing queries** — Quine supports standing queries for stream-mode event matching. The client interface does not expose these in v1; add when stream mode begins.
4. **ID collision probability** — 8 bytes of SHA-256 gives a ~50% collision probability at ~4 billion nodes. Fine for MODOK's data volumes; revisit if the graph grows beyond ~10M nodes.

## References

- Quine HTTP API docs: https://docs.quine.io/reference/rest-api.html
- Quine Cypher reference: https://docs.quine.io/reference/cypher/
- `docs/high-level-design.md` — system context
