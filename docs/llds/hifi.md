# HiFi Test Harness

## Context and Design Philosophy

HiFi is a differential test suite that verifies MODOK's ingest and retrieval semantics independently of Quine. It answers one question:

> Given the same world and the same debug query, does real MODOK produce the same meaningful debug packet as the reference model?

The mechanism: inject a `DummyQuine` in place of `QuineClient`. DummyQuine stores every write (nodes, edges) in memory and answers reads from that in-memory store. No Quine process, no HTTP, no Cypher engine. MODOK's production code is exercised unmodified; only the client it receives changes.

This keeps failures actionable:
- HiFi failure → bug in MODOK ingestion or retrieval logic.
- Quine contract test failure → bug in the QuineClient adapter, query shape, or Quine itself.

HiFi does not test Quine persistence, Cypher correctness, RocksDB behavior, or graph engine performance.

## What DummyQuine Is

DummyQuine is a drop-in replacement for `QuineClient`. It implements the same async method signatures:

```python
async def upsert_node(node: QuineNode) -> None
async def get_node(node_id: int, node_type: type[T]) -> T
async def node_exists(node_id: int) -> bool
async def write_edge(from_id: int, edge_type: str, to_id: int) -> None
async def replace_edges(from_id: int, edge_type: str, to_ids: list[int]) -> None
async def edge_exists(from_id: int, edge_type: str, to_id: int) -> bool
async def traverse(start_id: int, steps: list[TraversalStep]) -> list[QuineNode]
async def query(cypher: str, params: dict | None = None) -> list[dict]
async def ping() -> bool
```

It does not subclass `QuineClient`. Python duck typing is sufficient — callers (`run_ingestion`, `retrieve`) accept any object with this interface. No `GraphStore` ABC is introduced; that abstraction is unnecessary complexity for the test scope.

### Internal state

```python
_nodes: dict[int, QuineNode]          # node_id → QuineNode instance
_edges: list[tuple[int, str, int]]    # (from_id, edge_type, to_id)
```

That's the entire store. No secondary indexes are maintained eagerly — lookups scan when needed. The graph sizes in HiFi scenarios are small (tens of nodes, hundreds of edges at most); scan cost is irrelevant.

`replace_edges(from_id, edge_type, to_ids)` removes all tuples `(from_id, edge_type, *)` from `_edges` then appends new tuples for each `to_id`. This is the only destructive mutation.

`ping()` always returns `True` — DummyQuine is always "running".

## The Hard Problem: `query()`

The production DRE uses `client.query()` with raw Cypher strings exclusively — not `traverse()`. All five DRE traversal functions (`_graph_anchors`, `_traverse_feature_to_files`, `_traverse_error_to_known_issues`, `_traverse_ki_to_fixes`, `_traverse_similarity`) call `client.query()` with specific Cypher templates and return structured row lists.

DummyQuine cannot execute arbitrary Cypher. Instead, it implements a **query template registry**: a fixed mapping from recognized Cypher patterns to Python functions that walk `_nodes` and `_edges`.

### Template matching

Each DRE query has a distinctive shape. DummyQuine matches on a stable prefix or keyword fingerprint — not full Cypher parsing — and dispatches to the corresponding Python traversal:

| DRE call | Fingerprint | DummyQuine behavior |
|---|---|---|
| `_graph_anchors` (features) | `"AFFECTS]->(f:Feature"` | Walk `_edges` for `(issue_id, AFFECTS, *)`, filter nodes of type `Feature` with matching `project_slug`, return `feature_slug` property rows |
| `_graph_anchors` (errors) | `"HAS_ERROR]->(e:ErrorSignature"` | Walk `_edges` for `(issue_id, HAS_ERROR, *)`, filter `ErrorSignature` nodes |
| `_traverse_feature_to_files` | `"IMPLEMENTED_BY]->(m:Module)-[:DEFINED_IN]->(file:File)"` | Two-hop walk: feature→module→file |
| `_traverse_error_to_known_issues` | `"HAS_ERROR]-(ki:KnownIssue)"` | **Reverse** walk: scan `_edges` for tuples `(*, HAS_ERROR, error_node_id)` — the edge direction is KnownIssue→ErrorSignature, so this query follows edges *inbound* to the ErrorSignature |
| `_traverse_ki_to_fixes` | `"RESOLVED_BY]->(fix:Fix"` | Walk `_edges` for `(ki_node_id, RESOLVED_BY, *)`, filter `Fix` with matching `project_slug` |
| `_traverse_similarity` | `"HAS_SIMILARITY_MATCH]->(sm:SimilarityMatch)-[:MATCHES]->(ki:KnownIssue"` | Two-hop walk: issue→SimilarityMatch→KnownIssue, filter `review_status` |
| `recall` command (arbitrary) | fallback | Return empty list; callers that use `query()` for recall are tested separately |

If no fingerprint matches, `query()` returns an empty list and emits a warning. This makes unrecognised queries fail visibly without crashing the harness.

### Row format

The DRE expects `query()` rows in the Quine wire format:

```python
[{"id": node_id, "properties": {...}}]
```

DummyQuine constructs this format from its `_nodes` store when returning node results. For scalar returns (e.g. `RETURN f.feature_slug`), it returns the value directly as the row element.

## Reference Model

The reference model (`ReferenceModok`) is a small, independent implementation of MODOK's expected ingest and retrieval contract. It shares no implementation code with production MODOK except stable public schemas (`QuineNode` models, `DebugPacket`, `IngestionReport`).

Purpose: encode the contract, not the algorithm. If the reference model and real MODOK disagree, the bug is in one of them — and the reference model is deliberately simple enough that its output can be reasoned about by inspection.

### Reference ingest

`ReferenceModok.ingest(doc_path, frontmatter)` stores parsed entities in plain Python dicts. It applies the same ID scheme (`idFrom`) and the same edge rules described in the ingestion LLD, but without registry validation, LLM proposals, or error recovery. It is wrong to be incomplete; it should be right about what it does implement.

### Reference retrieval

`ReferenceModok.retrieve(issue_id, project_slug)` walks its in-memory entity store and assembles a `DebugPacket` using the same anchor extraction and traversal logic described in the DRE LLD, implemented directly without Cypher. It applies the same ranking rules (match count descending) and the same caps (`_KI_CAP=10`, `_FIX_CAP=10`, `_FILE_CAP=20`).

### What the reference model does not implement

- LLM anchor extraction (raw_text fallback). HiFi scenarios always supply graph anchors directly.
- SimilarityMatch traversal. Deferred to a later HiFi layer.
- Registry validation. The reference model accepts any well-formed frontmatter.
- `replace_edges` semantics on re-ingest. The reference model processes each scenario from a clean state.

These exclusions are intentional. The reference model covers the happy path for graph-anchored retrieval — the primary MODOK use case.

## Test Layers

### Layer 1: Golden scenarios

Hand-authored YAML fixtures, one per important case. Each scenario specifies inputs (docs with frontmatter, customer issues, edges to write) and the expected debug packet properties.

Minimal first set (five scenarios):
1. `feature_to_files` — CustomerIssue with AFFECTS edge → Feature → Module → File; expect files in packet.
2. `error_to_known_issue` — CustomerIssue with HAS_ERROR edge → ErrorSignature ← KnownIssue; expect KI in packet.
3. `known_issue_to_fix` — KnownIssue with RESOLVED_BY edge → Fix; expect Fix in packet.
4. `idempotent_reingest` — ingest same doc twice; expect one set of nodes/edges in DummyQuine (no duplicates).
5. `cross_project_isolation` — two projects with same feature slug; expect only project-local results in packet.

### Layer 2: Property tests

Generate random but valid MODOK worlds (N features, M known issues, K docs, P customer issues with edges into the graph) and assert structural invariants:

- Every node written has a stable ID matching `idFrom(...)`.
- Re-ingesting the same input produces the same set of `upsert_node` calls.
- Every node ID in a returned `DebugPacket` exists in DummyQuine's `_nodes`.
- No cross-project node appears in a packet for a different project.

### Layer 3: Metamorphic tests

Transformations that should not change the result:
- Input document order is shuffled.
- A duplicate ingest event is appended.
- An unrelated-project doc is added.

Transformations that should change the result:
- A new `Fix` node is linked to an existing `KnownIssue`.
- A `CustomerIssue` gains an additional `AFFECTS` edge.
- A `KnownIssue` status changes to `resolved`.

## Comparison Rules

Do not compare debug packets byte-for-byte. Compare semantic properties:

**Required:**
- Every ID in `expected.known_issues` is present in `actual.known_issues`.
- Every ID in `expected.recent_fixes` is present in `actual.recent_fixes`.
- Every path in `expected.relevant_files` is present in `actual.relevant_files`.
- IDs in `expected.must_not_include` are absent from all packet sections.
- `actual.confidence > 0` when at least one anchor matched.

**Not required (ordering):**
- Exact position of items within a list is not asserted unless the scenario specifically tests ranking.

Helper assertions:
```python
assert_in_packet(actual, section="known_issues", id="KI-001")
assert_not_in_packet(actual, id="KI-unrelated")
assert_confidence_positive(actual)
```

## Scenario Format

Scenarios are YAML files in `tests/hifi/scenarios/`. Each scenario drives both the reference model and the real MODOK + DummyQuine:

```yaml
name: error_to_known_issue

nodes:
  - type: CustomerIssue
    project_slug: stagehand
    source_system: zendesk
    ticket_id: "1001"
    summary: "SHTP version mismatch"

  - type: ErrorSignature
    project_slug: stagehand
    normalized_error: "shtp_version_mismatch"

  - type: KnownIssue
    project_slug: stagehand
    issue_id: "KI-001"
    summary: "SHTP v1/v2 version mismatch causes tracker dropout"
    status: open

edges:
  - from: [CustomerIssue, stagehand, zendesk, "1001"]
    type: HAS_ERROR
    to: [ErrorSignature, stagehand, shtp_version_mismatch]

  - from: [KnownIssue, stagehand, KI-001]
    type: HAS_ERROR
    to: [ErrorSignature, stagehand, shtp_version_mismatch]

query:
  issue: [CustomerIssue, stagehand, zendesk, "1001"]
  project_slug: stagehand

expected:
  known_issues:
    must_include: [KI-001]
  recent_fixes:
    must_include: []
  confidence_positive: true
```

Node and edge references use the same `idFrom` tuple form — the harness computes the integer IDs at load time. The harness writes all nodes to DummyQuine before writing any edges, so `get_node` calls during edge setup never fail on missing nodes.

## Module Layout

```
tests/hifi/
    __init__.py
    scenarios/
        feature_to_files.yaml
        error_to_known_issue.yaml
        known_issue_to_fix.yaml
        idempotent_reingest.yaml
        cross_project_isolation.yaml
    dummy_quine/
        __init__.py
        client.py          # DummyQuine class
        query_dispatch.py  # fingerprint → Python traversal mapping
    reference/
        __init__.py
        model.py           # ReferenceModok.ingest(), .retrieve()
    harness/
        __init__.py
        loader.py          # load_scenario(yaml_path) → Scenario
        runner.py          # run_scenario(scenario) → (expected, actual)
        compare.py         # assert_in_packet(), assert_not_in_packet(), etc.
    test_golden.py
    test_properties.py
    test_metamorphic.py
```

## Decisions & Alternatives

| Decision | Chosen | Alternatives Considered | Rationale |
|---|---|---|---|
| DummyQuine interface | Duck-type `QuineClient` API, no ABC | `GraphStore` ABC; subclass `QuineClient` | No production refactor required. ABC adds a layer that benefits only HiFi. Subclassing `QuineClient` would inherit HTTP machinery unnecessarily. |
| `query()` implementation | Fingerprint dispatch to Python traversals | Full Cypher parser; per-test mock returns | Full parser is heavy and fragile. Per-test mocks are what we already have in unit tests — HiFi adds value by exercising the real DRE against a real in-memory graph, not another layer of mocks. Fingerprint dispatch is narrow but covers the actual DRE call set. |
| Reference model scope | Happy-path graph-anchored retrieval only | Full parity with production MODOK | The reference model encodes the contract, not every edge case. LLM fallback, similarity traversal, and re-ingest semantics are deferred — they can be added as HiFi expands. Full parity defeats the purpose: a complex reference model is as likely to contain bugs as the real code. |
| Scenario format | YAML with `idFrom` tuple node/edge references | Python fixtures; JSON | YAML is readable and diffable. `idFrom` tuples keep scenarios honest about the ID scheme without hardcoding magic integers. Python fixtures couple scenarios to test framework internals. |
| Comparison style | Semantic (`must_include` / `must_not_include`) | Exact byte-for-byte match | Debug packet ordering is not fully deterministic (e.g. two nodes with equal match counts). Semantic comparison catches real bugs without brittleness. |
| Layer 1 scenario count | 5 golden scenarios for v1 | More coverage up front | Five scenarios prove the harness shape. Property tests provide breadth. Adding golden scenarios is cheap once the harness runs. |

## Open Questions & Future Decisions

### Deferred

1. **SimilarityMatch traversal in reference model** — the reference model currently skips `_traverse_similarity`. Add a golden scenario and reference model implementation once SimilarityMatch ingestion is built.
2. **LLM anchor fallback coverage** — HiFi scenarios always provide graph anchors. Testing the raw_text → LLM parse → anchor path requires either a mock LLM or a real one. Add a Layer 1 scenario with a mock LLM gateway once the LLM gateway mock is stabilized.
3. **Re-ingest `replace_edges` semantics** — the reference model processes each scenario from a clean state. Testing that re-ingest correctly removes stale edges requires a multi-step scenario. Add to Layer 3 metamorphic tests.
4. **Performance bounds** — HiFi does not assert timing. If scenarios grow large enough that DummyQuine's scan cost matters, add secondary indexes. Not a v1 concern.
5. **Quine contract tests** — separate from HiFi (`tests/contract/`). Verify that `RealQuineClient` and `DummyQuine` agree on the graph interface. Requires a live Quine instance. Tracked in quine-client specs as `[C]`-annotated specs.

## References

- `docs/hifi-brainstorm.md` — original design brainstorm
- `docs/llds/diagnostic-retrieval-engine.md` — DRE traversal functions that DummyQuine must serve
- `docs/llds/ingestion-pipeline.md` — ingestion entry point exercised by HiFi
- `docs/llds/quine-client.md` — QuineClient API that DummyQuine mirrors
- `src/modok/quine/client.py` — the interface DummyQuine implements
- `src/modok/retrieval/engine.py` — the five `client.query()` call sites DummyQuine must handle
