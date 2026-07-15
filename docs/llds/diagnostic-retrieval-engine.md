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
    feature_source_files: dict[str, list[str]] | None = None,
    on_progress: Callable[[str, DebugPacket], None] | None = None,
) -> DebugPacket
```

`issue_id` must be the ID of an existing `CustomerIssue` node. The caller is responsible for ingesting the issue before calling `retrieve`.

`project_slug` is required and verified against the fetched node before any traversal.

`backend` is forwarded to the LLM gateway if the fallback path is needed.

`valid_slugs`, `feature_slugs`, `module_slugs`, `feature_descriptions`, `module_descriptions` are forwarded to `gateway.parse_ticket` on the LLM fallback path; they guide the LLM toward valid slug values.

`module_elements` — maps module slug → list of registered element names (UI signals, emitted events, named components). Used for element anchor matching.

`module_source_files` — maps module slug → list of source file paths for that module. Used for element anchor matching and pre-matching.

`feature_source_files` — maps feature slug → the feature's own declared `source_files` from the registry (distinct from the union of its modules' files). Used to distinguish primary from peripheral evidence during Feature/Module traversal — see § Graph Traversal.

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
MATCH (ci) WHERE id(ci) = $issue_id AND ci.node_type = 'CustomerIssue'
MATCH (ci)-[:AFFECTS]->(f) WHERE f.node_type = 'Feature' AND f.project_slug = $project_slug
RETURN f.feature_slug
```

```cypher
MATCH (ci) WHERE id(ci) = $issue_id AND ci.node_type = 'CustomerIssue'
MATCH (ci)-[:HAS_ERROR]->(e) WHERE e.node_type = 'ErrorSignature' AND e.project_slug = $project_slug
RETURN e.normalized_error
```

If at least one feature slug or error signature is found, the LLM fallback is skipped entirely.

**Two real bugs found live, testing a real GitHub issue's debug-packet write-back** (`docs/llds/standing-queries.md` — the standing query's GitHub comment came back with only a summary line, no known issues/fixes/files, despite a real `AFFECTS` edge sitting in the graph):

1. **`:Label` syntax never matches real Quine.** The original queries used `(ci:CustomerIssue)` / `(f:Feature {project_slug: ...})` — established multiple times elsewhere in this project (`docs/llds/standing-queries.md § Live Verification Findings`) that `node_type` is a property, not a real Quine label, so these patterns silently matched nothing. This meant "graph-first" was a dead code path in production — `retrieve()` always fell through to the LLM fallback, even when real anchors existed. Fixed by filtering on `WHERE ... node_type = '...'` instead. This same bug, and fix, applied to four more traversal functions (`_traverse_error_to_known_issues`, `_traverse_ki_to_fixes`, `_fetch_fix_commit_sha`, `_traverse_similarity` — see § Graph Traversal) — all of `modok retrieve`/`diagnose`'s known-issue/fix/similarity lookups were affected, not just anchor extraction.
2. **`RETURN f.feature_slug` projects a scalar, not a node.** Even after fixing (1), the anchors still came back empty: real Quine returns a scalar `RETURN` projection as the raw value directly (`row[0] == "wifi-provisioning"`), not wrapped in a `{"id":..., "properties": {...}}` node dict. The original code read `row[0]["properties"]["feature_slug"]`, silently extracting nothing from every row. Fixed by reading `row[0]` directly. This distinction was already understood correctly elsewhere in this same file (`_traverse_similarity`'s handling of `sm.review_status`, and `modok retrieve`'s own `RETURN id(n)` resolution query) — `_graph_anchors` simply predated that understanding.

Neither bug was visible to the unit test suite, which mocks `client.query()` directly and returns whatever shape the test author chose — both bugs only exist in the gap between what real Quine actually returns and what the mocks assumed it returns.

### LLM fallback

If no graph anchors are found and `raw_text` is present on the `CustomerIssue` node:

1. **Mechanical pre-match** — `_pre_match_modules(raw_text, module_source_files, module_elements)` checks two signals independently:
   - Literal source file path mentions (e.g. `agent/src/main.c` appears verbatim in the text).
   - Element-name token overlap: words from `raw_text` are tokenized (camelCase/snake_case split, length > 2), and any module whose element name's token set is a subset of those tokens is matched. Example: ticket text containing `tracker_lost_logged` → tokens `{tracker, lost, logged}` → matches any module with an element `tracker_lost_logged`.
2. **LLM call** — `gateway.parse_ticket(raw_text, ...)` is called. The LLM result is **authoritative**: when it succeeds, its `feature_slugs`, `error_signatures`, `symptoms`, and `mentioned_files` are used directly and the pre-match result is discarded.
   - On `LLMResponseError` (bad output): fall back to pre-match results only; `error_signatures`, `symptoms`, and `mentioned_files` are empty.
   - On `LLMUnavailableError`: raises `DRELLMUnavailableError`.
3. **Mechanical validation pass** — all slugs (from LLM or pre-match fallback) are filtered against `valid_slugs` before any Quine traversal. This prevents hallucinated slugs from reaching the graph.
5. `symptoms` are stored for context but not used in graph traversal. `mentioned_files` are seeded directly into evidence maps as `ticket_mention` items.

If `raw_text` is `None` and no graph anchors exist, raises `DREAnchorError`.

## Graph Traversal

### Feature/Module anchor → files

For each slug in `feature_slugs` (which may be either Feature or Module slugs):

1. Try Feature traversal: `Feature -[:IMPLEMENTED_BY]-> Module -[:DEFINED_IN]-> File`
2. Also fetch: `Feature -[:HAS_TEST]-> TestFile`
3. If no files found via Feature, fall back to Module traversal: `Module -[:DEFINED_IN]-> File` and walk up to parent Feature for test files.
4. Files from step 1 or 3 get `feature_anchor` evidence (score 8.0). Test files get `test_coverage` evidence (score 7.0) — source intentionally outscores its own test for equivalent single-anchor evidence (found live: the reverse ordering put test files above the actual likely-buggy source file in every real ticket's ranked candidates).
5. `resolved_as` is `"feature"` or `"module"` and is used to build `affected_areas`.
6. **Paths are deduplicated (preserving first-seen order) before evidence assignment**, for both the Feature-level query and the Module-fallback query, source and test alike. Found live: a Feature with several Modules (`wifi-provisioning` → `stagehand-ble`, `stagehand-wifi-provision`, `wifi-provision-logic`, `stagehand-health`, `stagehand-wifi-provision-dbus`, `chroot-customize`) fans out per module in the two-hop `Feature -[IMPLEMENTED_BY]-> Module -[DEFINED_IN]-> File` query — a file reachable from more than one module (shared feature-level docs registered against several modules) came back once per module, so the same file received the same `feature_anchor` evidence item 6 times over, inflating its score well past what one genuine match would earn.
7. **Primary vs. peripheral evidence.** When `resolved_as == "feature"` and `feature_source_files` was supplied, a source file gets `feature_primary_file` evidence (9.0) if its path is in that feature's own declared `source_files` list from the registry, or `feature_anchor` evidence (3.0) if it's only reachable via one of the feature's modules. When resolution falls back to a bare Module slug, or no `feature_source_files` context is available, every file gets `feature_primary_file` (9.0) — a direct module match is already narrow, and missing context shouldn't silently demote everything. Found live: `wifi-provisioning`'s module list includes modules only tangentially related to the feature itself (`chroot-customize` — OS image build tooling; `stagehand-health` — a general health monitor), and files reachable only through those modules were scoring identically to the feature's actual, registry-curated primary implementation files (`client/stagehand_client/wifi_provision_logic.py`, `scripts/stagehand-wifi-provision`, `client/stagehand_client/stagehand_ble.py` — the feature's own `source_files:` list). A handful of unrelated recent commits on the tangential files was then enough to outrank the file most directly relevant to the ticket. `feature_anchor` (peripheral) is also excluded from the diversity/corroboration bonus, same as `recent_commit` — see § Candidate Scoring.

### Error signature anchor → known issues

For each `normalized_error` in `error_sigs`:

```cypher
MATCH (e) WHERE e.node_type = 'ErrorSignature' AND e.project_slug = $project_slug
  AND e.normalized_error = $normalized_error
MATCH (e)<-[:HAS_ERROR]-(ki) WHERE ki.node_type = 'KnownIssue'
RETURN ki
```

`KnownIssue` nodes accumulate a `match_count`; each anchor that reaches the same issue increments it by 1.

### Known issue → fixes

For each `KnownIssue` found above:

```cypher
MATCH (ki) WHERE id(ki) = $ki_id
MATCH (ki)-[:RESOLVED_BY]->(fix) WHERE fix.node_type = 'Fix' AND fix.project_slug = $project_slug
RETURN fix
```

`Fix` nodes accumulate `match_count` per hop.

### Pre-computed similarity

```cypher
MATCH (ci) WHERE id(ci) = $issue_id AND ci.node_type = 'CustomerIssue'
MATCH (ci)-[:HAS_SIMILARITY_MATCH]->(sm)-[:MATCHES]->(ki)
WHERE sm.node_type = 'SimilarityMatch' AND ki.node_type = 'KnownIssue'
  AND ki.project_slug = $project_slug AND sm.review_status IN ['candidate', 'confirmed']
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
| `feature_primary_file` | 9.0 | Feature/module graph traversal → source File that is in the feature's own declared `source_files` (or resolved via a bare Module slug) |
| `feature_anchor` | 3.0 | Feature/module graph traversal → source File reachable only via one of the feature's modules, not in the feature's own declared `source_files` |
| `test_coverage` | 7.0 | Feature/module graph traversal → TestFile |
| `element_anchor_match` | 6.0 | Registered element name token-matches symptom/error terms |
| `function_anchor_match` | 6.0 | Git hunk function def token-matches func_anchor_tokens |
| `recent_commit` | 1.5 | File touched by a recent commit, with no established relevance to the specific ticket |
| `doc_penalty` | negative | Applied to non-source files (×0.25 actionability multiplier) |

**`recent_commit`'s weight is deliberately low** (`docs/scoring-brainstorm.md` § Recency, § MODOK evidence-type mapping). It fires for *any* file touched by *any* recent commit, independent of whether that commit touched anything relevant to the ticket — it is corroborated relevance (`function_anchor_match`, 6.0, fired separately when a commit's diff actually matches an anchored symbol) that deserves the higher weight, not bare recency. Found live: at the previous weight (4.0), a handful of unrelated commits on a frequently-edited operational script (`chroot-customize.sh`, `stagehand-health`) accumulated enough decayed `recent_commit` evidence to outrank a file that was directly named in the ticket but hadn't been touched recently — the exact anti-pattern `scoring-brainstorm.md` warns against ("a recent vague keyword match should not beat an older exact prior fix"). Lowering the base weight to 1.5 caps the maximum contribution from an unbounded string of unrelated commits to roughly 3.0 (geometric decay converges to ~2× base), instead of ~8.0.

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
3. A diversity bonus of `3.0 × min(unique_corroborating_types - 1, 4)` is added, where `unique_corroborating_types` excludes `recent_commit` (see `_NON_CORROBORATING_TYPES`). Files with signal from multiple independent sources are preferred — but bare recency is not treated as independent corroboration of relevance; a file's own decayed `recent_commit` score still adds to its total, it just cannot unlock the bonus on its own. Found live: before this exclusion, a file whose only evidence was a broad `feature_anchor` match plus several commits from unrelated maintenance work still got the full +3.0 bonus for having a second evidence *type* present, letting a frequently-edited operational script outrank a file directly relevant to the ticket that simply hadn't been touched recently.
4. All negative items (penalties) are summed directly and added.
5. Result is rounded to one decimal place.

Confidence label:
- `"high"`: score ≥ 20.0
- `"medium"`: score ≥ 10.0
- `"low"`: score < 10.0

Non-source files (docs, markdown, config) receive a `doc_penalty` item equal to `raw_score × (0.25 - 1.0)`. This penalizes files that cannot contain bugs without removing them from the output. "Source" (`_is_source_path`) includes the fixed extension set (now including `.sh`) *and* any extensionless file directly under a `scripts/` directory — found live: shell scripts and extensionless deployment/provisioning scripts (e.g. `scripts/stagehand-wifi-provision`) were previously classified as non-source and penalized identically to a markdown doc, despite being real operational code directly relevant to the ticket.

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
| `recent_commit` base weight | 1.5 (low) | 4.0 (original); split into a separate "correlated" evidence type | `docs/scoring-brainstorm.md` treats recency as a modifier, not an independent strong signal — the correlated case is already captured by `function_anchor_match` (6.0), so a separate type wasn't needed, just a lower base weight for the uncorrelated case |
| `recent_commit` excluded from diversity bonus | Excluded from the corroborating-type count in `_score_candidate` | Leave it counting; add a third tier between "counts" and "doesn't" | Lowering the base weight alone wasn't sufficient — the flat +3.0 bonus for a second *type* still let weak, uncorrelated recency evidence out-rank files with no recency evidence at all. A binary in/out of the corroboration count is the simplest rule that fixes the observed case without adding new dimensions |
| Feature/Module traversal path dedup | `dict.fromkeys` dedup preserving order, applied to source and test paths in both the Feature and Module-fallback queries | `RETURN DISTINCT file` in Cypher | Deduping in Python is simpler to reason about across the two-hop `OPTIONAL MATCH` fan-out and doesn't depend on how Quine's `DISTINCT` interacts with multi-column projections (`f, m, file`) |
| Primary/peripheral split for feature-traversal source files | New `feature_primary_file` (9.0, corroborating) vs. demoted `feature_anchor` (3.0, non-corroborating), keyed off the feature's own registry `source_files` list | Narrow the feature's module list instead (registry-side fix); add a hub-penalty formula per `docs/scoring-brainstorm.md` | The feature's own `source_files` is already the curated, feature-owner-declared signal for "this implements the feature" — using it doesn't require every project's registry to keep an artificially narrow module list, and it's a much smaller change than a general hub-penalty model. Registry curation (e.g. trimming `wifi-provisioning`'s module list) remains a valid complementary fix, left to the project maintainer |

## Open Questions & Future Decisions

### Resolved
1. ✅ Evidence accumulation chosen over match count — typed EvidenceItem with decay-weighted scoring.
2. ✅ Function def matching via git hunk data on Commit nodes — confirmed Quine does not support edge properties.
3. ✅ Element anchor matching uses `symptom_error_tokens` to avoid module name self-match.

### Deferred
1. **Recency decay by age, not just position** — recent commits within the same file contribute uniform per-hit score (only dampened by the existing same-type geometric decay, not by how many days old the commit is); a true recency-by-age multiplier (e.g., exponential by days, per `docs/scoring-brainstorm.md` § Recency multiplier) could be layered on top of the now-lowered base weight without changing the evidence model. Not done yet — the immediate live bug was fixed by lowering the base weight (see Evidence Sources), which was sufficient and much lower-risk than adding a new dimension.
2. **Full specificity/directness/reliability multiplier model** — `docs/scoring-brainstorm.md` proposes per-evidence-item dimensions (specificity, directness, reliability, recency, actionability, confidence) multiplied together, plus hub/stale/contradiction penalties. The current implementation only has flat per-type base scores, the corroboration bonus, and same-type geometric decay — the latter two already match the brainstorm's formulas, but the richer per-dimension weighting does not exist. Adopting it fully would be a larger scoring-engine redesign; deferred until a second concrete ranking failure demonstrates the flat-weight model is insufficient beyond what targeted weight adjustments (like the `recent_commit` fix above) can address.
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
- `docs/scoring-brainstorm.md` — evidence weighting design rubric; current implementation follows its corroboration bonus and same-type decay formulas exactly, and its recency-as-modifier principle for `recent_commit`'s weight, but not yet its full per-dimension multiplier model (see Open Questions)
