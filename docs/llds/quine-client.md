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

Quine node IDs are UUIDs. By default Quine uses its UUID ID provider; MODOK does not configure an alternative. Node addresses are never generated as random values — they are always computed deterministically using Quine's built-in `idFrom()` Cypher function.

`idFrom()` is a Quine Cypher function that accepts one or more string arguments and returns a UUID that is deterministic for those inputs: the same arguments always produce the same UUID, so ingestion is idempotent by construction. It is embedded directly in Cypher query strings — MODOK never computes Quine node addresses in Python.

The node type name is always the first argument to `idFrom()`. This guarantees that two node types with identical remaining parts produce different addresses. The canonical addressing pattern in Cypher is:

```cypher
MATCH (n) WHERE id(n) = idFrom('feature', $project_slug, $feature_slug)
SET n += {node_type: 'Feature', project_slug: $project_slug, feature_slug: $feature_slug, name: $name}
```

`SimilarityMatch` includes `project_slug` as an `idFrom()` argument despite being a computed node — without it, two projects whose `CustomerIssue` and `KnownIssue` nodes hash identically would silently share a `SimilarityMatch` node across project boundaries, violating isolation.

`idFrom()` arguments by node type (first argument is always the type prefix):

| Node type | `idFrom()` arguments |
|---|---|
| `Project` | `'project', project_slug` |
| `ProductArea` | `'product-area', project_slug, area_slug` |
| `Feature` | `'feature', project_slug, feature_slug` |
| `Module` | `'module', project_slug, module_slug` |
| `File` | `'file', project_slug, repo_path` |
| `Doc` | `'doc', project_slug, doc_path` |
| `DocSection` | `'doc-section', project_slug, doc_path, heading_slug` |
| `TestPlan` | `'test-plan', project_slug, plan_slug` |
| `TestCase` | `'test-case', project_slug, plan_slug, case_slug` |
| `KnownIssue` | `'known-issue', project_slug, issue_id` |
| `CustomerIssue` | `'customer-issue', project_slug, source_system, ticket_id` |
| `ErrorSignature` | `'error', project_slug, normalized_error` |
| `Fix` | `'fix', project_slug, fix_id` |
| `ResolutionEvent` | `'resolution', project_slug, source_system, ticket_id, fix_id` |
| `Decision` | `'decision', project_slug, decision_id` |
| `Risk` | `'risk', project_slug, risk_id` |
| `FailureMode` | `'failure-mode', project_slug, feature_slug, mode_id` |
| `ObservationEvent` | `'observation', project_slug, source, event_id` |
| `SimilarityMatch` | `'similarity-match', project_slug, customer_issue_id, known_issue_id, method` |
| `DiagnosticNote` | `'diagnostic-note', project_slug, note_id` |
| `DeploymentEvent` | `'deployment', project_slug, service_name, version, deployed_at` |

`CustomerIssue` carries `project_slug` in its `idFrom()` arguments. Tickets are always ingested in the context of a known project — the ingestion CLI requires `--project`. Cross-project ticket ID collisions (same `source_system` + `ticket_id` in two projects) are therefore possible and must be namespaced by `project_slug`.

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
    project_slug: str
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
    source_system: str     # mirrors CustomerIssue — needed to disambiguate ticket_id across systems
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
    # raises QuineNodeNotFoundError if the node does not exist
    # use node_exists() when absence is a valid expected condition
    async def node_exists(self, node_id: QuineNodeId) -> bool: ...

    # Edge operations
    # Idempotent — writing the same edge twice is always a no-op.
    # Client guards with edge_exists() if Quine does not deduplicate natively.
    async def write_edge(self, from_id: QuineNodeId, edge_type: str, to_id: QuineNodeId) -> None: ...
    async def get_neighbors(
        self,
        node_id: QuineNodeId,
        edge_type: str,
        direction: Literal["out", "in", "both"] = "out",
    ) -> list[QuineNodeId]: ...
    async def edge_exists(self, from_id: QuineNodeId, edge_type: str, to_id: QuineNodeId) -> bool: ...

    # Traversal — returns hydrated nodes (properties included) in one round-trip.
    # Quine's Cypher endpoint returns properties inline; no separate fetch needed.
    async def traverse(self, start_id: QuineNodeId, steps: list[TraversalStep]) -> list[QuineNode]: ...
    # TraversalStep is (edge_type, direction). No node_type_filter — filtering by node
    # type is done in the Cypher WHERE clause when needed (retrieval engine uses query()
    # directly for complex traversals; traverse() handles simple multi-hop patterns).

    # Cypher escape hatch (for retrieval engine use only; not exposed via MCP)
    async def query(self, cypher: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...

    # Health
    async def ping(self) -> bool: ...
```

`upsert_node` writes the node if it doesn't exist and sets all current model properties if it does — using Cypher `SET n.field = $value` for each field in the pydantic model. Properties that were present on a prior write and have since been removed from the schema are **not** deleted; they persist as ghost properties until the node is explicitly replaced. This is accepted in v1 — MODOK schemas are stable and property removal is rare. If ghost properties become a problem, the fix is a fetch-then-replace pattern (MATCH, SET all current fields, REMOVE all others). `upsert_node` never touches edges; edges are written separately via `write_edge` and are never deleted by `upsert_node`.

`traverse` is a structured alternative to raw Cypher for common multi-hop patterns. A `TraversalStep` is `(edge_type, direction)`. There is no `node_type_filter` parameter — callers that need type-filtered traversals use the `query()` escape hatch with an explicit Cypher WHERE clause.

The raw `query` escape hatch is available for the retrieval engine's complex traversals. It is not exposed via MCP to agents.

## Wire Format

Quine's HTTP API accepts and returns JSON. Node properties are a flat `{ key: value }` map. Node IDs are UUIDs managed by Quine's UUID ID provider.

Node addressing uses Quine's built-in `idFrom()` Cypher function, which is embedded directly in query strings. MODOK never computes or stores Quine node IDs in Python — all ID resolution happens inside Quine at query execution time.

**Upsert pattern:**
```cypher
MATCH (n) WHERE id(n) = idFrom('feature', $project_slug, $feature_slug)
SET n += {node_type: 'Feature', project_slug: $project_slug, feature_slug: $feature_slug, name: $name}
```

**Edge write pattern:**
```cypher
MATCH (a) WHERE id(a) = idFrom('feature', $project_slug, $feature_slug)
MATCH (b) WHERE id(b) = idFrom('module', $project_slug, $module_slug)
MERGE (a)-[:IMPLEMENTED_BY]->(b)
```

**Query endpoint:** `POST /api/v1/query/cypher` with body `{"text": "<cypher>", "parameters": {...}}`. The field name is `text`, not `query`.

Property serialization: pydantic models are serialized via `.model_dump()`, then the `node_type` field is stored as a Quine property alongside all other fields. On read, `node_type` is used to dispatch to the correct pydantic model for deserialization.

## Connection and Retry

The client wraps `httpx.AsyncClient`. Connection parameters:

- `base_url`: Quine HTTP endpoint (e.g., `http://localhost:8080`)
- `timeout`: default 10s **per attempt** (not per total operation — each retry gets a fresh 10s window)
- `retries`: 3 attempts with exponential backoff for transient HTTP errors (5xx, timeout)
- No retry on 4xx (client errors are bugs, not transients)

**Edge-before-node writes are permitted.** Writing an edge to a node that doesn't yet exist in Quine creates a shell node. This is valid intermediate state during ingestion — doc sections reference features before features are written, depending on parse order. `upsert_node` promotes shell nodes to full nodes when called. The "fail loudly" principle applies to reads (missing nodes on `get_node` raise), not to edge writes.

**Ghost properties are a known limitation of `upsert_node`.** The Cypher `SET` clause writes current model fields but does not remove properties that were present on a prior write and have since been removed from the schema. A node that was written with an old schema version may carry extra properties indefinitely. This is accepted in v1 — MODOK's schemas are stable and property removal is rare. If ghost properties become a problem, the fix is a fetch-then-replace pattern (read current properties, SET all current fields, REMOVE all others).

**`replace_edges` is the reconciliation primitive for authoritative relationships.** When ingestion re-processes a source (doc, ticket, registry entry), it calls `replace_edges(from_id, edge_type, to_ids)` to delete all existing edges of that type from the source node and recreate only the current set. This prevents stale edges from accumulating when metadata changes. Ingestion callers are responsible for knowing which edge types they own; `replace_edges` is never called speculatively.

**No atomicity guarantee across sequential calls.** The client does not guarantee that `node_exists()` followed by `get_node()` is atomic. Callers that need check-then-act behavior must call `get_node` directly and handle `QuineNodeNotFoundError`. At MODOK's single-writer sequential ingestion model this is not a practical concern, but the contract is explicit.

The client does not manage Quine's lifecycle. Quine is started externally (Docker or JAR) before MODOK runs.

## Decisions & Alternatives

| Decision | Chosen | Alternatives Considered | Rationale |
|---|---|---|---|
| ID derivation | Quine's built-in `idFrom()` Cypher function; node type name is always first argument; embedded inline in Cypher strings; MODOK never computes node addresses in Python | SHA-256 int64 (rejected — wrong ID type; Quine uses UUIDs), UUID v5 in Python (rejected — requires Python UUID generation vs. Quine-native), sequential int, Quine auto-ID (rejected — non-deterministic) | `idFrom()` is deterministic and UUID-native; ingestion is idempotent by construction; no Python-side ID computation means no risk of Python/Quine ID mismatch; type-as-first-argument guarantees two node types with identical remaining parts never collide |
| `traverse` return type | Hydrated `list[QuineNode]` | IDs only + per-ID `get_node` fetches | Quine's Cypher endpoint (`POST /api/v1/query/cypher`) returns node properties inline in the same response — no second round-trip needed |
| No edge properties | Intermediate nodes for metadata | Edge properties in Quine | Quine's edge model does not support rich properties; intermediate nodes (e.g., `SimilarityMatch`) make metadata queryable |
| Upsert semantics | SET current fields only, never touch edges | Full node replace (remove old properties too), merge | SET is sufficient for v1 — schemas are stable and property removal is rare. Ghost properties are accepted; the fetch-then-replace pattern is the documented fix if they become a problem. Full node replace would require a MATCH+DELETE+RECREATE sequence, losing edges unless carefully reconstructed. |
| `get_node` on missing ID | Raise `QuineNodeNotFoundError` | Return `None` | A missing node is almost always a bug in the ingestion pipeline, not a normal condition; `node_exists()` is the opt-in check for callers that expect absence |
| `write_edge` on duplicate | No-op (idempotent) | Raise on duplicate | Ingestion runs are repeatable by design; raising on duplicate would break every re-ingest |
| Async client | `httpx.AsyncClient` | `requests` (sync), `aiohttp` | `httpx` supports both sync and async, has a clean test-double story (`httpx.MockTransport`), and is the modern choice for Python async HTTP |
| Raw Cypher escape hatch | Exposed internally, not via MCP | Fully abstracted, no raw Cypher | Retrieval engine needs complex traversals; hiding Cypher from agents prevents injection risk while preserving internal power |

## Open Questions & Future Decisions

### Resolved
1. ✅ Multi-project namespace — `project_slug` in every ID tuple from day one.
2. ✅ `CustomerIssue` ID includes `project_slug` — ingestion always has project context; same `source_system` + `ticket_id` can appear in multiple projects and must be namespaced.
3. ✅ No edge properties — use intermediate nodes for relationship metadata.
4. ✅ `upsert_node` uses SET for current fields; ghost properties from removed schema fields are accepted in v1. Fetch-then-replace is the documented fix if needed.
5. ✅ Node type name is always first `idFrom()` argument — type collisions impossible by construction.
6. ✅ `traverse` returns hydrated nodes — Quine's Cypher endpoint returns properties inline, no second round-trip.
7. ✅ `get_node` on missing ID raises `QuineNodeNotFoundError` — `node_exists()` for opt-in absence checks.
8. ✅ `write_edge` is idempotent — duplicate writes are always no-ops.
9. ✅ `ResolutionEvent` ID includes `source_system` — disambiguates ticket IDs across source systems.

### Deferred
1. **Quine authentication** — Quine's auth model in production (shared Mac mini). Currently `QuineAuth | None`; implementation deferred until shared deployment.
2. **Batch write API** — ingestion of large doc trees may benefit from batched node/edge writes. Quine's batch endpoint needs evaluation against the upsert-per-node approach for throughput.
3. **Standing queries** — Quine supports standing queries for stream-mode event matching. The client interface does not expose these in v1; add when stream mode begins.
4. **`idFrom()` collision probability** — Quine's `idFrom()` uses UUID-space (128-bit); collision probability at MODOK's data volumes is negligible. No action required unless Quine changes its ID provider.

## References

- Quine HTTP API docs: https://docs.quine.io/reference/rest-api.html
- Quine Cypher reference: https://docs.quine.io/reference/cypher/
- `docs/high-level-design.md` — system context
