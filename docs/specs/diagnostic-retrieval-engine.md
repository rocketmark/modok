# Diagnostic Retrieval Engine Specs

Specs for `modok.retrieval` — the read path that assembles a debug packet from a `CustomerIssue` node ID.

LLD: `docs/llds/diagnostic-retrieval-engine.md`

---

## Test Level Convention

See `docs/testing-standard.md` for full definitions.

- **[U]** — Unit test with mocked dependencies.
- **[P]** — Property test (`hypothesis`). Implies [U].
- **[C]** — Contract test against live Quine instance. Implies [U].

---

## Interface and Project Isolation

- [x] **DRE-IFACE-001** [U]: `retrieve(issue_id, project_slug, ...)` shall fetch the `CustomerIssue` node for `issue_id` and verify that its `project_slug` matches the argument. If the node is not found or the `project_slug` does not match, the system shall raise `DRENotFoundError` without performing any traversal.
- [x] **DRE-IFACE-002** [U]: When `project_slug` does not match the fetched node, the system shall raise `DRENotFoundError` before issuing any Cypher query. `project_slug` is always the caller-supplied argument; it is never derived from graph state.
- [x] **DRE-IFACE-003** [U]: When Quine is unreachable, `retrieve` shall raise `DREGraphUnavailableError`.

---

## Anchor Extraction

- [x] **DRE-ANCH-001** [U, C]: When the `CustomerIssue` node has outbound `AFFECTS` edges to `Feature` nodes in the same project, the system shall use those feature slugs as anchors and shall not call the LLM gateway. `_graph_anchors`'s Cypher shall filter both node types by a `WHERE ... node_type = '...'` equality check, never `:Label` syntax (found live against Quine 1.10.0 — `node_type` is a property, not a real label, so `(ci:CustomerIssue)`/`(f:Feature {...})` never matches, silently making graph-first anchoring a dead code path that always fell through to the LLM fallback). `RETURN f.feature_slug` projects a scalar; the system shall read it as the raw value at `row[0]`, not as `row[0]["properties"]["feature_slug"]` — a second, independent bug found in the same live pass, since real Quine returns scalar projections as bare values, not node dicts.
- [x] **DRE-ANCH-002** [U, C]: When the `CustomerIssue` node has outbound `HAS_ERROR` edges to `ErrorSignature` nodes in the same project, the system shall use those normalized error strings as anchors and shall not call the LLM gateway. Same `node_type`-property and scalar-row requirements as DRE-ANCH-001 apply symmetrically.
- [x] **DRE-ANCH-003** [U]: When graph anchors are found — at least one feature slug or one error signature after project-scoped filtering — the LLM fallback (i.e., the `parse_ticket` call) shall be skipped entirely, regardless of the `backend` parameter.
- [x] **DRE-ANCH-004** [U]: When no graph anchors are found and `CustomerIssue.raw_text` is present, the system shall compute mechanical pre-match results, call `gateway.parse_ticket`, then use the LLM result as the authoritative source for `feature_slugs`, `error_signatures`, `symptoms`, and `mentioned_files`. The pre-match result is used only when the LLM call fails (see DRE-ANCH-006). After the LLM path completes, the result shall be filtered against `valid_slugs` (see DRE-ANCH-010).
- [x] **DRE-ANCH-005** [U]: When no graph anchors are found and `CustomerIssue.raw_text` is `None`, the system shall raise `DREAnchorError`.
- [x] **DRE-ANCH-006** [U]: When `parse_ticket` raises `LLMResponseError`, the system shall fall back to the mechanical pre-match results with empty `error_signatures` and `symptoms`, rather than raising `DREAnchorError`.
- [x] **DRE-ANCH-007** [U]: When `parse_ticket` raises `LLMUnavailableError`, the system shall raise `DRELLMUnavailableError`.
- [x] **DRE-ANCH-008** [U]: `symptoms` returned by `parse_ticket` shall be stored in `IssueAnchors.symptoms` for context but shall not be used in graph traversal or match count scoring.
- [x] **DRE-ANCH-009** [U]: When no graph anchors are found and `raw_text` is present, the system shall pre-scan `raw_text` for (a) literal source file path mentions using `module_source_files`, and (b) element-name token matches using `module_elements` — tokenizing words from `raw_text` and checking whether any element's token set is a subset of those tokens. The resulting module slugs are used as the fallback `feature_slugs` when the LLM call fails (DRE-ANCH-006); they are not added to the LLM result when the LLM call succeeds.
- [x] **DRE-ANCH-010** [U]: After merging pre-matched and LLM-returned slugs, the system shall filter all `feature_slugs` against `valid_slugs` (when provided) before any Quine traversal.

---

## Quick Investigation Summary

- [x] **DRE-QUICK-001** [U]: `quick_investigation_summary(issue_id, project_slug, client, feature_source_files=None)` shall fetch only the `CustomerIssue`'s graph-first anchors (`_graph_anchors` — the same `AFFECTS`/`HAS_ERROR` lookup as `DRE-ANCH-001`/`DRE-ANCH-002`) and shall perform no traversal (feature/module-to-file, error-to-known-issue, commit lookups), no evidence scoring, and **no LLM call of any kind**. An earlier version of this function reused `gateway.summarise_packet` with a reduced input; found live, that call alone measured ~85s standalone, and in production the gap to the following "results" comment (whose own summary call, per `DRE-SUMM-001`, landed on an already-warm local model) was often just a few seconds — the intended head start mostly didn't materialize, since the LLM call itself was the slow part, not the traversal this function was built to skip.
- [x] **DRE-QUICK-002** [U]: When at least one feature slug or error signature is found, the system shall mechanically construct the summary string as `"Features: {slugs}"` and/or `"Errors: {sigs}"` (joined by ` · ` when both present), followed by `". Likely files: {paths}"` when `feature_source_files` yields any paths for the resolved feature slugs (capped at 5, first-seen order, deduplicated across slugs). No natural-language generation is involved.
- [x] **DRE-QUICK-003** [U]: On any failure or absence of information — the `CustomerIssue` node not found, the graph-anchor query failing, or no feature/error anchors found at all — the system shall return a fallback string (`CustomerIssue.summary`, i.e. the ticket title) rather than raising, except when the node itself cannot be fetched, in which case it shall return `""`. This function shall never raise.

---

## Graph Traversal

- [x] **DRE-TRAV-001** [U]: For each feature slug anchor, the system shall first attempt to traverse `Feature -[:IMPLEMENTED_BY]-> Module -[:DEFINED_IN]-> File` and `Feature -[:HAS_TEST]-> TestFile`. If no files are found via Feature, the system shall fall back to treating the slug as a Module slug and traverse `Module -[:DEFINED_IN]-> File`. Source files found shall receive `feature_anchor` evidence (score 8.0). Test files found via `HAS_TEST` shall NOT receive a scored evidence item — see DRE-TESTCOV-001/002 for what they receive instead.
- [x] **DRE-TRAV-002** [U]: For each error signature anchor, the system shall traverse `ErrorSignature <-[:HAS_ERROR]- KnownIssue` within the project and accumulate matching `KnownIssue` nodes by match count.
- [x] **DRE-TRAV-003** [U]: For each `KnownIssue` found via error signature traversal, the system shall traverse `KnownIssue -[:RESOLVED_BY]-> Fix` and accumulate matching `Fix` nodes by match count.
- [x] **DRE-TRAV-004** [U]: The system shall traverse `CustomerIssue -[:HAS_SIMILARITY_MATCH]-> SimilarityMatch -[:MATCHES]-> KnownIssue` and include `KnownIssue` nodes where `SimilarityMatch.review_status` is `"candidate"` or `"confirmed"`. Nodes where `review_status` is `"rejected"` shall be excluded.
- [x] **DRE-TRAV-005** [U, C]: All traversal queries shall include `project_slug` as a parameter. The DRE shall not return nodes from a different project even if reachable via graph traversal. All traversal Cypher (`_traverse_error_to_known_issues`, `_traverse_ki_to_fixes`, `_fetch_fix_commit_sha`, `_traverse_similarity`) shall filter node types by `WHERE ... node_type = '...'` equality, never `:Label` syntax, matching DRE-ANCH-001's finding — these were found live to use the same broken label syntax, silently returning zero traversal results (no known issues, no prior fixes) even when the underlying graph data existed.
- [x] **DRE-TRAV-006** [U]: All traversals shall use `QuineClient.query()` with explicit Cypher. The DRE shall not use `QuineClient.traverse()`.
- [x] **DRE-TRAV-007** [U]: After the preliminary file list is established from feature/module traversal, the system shall query recent `Commit` nodes that have `TOUCHES` edges to those files, sorted by timestamp descending, capped at 10. Each result shall include the commit's `sha`, `timestamp`, `author_name`, `message`, and `files_touched`. The `file_hunks` JSON property on each `Commit` node shall be parsed into `file_hunk_data` for use in function anchor matching.
- [x] **DRE-TRAV-008** [U]: File paths returned from `_traverse_feature_to_files` (both the Feature-level source/test paths and the Module-fallback source/test paths) shall be deduplicated, preserving first-seen order, before evidence is assigned. Found live: a Feature with multiple Modules (`wifi-provisioning` → 6 modules) whose `OPTIONAL MATCH (f)-[:IMPLEMENTED_BY]->(m) OPTIONAL MATCH (m)-[:DEFINED_IN]->(file)` two-hop query fans out per module — a file reachable from more than one module (e.g. a shared feature-level doc registered against several modules) was returned once per module, producing multiple identical `feature_anchor` evidence items for the same file and the same slug, inflating its score well beyond what a single genuine match would justify.
- [x] **DRE-TRAV-009** [U]: When a feature slug resolves via the Feature-level path (`resolved_as == "feature"`) and a `feature_source_files` mapping (feature slug → the feature's own declared `source_files` from the registry) is supplied, each source file found by traversal shall receive `feature_primary_file` evidence (score 9.0) if its path is in that feature's declared list, or `feature_anchor` evidence (score 3.0) otherwise. When resolution falls back to a bare Module slug (`resolved_as == "module"`), or no `feature_source_files` mapping is supplied, all source files shall receive `feature_primary_file` evidence (score 9.0) — a direct module-slug resolution is already narrow enough not to need the distinction, and the absence of registry context should not silently demote every file. Found live: a Feature whose module list includes tangentially-related modules (e.g. `wifi-provisioning` → `chroot-customize`, an OS image build script; `stagehand-health`, a general health monitor) let those modules' files accumulate evidence identical in strength to the feature's actual, registry-declared primary implementation files, letting a frequently-committed but only tangentially-related file outrank the file most directly responsible for the reported symptom.

---

## Test Coverage (Informational)

- [x] **DRE-TESTCOV-001** [U]: Every test path discovered via `HAS_TEST` traversal for a resolved anchor slug shall be recorded in a covering-slugs map (path → deduplicated list of slugs) and shall NOT receive a scored `EvidenceItem`. The test path shall still be seeded as an eligible (empty-evidence) key in the test-file evidence map so it can accrue real evidence from later steps (recent commits, `commit_message_match`, `function_anchor_match`, `ticket_mention`). Found live (rocketmark/stagehand#31): a test file covering two features accumulated two `test_coverage` evidence items that summed via same-type geometric decay (7.0 + 7.0×0.5 = 10.5) to a MEDIUM confidence score despite having no evidence tying it to that specific ticket.
- [x] **DRE-TESTCOV-002** [U]: Immediately before scored candidates are built, for every path in the covering-slugs map: if its test-file evidence entry is still empty, the system shall remove it from the scored-candidate evidence map and append it to `DebugPacket.covered_tests` as `{path, covering_slugs}`; if the entry is non-empty (real evidence was added by a later step), the path shall remain in `scored_candidates`/`relevant_tests` and shall NOT also appear in `covered_tests`. The two lists are disjoint by construction — no path appears in both.

---

## Known Issue and Fix Prioritization

- [x] **DRE-SCORE-001** [U]: Each `KnownIssue` item shall carry a `match_count` initialised to `1` on first appearance via anchor traversal. Each additional anchor that reaches the same item shall increment `match_count` by `1`.
- [x] **DRE-SCORE-002** [U]: A `KnownIssue` reached via a `confirmed` `SimilarityMatch` shall have its `match_count` incremented by `2`. A `KnownIssue` reached via a `candidate` `SimilarityMatch` shall have its `match_count` incremented by `1`.
- [x] **DRE-SCORE-003** [P]: `KnownIssue` and `Fix` result lists shall be sorted descending by `match_count`. Items with equal `match_count` preserve insertion order (first-found).
- [x] **DRE-SCORE-004** [P]: `known_issues` shall be capped at 10 items after sorting. `prior_fixes` shall be capped at 10 items.
- [x] **DRE-SCORE-005** [P]: For any two `KnownIssue` items A and B where A was matched by more anchors than B, A shall appear before B in the `known_issues` list.
- [x] **DRE-SCORE-006** [P]: `Fix` nodes shall have their `match_count` incremented by `1` for each `KnownIssue -[:RESOLVED_BY]-> Fix` hop that fires during traversal. If N `KnownIssue` nodes resolve to the same `Fix`, the `Fix` shall have `match_count` equal to N from those hops alone.

---

## Anchor Token Matching

- [x] **DRE-TOKEN-001** [U]: `tokenize(name)` (`src/modok/text_utils.py`) shall split a camelCase, snake_case, or kebab-case identifier into lowercase tokens of length greater than 2. CamelCase boundaries shall be detected via regex; underscore, hyphen, and whitespace are split points.
- [x] **DRE-TOKEN-004** [U]: `tokenize` shall exclude common English stopwords (articles, conjunctions, prepositions, pronouns, basic auxiliary verbs — e.g. `"and"`, `"the"`, `"for"`, `"not"`, `"with"`) from its output, for both identifier tokenization and free-text tokenization (`extract_text_tokens`). Found live: a multi-word feature slug like `client-signal-and-output` or `recording-and-export` literally contains a stopword as one of its hyphen-joined components: without this filter, `and` alone — present in nearly any sentence — made both slugs token-match almost every ticket, regardless of actual relevance, and outranked the ticket's actual correct feature match in the DRE's confidence-scored candidate list.
- [x] **DRE-TOKEN-002** [U]: `symptom_error_tokens` shall be built from `error_sigs + symptoms` only, excluding feature and module slug tokens. This prevents module-named elements (e.g. `DeviceCard` in the `device-card` module) from self-matching via the module's own name tokens.
- [x] **DRE-TOKEN-003** [U]: `func_anchor_tokens` shall be built from `symptom_error_tokens` plus the tokens of all `matched_elements` confirmed during element anchor matching. This set is used for function anchor matching.

---

## Element Anchor Matching

- [x] **DRE-ELEM-001** [U]: For each resolved module slug, the system shall compare registered element names (from `module_elements`) against `symptom_error_tokens`. An element matches if `_tokenize(element_name)` has a non-empty intersection with `symptom_error_tokens`.
- [x] **DRE-ELEM-002** [U]: When matching elements are found for a module, each source or test file already present in the evidence map shall receive an `element_anchor_match` evidence item with score 6.0. The explanation shall list the matching element names (up to 3), comma-separated.
- [x] **DRE-ELEM-003** [U]: Element anchor matching shall not add new files to the evidence map. It only adds evidence to files already present from feature/module traversal.
- [x] **DRE-ELEM-004** [U]: When element anchor matching produces matches, the tokens of each matched element shall be added to `matched_elements` for use by `func_anchor_tokens` in subsequent function anchor matching.

---

## Function Anchor Matching

- [x] **DRE-FUNC-001** [U]: For each recent commit, for each file it touched that is already in the evidence map, the system shall check `file_hunk_data[file_path]` for function definition names (from `+` diff lines) whose `_tokenize(def_name)` has a non-empty intersection with `func_anchor_tokens`.
- [x] **DRE-FUNC-002** [U]: When a matching function definition is found, the file shall receive a `function_anchor_match` evidence item with score 6.0. The explanation shall be `"{names} · {sha_short}"` where `names` is the matched function names (up to 3, comma-separated) and `sha_short` is the 7-character commit SHA.
- [x] **DRE-FUNC-003** [U]: Function anchor matching shall not add new files to the evidence map. It only adds evidence to files already present from earlier traversal steps.

---

## Candidate Scoring

- [x] **DRE-CAND-001** [U]: Each file's evidence items shall be scored by `_score_candidate`: positive items grouped by type, summed within each type group using geometric decay (first item at full value, second at ×0.5, third at ×0.25, etc.), plus a diversity bonus of 3.0 per unique *corroborating* positive evidence type beyond the first (capped at 4 types; see `DRE-CAND-006` for which types count as corroborating). Penalty items are summed directly.
- [x] **DRE-CAND-002** [U]: Non-source files (files whose extension is not in the source extension set, and are not an extensionless file under a `scripts/` directory) shall receive a `doc_penalty` evidence item equal to `raw_score × (0.25 - 1.0)`, computed after all other evidence is scored. The source extension set includes `.sh`. An extensionless file directly under a `scripts/` directory (e.g. `scripts/stagehand-wifi-provision`) is treated as source, not penalized — found live: shell scripts and extensionless deployment/provisioning scripts were classified as non-source and penalized identically to markdown documentation, dropping directly-relevant operational scripts to the bottom of the ranked candidate list.
- [x] **DRE-CAND-003** [U]: Each `ScoredCandidate` shall carry a `confidence` label: `"high"` for score ≥ 20.0, `"medium"` for score ≥ 10.0, `"low"` otherwise.
- [x] **DRE-CAND-004** [U]: Source candidates and test candidates shall be built and sorted separately (cap 20 each). `scored_candidates` shall be the merged and re-sorted list. `relevant_files` and `relevant_tests` shall be the ordered paths from each respective list.
- [x] **DRE-CAND-005** [U]: A `recent_commit` evidence item (a file touched by a recent commit, independent of whether that commit's diff matches an anchored symbol) shall have a score of 1.5. This is deliberately low relative to `function_anchor_match` (6.0, the same commit correlated with an anchored symbol via `DRE-FUNC-002`) per `docs/scoring-brainstorm.md` § Recency: bare recency is a weak, uncorroborated signal, not independent strong evidence — found live, at a higher weight (4.0) a string of unrelated commits on a frequently-edited file could out-rank a file directly named in the ticket but not recently touched.
- [x] **DRE-CAND-006** [U]: `recent_commit` and `feature_anchor` (the peripheral, non-primary variant introduced by `DRE-TRAV-009`) are non-corroborating types: they shall not, by themselves, count toward the number of unique evidence types used to compute the diversity bonus in `DRE-CAND-001` — but if a candidate has at least one evidence type outside this non-corroborating set (i.e. at least one genuinely direct piece of evidence), then all of that candidate's evidence types, including the non-corroborating ones, count toward the bonus. Both non-corroborating types still contribute their own decayed score regardless. Found live in two directions: (1) a file with only a broad `feature_anchor` match plus a handful of unrelated recent commits received the full +3.0 corroboration bonus just for having a second evidence *type* present, letting a frequently-edited-but-unrelated operational script (`pi-image/chroot-customize.sh`) outrank a file directly relevant to the ticket; (2) after excluding `recent_commit` from the bonus unconditionally, a file that was *already* well-evidenced (an `element_anchor_match` on the specific symptom, e.g. `reinit_requested`) lost deserved credit for its several genuinely corroborating recent commits, dropping its score well below the equivalent MODOK v1 result for the same ticket. The conditional rule fixes both: recency reinforces an already-plausible candidate, but cannot manufacture apparent strength from weak signals alone.
- [x] **DRE-CAND-007** [U]: For each recent commit, if the commit's own message (first line, tokenized) shares at least one token with `anchor_tokens` (the ticket's resolved feature/module slugs, error signatures, and symptoms), each file touched by that commit and already in the evidence map shall receive a `commit_message_match` evidence item with score 9.0. The explanation shall be `"{message} · {sha_short}"` (message truncated to 80 characters). This is a corroborating type (counts toward the `DRE-CAND-006` diversity bonus). Found live: a commit titled "fixed wifi provisioning" was indistinguishable, under `recent_commit` alone, from four other commits on the same file with unrelated messages ("cleaned up docs", etc.) — the commit's own message is a targeted, self-describing signal that a bare "touched recently" evidence item discards.

---

## Streaming

- [x] **DRE-STREAM-001** [U]: When `on_progress` is provided, the system shall call `on_progress("loading", partial_packet)` immediately after project slug verification and before LLM anchor extraction. The partial packet shall contain only `issue.summary`; all lists shall be empty and `summary` shall be `""`.
- [x] **DRE-STREAM-002** [U]: When `on_progress` is provided, the system shall call `on_progress("partial", partial_packet)` after traversal and scoring but before the `summarise_packet` LLM call. The partial packet shall have all evidence populated but `summary = ""`.

---

## LLM Summary

- [x] **DRE-SUMM-001** [U]: After scoring, the system shall call `gateway.summarise_packet` with `issue_text`, `module_slugs`, `error_signatures`, `symptoms`, `relevant_files`, `relevant_tests`, `matched_elements`, `recent_commits` (up to 5), and `known_issues`.
- [x] **DRE-SUMM-002** [U]: If `summarise_packet` raises any exception, `summary` shall fall back to `issue.summary`. The debug packet shall still be returned with all other fields populated.

---

## Debug Packet Structure

- [x] **DRE-PKT-001** [U]: `retrieve` shall return a `DebugPacket` containing `issue`, `affected_areas`, `relevant_files`, `relevant_tests`, `known_issues`, `prior_fixes`, `recent_commits`, `recent_dependency_changes` (docs/specs/dependency-graph-ingestion.md § DEPG-DRE-004), `covered_tests` (§ Test Coverage, DRE-TESTCOV-002), `scored_candidates`, and `summary`.
- [x] **DRE-PKT-002** [U]: Result sections with no matches shall be returned as empty lists, not omitted from the packet.
- [x] **DRE-PKT-003** [U]: `DebugPacket.issue.summary` shall be set to the `summary` field of the fetched `CustomerIssue` node.
- [x] **DRE-PKT-004** [U]: `DebugPacket.affected_areas` shall contain one `AffectedArea` per resolved feature slug (type `"feature"`) and one per resolved module slug (type `"module"`).
- [x] **DRE-PKT-005** [U]: Each `PriorFix` in `prior_fixes` shall include the fix `summary` and a `commit` field set to the commit SHA associated with the fix, or `""` if not available.

---

## Write Boundary

- [x] **DRE-WRITE-001** [U]: `retrieve` shall not call `upsert_node`, `write_edge`, `replace_edges`, or any Quine write method.
- [x] **DRE-WRITE-002** [U]: `retrieve` shall not write to any file on disk.

---

## Error Types

- [x] **DRE-ERR-001** [U]: When the `CustomerIssue` node is not found in Quine, the system shall raise `DRENotFoundError`.
- [x] **DRE-ERR-002** [U]: When the fetched `CustomerIssue.project_slug` does not match the `project_slug` argument, the system shall raise `DRENotFoundError`.
- [x] **DRE-ERR-003** [U]: When anchor extraction fails (no graph anchors, no `raw_text`, LLM parse failure, or LLM returns zero anchor instances), the system shall raise `DREAnchorError`.
- [x] **DRE-ERR-004** [U]: When Quine is unreachable at any point during retrieval, the system shall raise `DREGraphUnavailableError`.
- [x] **DRE-ERR-005** [U]: When the LLM gateway is unreachable during the fallback anchor extraction path, the system shall raise `DRELLMUnavailableError`.
