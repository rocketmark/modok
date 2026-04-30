# Diagnostic Retrieval Engine

## Context and Design Philosophy

The Diagnostic Retrieval Engine (DRE) is MODOK's read path. Given a `CustomerIssue` node ID, it extracts anchors from the issue's raw text, traverses Quine to find related nodes, and returns a debug packet.

The DRE is strictly read-only. It writes nothing to Quine. Its only output is the debug packet returned to the caller.

One rule governs prioritization: **items matched by more anchors appear first**. No numeric scoring, no calibration, no weights. Match count is a concrete, unambiguous signal that extends naturally to weighted scoring later without changing the interface.

## Interface

```python
async def retrieve(
    issue_id: QuineNodeId,
    project_slug: str,
    backend: str = "local",
) -> DebugPacket
```

`issue_id` must be the ID of an existing `CustomerIssue` node. The caller is responsible for ingesting the issue before calling `retrieve`. The DRE does not accept raw text — anchor extraction and node creation are ingestion responsibilities.

`project_slug` is required and used in every Quine query. The DRE never traverses cross-project.

`backend` is forwarded to the LLM gateway for anchor extraction. Defaults to `"local"`.

## Debug Packet Schema

```python
@dataclass
class AnchorSet:
    feature_slug: str | None
    error_signatures: list[str]
    environment: dict[str, str]
    symptoms: list[str]

@dataclass
class KnownIssueRef:
    known_issue_id: str
    summary: str
    status: str
    match_count: int        # number of anchors that pointed to this item

@dataclass
class FixRef:
    fix_id: str
    summary: str
    kind: str
    match_count: int

@dataclass
class FileRef:
    repo_path: str
    match_count: int

@dataclass
class EvidenceAnchor:
    anchor_type: str        # "feature", "error_signature", "environment"
    anchor_value: str
    matched_node_ids: list[str]

@dataclass
class DebugPacket:
    issue_summary: str
    anchors: AnchorSet
    known_issues: list[KnownIssueRef]   # sorted descending by match_count
    recent_fixes: list[FixRef]           # sorted descending by match_count
    relevant_files: list[FileRef]        # sorted descending by match_count
    evidence: list[EvidenceAnchor]
    confidence: float                    # matched_anchors / total_anchors; 0.0 if no anchors
```

Sections with no matches are returned as empty lists, not omitted. A caller can distinguish "nothing found" from "field not returned."

`relevant_tests` is omitted — no `Test` node type exists yet.

## Anchor Extraction

The DRE fetches the `CustomerIssue` node from Quine. It calls `gateway.parse_ticket(raw_text, project_slug)` to extract anchors.

If `raw_text` is `None` or `parse_ticket` raises `LLMResponseError`, the DRE raises `DREAnchorError`. The caller decides whether to retry or surface the error. The DRE does not silently proceed with no anchors — an anchor-less traversal returns an empty packet with no diagnostic value.

If `parse_ticket` raises `LLMUnavailableError`, the DRE raises `DREUnavailableError`.

## Graph Traversal

Each anchor type drives a separate traversal. Results from all traversals are collected, deduplicated, and scored by match count before assembly.

### Feature anchor

```cypher
MATCH (f:Feature {project_slug: $project_slug, feature_slug: $feature_slug})
MATCH (f)-[:HAS_FILE]->(file)
RETURN file
```

Files found here contribute to `relevant_files`.

### Error signature anchor

For each error string in `error_signatures`:

```cypher
MATCH (e:ErrorSignature {project_slug: $project_slug, normalized_error: $normalized_error})
MATCH (e)-[:LINKED_TO]->(ki:KnownIssue)
RETURN ki
```

`KnownIssue` nodes found here contribute to `known_issues`.

### Known issue → fix

For each `KnownIssue` found above:

```cypher
MATCH (ki:KnownIssue) WHERE id(ki) = $ki_id
MATCH (ki)-[:FIXED_BY]->(fix:Fix)
RETURN fix
```

`Fix` nodes found here contribute to `recent_fixes`.

### Pre-computed similarity

```cypher
MATCH (ci:CustomerIssue) WHERE id(ci) = $issue_id
MATCH (ci)-[:HAS_SIMILARITY_MATCH]->(sm:SimilarityMatch)-[:MATCHES]->(ki:KnownIssue)
RETURN ki
```

Any `KnownIssue` reached this way also contributes to `known_issues` with `match_count` incremented for the overlap.

All traversals are performed via `QuineClient.query()`. The DRE uses raw Cypher through `query()` for traversals — not `traverse()` — because the multi-hop patterns require inline filtering by `project_slug`.

## Match Count and Prioritization

Each result item accumulates a `match_count` as traversals complete:

- First time an item appears: `match_count = 1`
- Each additional anchor that also points to the same item: `match_count += 1`

After all traversals, each result list is sorted `descending by match_count`. Items with equal `match_count` preserve insertion order (first-found).

`confidence = len([a for a in anchors if a produced at least one result]) / total_anchors`

If no anchors were extracted, `confidence = 0.0`.

## Error Types

```python
class DREError(Exception): pass
class DRENotFoundError(DREError): pass      # CustomerIssue node not found in Quine
class DREAnchorError(DREError): pass        # anchor extraction failed
class DREUnavailableError(DREError): pass   # Quine or LLM gateway unreachable
```

## Project Isolation

Every Cypher query includes `project_slug` as a property filter. The `project_slug` argument to `retrieve()` is never derived from the graph — it is always passed explicitly by the caller. This prevents a cross-project traversal even if two projects share node IDs (which deterministic hashing makes unlikely but not impossible).

## ID Scheme

The DRE creates no nodes. It reads `CustomerIssue`, `Feature`, `File`, `ErrorSignature`, `KnownIssue`, `Fix`, and `SimilarityMatch` nodes. IDs are resolved by Quine traversal; the DRE does not call `idFrom` directly.

## Decisions & Alternatives

| Decision | Chosen | Alternatives Considered | Rationale |
|---|---|---|---|
| Input is node ID, not raw text | `CustomerIssue` node ID required | Accept raw text; accept either | Ingestion and retrieval are separate responsibilities; node-ID-only keeps the DRE read-only and avoids duplicating ingestion logic |
| Anchor extraction via LLM gateway | `parse_ticket` called at retrieve time | Store parsed anchors on node at ingest; skip LLM in DRE | Anchors may change as the model improves; re-parsing at retrieve time keeps them fresh without a migration |
| Match count, not numeric scoring | Integer count of anchors matched | Float weights; tier enum | Match count is a concrete signal requiring no calibration; extends to weighted scoring by multiplying per-anchor weights later |
| `DREAnchorError` on extraction failure | Hard exception; caller decides | Proceed with empty anchors; log and degrade | An anchor-less traversal returns an empty packet with no diagnostic value; surfacing the failure is more honest |
| `relevant_tests` omitted | Not in schema | Included as empty list | No `Test` node type exists; an empty list implies the field exists but returned nothing, which is misleading |
| Raw Cypher via `query()` | `QuineClient.query()` for all DRE traversals | `QuineClient.traverse()` | Multi-hop patterns require inline `project_slug` filtering; `traverse()` abstraction cannot express this without extension |
| Pre-computed similarity included | Traverse `HAS_SIMILARITY_MATCH` if present | Similarity only via anchor extraction | Pre-computed matches are validated graph facts; ignoring them wastes prior work |

## Open Questions & Future Decisions

### Deferred

1. **Weighted scoring** — match count is the v1 signal. If retrieval quality needs improvement, per-anchor-type weights (error signature > feature > environment) are the natural next step. Match count becomes the base; weights are multipliers.
2. **Recency boost** — `Fix` nodes carry no timestamp in v1. A recency signal (days since fix) requires either a timestamp field on `Fix` or a linked `ResolutionEvent`. Deferred until `ResolutionEvent` ingestion is built.
3. **Vector index recall** — design review §4.4 defers vector index until graph-only retrieval is proven. When added, vector candidates would be injected into the traversal results before match-count sorting.
4. **`relevant_tests`** — add when `Test` node type and `HAS_TEST` edges are introduced in the ingestion pipeline.
5. **Anchor caching** — re-parsing raw text via LLM on every `retrieve()` call is correct but slow. Caching parsed anchors on the `CustomerIssue` node (as a property or linked node) would eliminate the LLM call for repeat retrievals. Deferred until latency is measured as a problem.

## References

- `docs/high-level-design.md §System Design` — DRE role and debug packet concept
- `docs/llds/llm-gateway.md` — `parse_ticket` interface
- `docs/llds/quine-client.md` — `query()` and `get_node()` interfaces
- `docs/llds/static-ingestion.md` — upstream pipeline that creates `CustomerIssue` nodes
