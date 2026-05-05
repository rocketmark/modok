# Diagnostic Retrieval Engine

## Context and Design Philosophy

The Diagnostic Retrieval Engine (DRE) is MODOK's read path. Given a `CustomerIssue` node ID, it extracts anchors from the graph, traverses Quine to find related nodes, scores candidate files by evidence, and returns a debug packet.

The DRE is strictly read-only. It writes nothing to Quine. Its only output is the debug packet returned to the caller.

Two rules govern the DRE:

**Graph-first anchors.** Anchors are read from validated graph edges on the `CustomerIssue` node. The LLM gateway is a fallback, invoked only when graph anchors are insufficient. This preserves ingestion as the source of truth and makes retrieval deterministic and fast in the common case.

**Evidence-weighted file scoring.** Files accumulate evidence items from multiple independent signals (graph traversal, element matching, recent commits, function matching). Each signal type contributes once per type to a file's score, with diminishing returns for repeated signals of the same type. Diversity of evidence is rewarded.

## Interface

```python
async def retrieve(
    issue_id: str,
    project_slug: str,
    client: QuineClient,
    backend: str = "local",
    valid_slugs: list[str] | None = None,
    feature_slugs: list[str] | None = None,
    module_slugs: list[str] | None = None,
    feature_descriptions: dict[str, str] | None = None,
    module_descriptions: dict[str, str] | None = None,
    module_elements: dict[str, list[str]] | None = None,
    module_source_files: dict[str, list[str]] | None = None,
    on_progress: Callable[[str, DebugPacket], None] | None = None,
) -> DebugPacket
```

`issue_id` must be the ID of an existing `CustomerIssue` node. The caller is responsible for ingesting the issue before calling `retrieve`.

`project_slug` is required and verified against the fetched node before any traversal.

`backend` is forwarded to the LLM gateway if the fallback path is needed.

`valid_slugs`, `feature_slugs`, `module_slugs`, `feature_descriptions`, `module_descriptions` are forwarded to `gateway.parse_ticket` on the LLM fallback path; they guide the LLM toward valid slug values.

`module_elements` — maps module slug → list of registered element names (UI signals, emitted events, named components). Used for element anchor matching.

`module_source_files` — maps module slug → list of source file paths for that module. Used for element anchor matching and pre-matching.

`on_progress` — optional streaming callback called at two points:
- Before LLM anchor extraction: `on_progress("loading", partial_packet)` — lets the caller show the ticket title immediately.
- After traversal but before LLM summary: `on_progress("partial", partial_packet)` — lets the caller show candidates while the summary is being generated.

## Debug Packet Schema

```python
class IssueSummary(BaseModel):
    summary: str
    anchors: IssueAnchors

class IssueAnchors(BaseModel):
    features: list[str]     # resolved feature slugs
    errors: list[str]       # error signature strings
    symptoms: list[str]     # symptom strings (informational; not used in traversal)

class AffectedArea(BaseModel):
    type: str               # "feature" or "module"
    id: str                 # e.g. "feature:tracker" or "module:device-card"
    name: str               # the slug

class EvidenceItem(BaseModel):
    type: str               # see Evidence Sources section
    score: float
    explanation: str

class ScoredCandidate(BaseModel):
    path: str               # repo-relative file path
    kind: str               # "source" or "test"
    score: float
    confidence: str         # "high", "medium", or "low"
    evidence: list[EvidenceItem]

class KnownIssueRef(BaseModel):
    id: str
    summary: str

class PriorFix(BaseModel):
    id: str
    commit: str | None
    summary: str

class RecentCommit(BaseModel):
    sha: str
    timestamp: str
    author_name: str
    message: str
    files_touched: list[str]

class DebugPacket(BaseModel):
    issue: IssueSummary
    affected_areas: list[AffectedArea]
    relevant_files: list[str]       # paths from source_candidates, ordered by score
    relevant_tests: list[str]       # paths from test_candidates, ordered by score
    known_issues: list[KnownIssueRef]
    prior_fixes: list[PriorFix]
    recent_commits: list[RecentCommit]
    scored_candidates: list[ScoredCandidate]
    summary: str                    # one-sentence LLM summary; "" if generation fails
```

Sections with no matches are returned as empty lists.

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

If at least one feature slug or error signature is found, the LLM fallback is skipped entirely.

### LLM fallback

If no graph anchors are found and `raw_text` is present on the `CustomerIssue` node:

1. **Mechanical pre-match** — `_pre_match_modules(raw_text, module_source_files, module_elements)` checks two signals independently:
   - Literal source file path mentions (e.g. `agent/src/main.c` appears verbatim in the text).
   - Element-name token overlap: words from `raw_text` are tokenized (camelCase/snake_case split, length > 2), and any module whose element name's token set is a subset of those tokens is matched. Example: ticket text containing `tracker_lost_logged` → tokens `{tracker, lost, logged}` → matches any module with an element `tracker_lost_logged`.
2. **LLM augmentation** — `gateway.parse_ticket(raw_text, ...)` is called to validate the pre-matched candidates and identify anything the mechanical pass missed. It returns `feature_slugs`, `error_signatures`, `symptoms`, and `mentioned_files`.
   - On `LLMResponseError` (bad output): fall back to pre-match results only; `error_signatures`, `symptoms`, and `mentioned_files` are empty.
   - On `LLMUnavailableError`: raises `DRELLMUnavailableError`.
3. **Merge** — pre-matched slugs are unioned with LLM-returned slugs (pre-match first, deduped).
4. **Mechanical validation pass** — all merged slugs are filtered against `valid_slugs` before any Quine traversal. This prevents hallucinated slugs from reaching the graph.
5. `symptoms` are stored for context but not used in graph traversal. `mentioned_files` are seeded directly into evidence maps as `ticket_mention` items.

If `raw_text` is `None` and no graph anchors exist, raises `DREAnchorError`.

## Graph Traversal

### Feature/Module anchor → files

For each slug in `feature_slugs` (which may be either Feature or Module slugs):

1. Try Feature traversal: `Feature -[:IMPLEMENTED_BY]-> Module -[:DEFINED_IN]-> File`
2. Also fetch: `Feature -[:HAS_TEST]-> TestFile`
3. If no files found via Feature, fall back to Module traversal: `Module -[:DEFINED_IN]-> File` and walk up to parent Feature for test files.
4. Files from step 1 or 3 get `feature_anchor` evidence (score 7.0). Test files get `test_coverage` evidence (score 8.0).
5. `resolved_as` is `"feature"` or `"module"` and is used to build `affected_areas`.

### Error signature anchor → known issues

For each `normalized_error` in `error_sigs`:

```cypher
MATCH (e:ErrorSignature {project_slug: $project_slug, normalized_error: $normalized_error})
MATCH (e)<-[:HAS_ERROR]-(ki:KnownIssue)
RETURN ki
```

`KnownIssue` nodes accumulate a `match_count`; each anchor that reaches the same issue increments it by 1.

### Known issue → fixes

For each `KnownIssue` found above:

```cypher
MATCH (ki:KnownIssue) WHERE id(ki) = $ki_id
MATCH (ki)-[:RESOLVED_BY]->(fix:Fix)
RETURN fix
```

`Fix` nodes accumulate `match_count` per hop.

### Pre-computed similarity

```cypher
MATCH (ci:CustomerIssue) WHERE id(ci) = $issue_id
MATCH (ci)-[:HAS_SIMILARITY_MATCH]->(sm:SimilarityMatch)-[:MATCHES]->(ki:KnownIssue)
WHERE sm.review_status IN ['candidate', 'confirmed']
RETURN ki, sm.review_status
```

`confirmed` adds +2, `candidate` adds +1 to `match_count`.

### Recent commits

After the feature/module traversal establishes the preliminary file list, the DRE queries recent commits touching those files:

```cypher
MATCH (f) WHERE id(f) = idFrom('file', $project_slug, $file_path)
OPTIONAL MATCH (c)-[:TOUCHES]->(f)
RETURN f, c
```

The query is run per file path (both source and test). Results are deduplicated by SHA, sorted by timestamp descending, and capped at 10. Each `Commit` node carries a `file_hunks` property (JSON string) parsed into `file_hunk_data: dict[file_path, list[hunk]]` for function anchor matching.

## Evidence Sources

Each file accumulates `EvidenceItem` records. Items are typed:

| Type | Base score | Source |
|---|---|---|
| `ticket_mention` | 10.0 | File path explicitly named in ticket raw text (LLM parse) |
| `test_coverage` | 8.0 | Feature/module graph traversal → TestFile |
| `feature_anchor` | 7.0 | Feature/module graph traversal → source File |
| `element_anchor_match` | 6.0 | Registered element name token-matches symptom/error terms |
| `function_anchor_match` | 6.0 | Git hunk function def token-matches func_anchor_tokens |
| `recent_commit` | 4.0 | File touched by a recent commit |
| `doc_penalty` | negative | Applied to non-source files (×0.25 actionability multiplier) |

## Anchor Token Matching

### Tokenizer

`_tokenize(name: str) -> set[str]` splits a camelCase, snake_case, or kebab-case identifier into lowercase tokens longer than 2 characters. Steps: split on `[_\-\s]+`, then split each part on camelCase boundaries via regex, lowercase all, filter length ≤ 2.

Examples:
- `"reinit_requested"` → `{"reinit", "requested"}`
- `"DeviceCard"` → `{"device", "card"}`
- `"_make_tracker_row"` → `{"make", "tracker", "row"}`

### Token sets

Three token sets are built during retrieval:

**`anchor_tokens`** — tokens from all of `feature_slugs + error_sigs + symptoms`. Used for context but not directly for function matching (too broad; feature/module slug names produce tokens that over-match element names within their own module).

**`symptom_error_tokens`** — tokens from `error_sigs + symptoms` only. Excludes feature/module slug tokens. Used as the base for element anchor matching so that module-named elements (e.g., `DeviceCard` in the `device-card` module) do not self-match.

**`func_anchor_tokens`** — `symptom_error_tokens` plus tokens from all `matched_elements`. Used for function anchor matching. This is more specific than `anchor_tokens` while still incorporating element names that were confirmed to match.

## Element Anchor Matching

After the preliminary file list is established, for each resolved module slug:

1. Fetch `module_elements.get(slug, [])` — the list of registered element names.
2. Find `matching_elements`: elements whose `_tokenize(elem)` overlaps with `symptom_error_tokens`.
3. For each source or test file already in the evidence maps for that module, add an `element_anchor_match` item (score 6.0). Explanation is the matched element names (up to 3), comma-separated.
4. Extend `matched_elements` with the matching elements, so `func_anchor_tokens` benefits from the confirmed match.

Only files already in the evidence maps receive this evidence — element matching does not discover new files.

## Function Anchor Matching

For each recent commit, for each file it touched that is already in the evidence map:

1. Load `file_hunk_data[file_path]` from the commit's parsed `file_hunks` property.
2. Call `_matching_defs(hunk_data, func_anchor_tokens)` — returns function/method definition names from `+` lines of the diff whose tokens overlap with `func_anchor_tokens`.
3. If any match: add `function_anchor_match` evidence (score 6.0). Explanation is `"{names} · {sha_short}"` — the matched names and the 7-char commit SHA.

The SHA in the explanation is used by the demo UI to display function matches inline with the correct commit row.

## Candidate Scoring

Each file's evidence items are combined by `_score_candidate`:

1. Positive items are grouped by type.
2. Within each type group, items are sorted descending and summed with geometric decay: `score[0] + score[1]×0.5 + score[2]×0.25 + …`. This rewards the first hit of each type but dampens redundant hits of the same type.
3. A diversity bonus of `3.0 × min(unique_positive_types - 1, 4)` is added. Files with signal from multiple independent sources are preferred.
4. All negative items (penalties) are summed directly and added.
5. Result is rounded to one decimal place.

Confidence label:
- `"high"`: score ≥ 20.0
- `"medium"`: score ≥ 10.0
- `"low"`: score < 10.0

Non-source files (docs, markdown, config) receive a `doc_penalty` item equal to `raw_score × (0.25 - 1.0)`. This penalizes files that cannot contain bugs without removing them from the output.

Source candidates and test candidates are built and sorted separately (cap: 20 each), then merged and re-sorted by score for `scored_candidates`. `relevant_files` and `relevant_tests` are the ordered paths from each list.

## LLM Summary

After traversal and scoring, `gateway.summarise_packet` is called with:
- `issue_text`, `module_slugs`, `error_signatures`, `symptoms`
- `relevant_files`, `relevant_tests`
- `matched_elements` — element names that matched, so the summary can name them explicitly
- `recent_commits` (up to 5), `known_issues`

The summary prompt instructs the LLM to prioritize matched elements > named errors/known issues > files > commits. On any exception, `summary` falls back to `issue.summary`.

## Streaming

Two `on_progress` events are emitted if the callback is provided:

1. **`"loading"`** — emitted immediately after project slug verification, before LLM anchor extraction. The partial packet contains only `issue.summary`; all lists are empty. Lets the caller show the ticket title before the LLM call.

2. **`"partial"`** — emitted after traversal and scoring but before the summary LLM call. The partial packet has all evidence but `summary = ""`. Lets the caller render candidates while waiting for the summary.

## Project Isolation

`project_slug` is passed explicitly to `retrieve()` and verified against the fetched `CustomerIssue` node before any traversal begins. Every Cypher query includes `project_slug` as a property filter or as part of the `idFrom` key. The DRE never derives `project_slug` from graph state.

## Eventual Consistency

The DRE reads whatever edges are currently in Quine. If ingestion has not yet run `replace_edges` after a metadata change, the DRE may return stale relationships. This is accepted. The ingestion pipeline is responsible for edge reconciliation.

## Error Types

```python
class DREError(Exception): pass
class DRENotFoundError(DREError): pass          # CustomerIssue not found or project_slug mismatch
class DREAnchorError(DREError): pass            # no anchors and LLM fallback failed
class DREGraphUnavailableError(DREError): pass  # Quine unreachable
class DRELLMUnavailableError(DREError): pass    # LLM gateway unreachable (fallback path only)
```

## Decisions & Alternatives

| Decision | Chosen | Alternatives Considered | Rationale |
|---|---|---|---|
| Input is node ID, not raw text | `CustomerIssue` node ID required | Accept raw text | Ingestion and retrieval are separate responsibilities; node-ID-only keeps the DRE stateless |
| Graph-first anchors | Read from `AFFECTS`/`HAS_ERROR` edges; LLM fallback only | Always LLM; always graph | Graph edges are validated facts; re-parsing every call is slow and redundant |
| Evidence-based scoring | EvidenceItem accumulation with type-grouped decay + diversity bonus | Pure match count; per-type weights; score-only | Named evidence items are auditable per file; diversity bonus rewards multi-signal agreement |
| Symptom/error tokens for element matching, not full anchor_tokens | `symptom_error_tokens` excludes feature/module slug tokens | Use full anchor_tokens | Module slug tokens (e.g., `card` from `device-card`) match unrelated element names within the same module |
| `func_anchor_tokens` = symptom/error + matched element tokens | Separate from `anchor_tokens` | Use `anchor_tokens` | Avoids function name false positives from module slug tokens; incorporates element matches for richer hunk matching |
| Function def names stored on Commit node as JSON | `file_hunks` JSON string on Commit node | Edge property; separate node | Quine does not support relationship/edge properties — all data must be on nodes |
| Recent commits capped at 10 | Fixed cap after sorting by timestamp desc | No cap; configurable | Prevents commit noise for heavily-modified files without losing the most recent signal |
| `on_progress` streaming callback | Caller-supplied callback, two events | SSE in engine; no streaming | Keeps engine transport-agnostic; the API route layer owns SSE framing |
| LLM summary includes `matched_elements` | Passed to `summarise_packet` | Summarize from files only | Without element names the LLM focuses on the file path; element names produce more specific summaries |
| Non-source file penalty | ×0.25 actionability multiplier applied as `doc_penalty` item | Exclude entirely; no penalty | Docs appear in results when they are the only anchor match, but their low score prevents them from displacing real source files |

## Open Questions & Future Decisions

### Resolved
1. ✅ Evidence accumulation chosen over match count — typed EvidenceItem with decay-weighted scoring.
2. ✅ Function def matching via git hunk data on Commit nodes — confirmed Quine does not support edge properties.
3. ✅ Element anchor matching uses `symptom_error_tokens` to avoid module name self-match.

### Deferred
1. **Recency boost on commits** — recent commits contribute uniform score; older commits are not penalized beyond being later in the list. A recency decay function (e.g., exponential by days) could be added without changing the evidence model.
2. **Vector index recall** — deferred until graph-only retrieval is proven insufficient.
3. **`relevant_tests`** — produced but the `Test` node type and `HAS_TEST` edge do not yet exist for all modules.
4. **Anchor caching** — LLM fallback re-parses raw text on every call. Fix: write derived anchors back as `AFFECTS`/`HAS_ERROR` edges during ingestion. The DRE does not write these itself.
5. **Traverse `HAS_FIX` edges** — `Feature -[HAS_FIX]-> Fix` edges from inline MODOK blocks are not yet traversed in the fix retrieval path.
6. **Element matching for non-module anchors** — currently element matching only fires for resolved module slugs. Feature slugs that do not resolve to a module do not benefit.

## References

- `docs/high-level-design.md §System Design` — DRE role and debug packet concept
- `docs/llds/llm-gateway.md` — `parse_ticket` and `summarise_packet` interfaces
- `docs/llds/quine-client.md` — `query()` and `get_node()` interfaces
- `docs/llds/ingestion-pipeline.md` — upstream pipeline; creates `CustomerIssue` nodes and writes `AFFECTS`/`HAS_ERROR` edges
- `docs/llds/github-ingestion.md` — ingests `Commit` nodes with `TOUCHES` edges and `file_hunks` hunk data
