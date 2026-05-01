# Diagnostic Retrieval Engine

## Context and Design Philosophy

The Diagnostic Retrieval Engine (DRE) is MODOK's read path. Given a `CustomerIssue` node ID, it extracts anchors from the graph, traverses Quine to find related nodes, and returns a debug packet.

The DRE is strictly read-only. It writes nothing to Quine. Its only output is the debug packet returned to the caller.

Two rules govern the DRE:

**Graph-first anchors.** Anchors are read from validated graph edges on the `CustomerIssue` node. The LLM gateway is a fallback, invoked only when graph anchors are insufficient. This preserves ingestion as the source of truth and makes retrieval deterministic and fast in the common case.

**Weighted match count prioritization.** Items matched by more anchors appear first. The count is weighted: similarity matches are worth more than anchor traversals, and confirmed matches are worth more than candidates. This is honest about the signal — not all matches are equal — while remaining calibration-free and deterministic.

## Interface

```python
async def retrieve(
    issue_id: QuineNodeId,
    project_slug: str,
    backend: str = "local",
) -> DebugPacket
```

`issue_id` must be the ID of an existing `CustomerIssue` node. The caller is responsible for ingesting the issue before calling `retrieve`. The DRE does not accept raw text — anchor extraction and node creation are ingestion responsibilities.

`project_slug` is required. The DRE verifies that the fetched `CustomerIssue` node's `project_slug` matches the argument before traversal. If they differ, raises `DRENotFoundError`.

`backend` is forwarded to the LLM gateway if the fallback path is needed. Defaults to `"local"`.

## Debug Packet Schema

```python
@dataclass
class AnchorSet:
    feature_slugs: list[str]        # from graph edges; may be empty
    error_signatures: list[str]     # from graph edges; may be empty
    symptoms: list[str]             # informational only; not used in traversal

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
    anchor_type: str        # "feature", "error_signature"
    anchor_value: str
    matched_node_ids: list[str]

@dataclass
class DebugPacket:
    issue_summary: str
    anchors: AnchorSet
    anchor_count: int                    # total anchor instances used; callers use this to interpret confidence
    known_issues: list[KnownIssueRef]   # sorted descending by match_count; max 10
    recent_fixes: list[FixRef]           # sorted descending by match_count; max 10
    relevant_files: list[FileRef]        # sorted descending by match_count; max 20
    evidence: list[EvidenceAnchor]
    confidence: float                    # see Confidence below
```

Sections with no matches are returned as empty lists, not omitted.

`environment` and `relevant_tests` are omitted from v1. No graph node type maps to environment anchors. No `Test` node type exists yet.

## Anchor Extraction

### Graph-first (primary path)

The DRE fetches the `CustomerIssue` node, then reads its outbound edges:

```cypher
MATCH (ci:CustomerIssue) WHERE id(ci) = $issue_id
MATCH (ci)-[:AFFECTS]->(f:Feature {project_slug: $project_slug})
RETURN f.feature_slug
```

```cypher
MATCH (ci:CustomerIssue) WHERE id(ci) = $issue_id
MATCH (ci)-[:HAS_ERROR]->(e:ErrorSignature {project_slug: $project_slug})
RETURN e.normalized_error
```

Anchors are considered sufficient if at least one feature slug or one error signature is found. If sufficient, the LLM fallback is skipped entirely.

### LLM fallback

If no graph anchors are found and `raw_text` is present on the `CustomerIssue` node, the DRE calls `gateway.parse_ticket(raw_text, project_slug, backend=backend)` and uses the returned `feature_slug` and `error_signatures` as anchors.

If `raw_text` is `None` and no graph anchors exist, raises `DREAnchorError`.

If `parse_ticket` raises `LLMResponseError`, raises `DREAnchorError`.

If `parse_ticket` raises `LLMUnavailableError`, raises `DRELLMUnavailableError`.

`symptoms` from the LLM result are included in `AnchorSet.symptoms` for context but are not used in graph traversal or match count.

## Graph Traversal

Each anchor drives a traversal. Results are collected, deduplicated by node ID, and scored by match count across all traversals.

### Feature anchor → files

For each `feature_slug` in anchors:

```cypher
MATCH (f:Feature {project_slug: $project_slug, feature_slug: $feature_slug})
MATCH (f)-[:IMPLEMENTED_BY]->(m:Module)-[:DEFINED_IN]->(file:File)
RETURN file
```

Files found here contribute to `relevant_files`.

### Error signature anchor → known issues

For each `normalized_error` in anchors:

```cypher
MATCH (e:ErrorSignature {project_slug: $project_slug, normalized_error: $normalized_error})
MATCH (e)<-[:HAS_ERROR]-(ki:KnownIssue)
RETURN ki
```

`KnownIssue` nodes found here contribute to `known_issues`.

### Known issue → fixes

For each `KnownIssue` found above:

```cypher
MATCH (ki:KnownIssue) WHERE id(ki) = $ki_id
MATCH (ki)-[:RESOLVED_BY]->(fix:Fix)
RETURN fix
```

`Fix` nodes found here contribute to `recent_fixes`. Each `KnownIssue -[:RESOLVED_BY]-> Fix` hop that fires increments the `Fix` node's `match_count` by 1. If multiple `KnownIssue` nodes resolve to the same `Fix`, their contributions are summed — a `Fix` reached by two `KnownIssue` nodes gets `match_count = 2`.

### Pre-computed similarity

```cypher
MATCH (ci:CustomerIssue) WHERE id(ci) = $issue_id
MATCH (ci)-[:HAS_SIMILARITY_MATCH]->(sm:SimilarityMatch)-[:MATCHES]->(ki:KnownIssue)
WHERE sm.review_status IN ['candidate', 'confirmed']
RETURN ki, sm.review_status
```

`KnownIssue` nodes reached via `confirmed` matches get `match_count += 2`; via `candidate` matches get `match_count += 1`. `rejected` matches are excluded. If a `KnownIssue` was already found via error signature traversal, its match count accumulates.

All traversals use `QuineClient.query()`. The DRE does not use `QuineClient.traverse()` — multi-hop patterns with inline `project_slug` filtering cannot be expressed through the `TraversalStep` abstraction without extension.

## Weighted Match Count and Prioritization

Each result item accumulates a weighted `match_count` as traversals complete:

- First time an item appears via anchor traversal: `match_count = 1`
- Each additional anchor that also reaches the same item: `match_count += 1`
- `Fix` reached by N `KnownIssue` nodes: `match_count = N` (summed across hops)
- `KnownIssue` reached via `confirmed` SimilarityMatch: `match_count += 2`
- `KnownIssue` reached via `candidate` SimilarityMatch: `match_count += 1`

The similarity weights reflect signal strength: a confirmed match is a validated fact worth more than a raw anchor hit; a candidate is worth as much as one anchor. All contributions accumulate with no upper bound other than the result cap.

After all traversals, each result list is sorted descending by `match_count`. Items with equal `match_count` preserve insertion order (first-found). Each list is capped before return:

- `known_issues`: max 10
- `recent_fixes`: max 10
- `relevant_files`: max 20

## Confidence

```
confidence = matched_anchor_instances / total_anchor_instances
```

Where an "anchor instance" is one feature slug or one error signature string. Each instance that produced at least one result in any traversal counts as matched.

If no anchors were extracted (impossible after the anchor extraction step, but defensive): `confidence = 0.0`.

Example: 1 feature slug + 3 error signatures = 4 instances. If 1 feature match and 2 error matches produced results: `confidence = 3/4 = 0.75`.

## Error Types

```python
class DREError(Exception): pass
class DRENotFoundError(DREError): pass          # CustomerIssue node not found, or project_slug mismatch
class DREAnchorError(DREError): pass            # no graph anchors and LLM fallback failed or unavailable
class DREGraphUnavailableError(DREError): pass  # Quine unreachable
class DRELLMUnavailableError(DREError): pass    # LLM gateway unreachable (fallback path only)
```

## Project Isolation

`project_slug` is passed explicitly to `retrieve()` and verified against the fetched `CustomerIssue` node before any traversal begins. Every Cypher query includes `project_slug` as a property filter. The DRE never derives `project_slug` from graph state.

## Eventual Consistency

The DRE reads whatever edges are currently in Quine. If ingestion has not yet run `replace_edges` after a metadata change, the DRE may return stale relationships. This is accepted in v1. The ingestion pipeline is responsible for edge reconciliation; the DRE documents the dependency on correct ingestion behavior.

## ID Scheme

The DRE creates no nodes. It reads `CustomerIssue`, `Feature`, `Module`, `File`, `ErrorSignature`, `KnownIssue`, `Fix`, and `SimilarityMatch` nodes. IDs are resolved by Quine traversal; the DRE does not call `idFrom` directly.

## Decisions & Alternatives

| Decision | Chosen | Alternatives Considered | Rationale |
|---|---|---|---|
| Input is node ID, not raw text | `CustomerIssue` node ID required | Accept raw text; accept either | Ingestion and retrieval are separate responsibilities; node-ID-only keeps the DRE read-only and stateless |
| Graph-first anchors | Read from `AFFECTS` / `HAS_ERROR` edges; LLM only as fallback | Always use LLM; always use graph | Graph edges are validated facts; LLM re-parsing every call is slow and redundant when ingestion already wrote the structure |
| LLM fallback on missing graph anchors | Call `parse_ticket` if no graph anchors found | Always error; always succeed with empty anchors | A `CustomerIssue` with no anchors and no raw text is genuinely ambiguous; surfacing the error is honest |
| `environment` omitted from v1 | Not in AnchorSet | Included; mapped to ObservationEvent | No graph node type maps to environment anchors; including it implies traversal support that doesn't exist |
| `symptoms` informational only | In AnchorSet but not scored | Used in traversal; omitted entirely | Symptoms are useful context for the consuming agent but no node type maps to them; keeping them in the packet is honest |
| Weighted match count | Integer accumulation with fixed per-source weights | Float weights calibrated on data; tier enum; pure unweighted count | Fixed weights (confirmed=+2, candidate=+1, anchor=+1) are honest about signal strength without requiring calibration; extends to tuned weights later |
| `confirmed` SimilarityMatch gets +2, `candidate` gets +1 | Differentiated by review status | All matches equal; exclude candidates | Confirmed matches are validated facts worth more than a raw anchor hit; candidates are uncertain but still a signal |
| Result caps | 10 / 10 / 20 | No cap; configurable cap | Common error signatures can fan out to hundreds of KnownIssues; caps prevent overwhelming output without losing the most relevant items |
| `RESOLVED_BY` for fix retrieval | `KnownIssue -[:RESOLVED_BY]-> Fix` | Via `ResolutionEvent` | `Fix` nodes are the general-purpose fix record; `ResolutionEvent` records specific real-world applications, not needed in the retrieval path |
| Raw Cypher via `query()` | `QuineClient.query()` for all traversals | `QuineClient.traverse()` | Multi-hop patterns with inline `project_slug` filtering cannot be expressed through `TraversalStep` without extension |
| Stale edges accepted | Document eventual consistency | DRE validates edge freshness | Edge reconciliation is ingestion's responsibility; the DRE cannot know when edges were written without timestamps |
| Split error types for graph vs LLM | `DREGraphUnavailableError`, `DRELLMUnavailableError` | Single `DREUnavailableError` | Callers need to distinguish: graph failure means no retrieval possible; LLM failure means graph-only retrieval may still be attempted |

## Open Questions & Future Decisions

### Deferred

1. **Weighted scoring** — match count is the v1 signal. Per-anchor-type weights (error signature > feature) are the natural next step if retrieval quality needs improvement.
2. **Recency boost** — `Fix` nodes carry no timestamp in v1. Requires a timestamp field on `Fix` or a linked `ResolutionEvent` with a `resolved_at` field. Deferred until `ResolutionEvent` ingestion is built.
3. **Vector index recall** — deferred per design review §4.4 until graph-only retrieval is proven. When added, vector candidates would be injected into traversal results before match-count sorting.
4. **`relevant_tests`** — add when a `Test` node type and `HAS_TEST` edges are introduced in the ingestion pipeline.
5. **Anchor caching** — `parse_ticket` in the fallback path re-parses raw text on every `retrieve()` call. This is intentional: anchors derived from LLM output are not written back to Quine, so each call repeats the LLM work. The repeat cost is accepted in v1. When it becomes a measured problem, the fix is to write the derived anchors back as `AFFECTS`/`HAS_ERROR` edges via the ingestion pipeline (making them graph-first on the next call). The DRE does not write these itself — writes belong to ingestion.
6. **Configurable result caps** — current caps (10/10/20) are hardcoded. If callers need different limits, a `max_results` parameter can be added without changing the core logic.
7. **`ResolutionEvent` in fix retrieval** — if showing only general `Fix` nodes proves insufficient (e.g., callers need to know which fixes were applied to real tickets), traverse `ResolutionEvent` as a secondary fix source.

## References

- `docs/high-level-design.md §System Design` — DRE role and debug packet concept
- `docs/llds/llm-gateway.md` — `parse_ticket` interface and error types
- `docs/llds/quine-client.md` — `query()` and `get_node()` interfaces; `replace_edges` for ingestion-side edge reconciliation
- `docs/llds/ingestion-pipeline.md` — upstream pipeline that creates `CustomerIssue` nodes and writes `AFFECTS` / `HAS_ERROR` edges
