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

- [x] **DRE-ANCH-001** [U]: When the `CustomerIssue` node has outbound `AFFECTS` edges to `Feature` nodes in the same project, the system shall use those feature slugs as anchors and shall not call the LLM gateway.
- [x] **DRE-ANCH-002** [U]: When the `CustomerIssue` node has outbound `HAS_ERROR` edges to `ErrorSignature` nodes in the same project, the system shall use those normalized error strings as anchors and shall not call the LLM gateway.
- [x] **DRE-ANCH-003** [U]: When graph anchors are found — at least one feature slug or one error signature after project-scoped filtering — the LLM fallback (i.e., the `parse_ticket` call) shall be skipped entirely, regardless of the `backend` parameter.
- [x] **DRE-ANCH-004** [U]: When no graph anchors are found and `CustomerIssue.raw_text` is present, the system shall call `gateway.parse_ticket(raw_text, project_slug, backend=backend)` and use the returned `feature_slugs` and `error_signatures` as anchors.
- [x] **DRE-ANCH-005** [U]: When no graph anchors are found and `CustomerIssue.raw_text` is `None`, the system shall raise `DREAnchorError`.
- [x] **DRE-ANCH-006** [U]: When `parse_ticket` raises `LLMResponseError`, the system shall raise `DREAnchorError`.
- [x] **DRE-ANCH-007** [U]: When `parse_ticket` raises `LLMUnavailableError`, the system shall raise `DRELLMUnavailableError`.
- [x] **DRE-ANCH-008** [U]: `symptoms` returned by `parse_ticket` shall be stored in `IssueAnchors.symptoms` for context but shall not be used in graph traversal or match count scoring.
- [x] **DRE-ANCH-009** [U]: When no graph anchors are found and `raw_text` is present, the system shall pre-scan `raw_text` for literal source file path mentions using `module_source_files` and seed matching module slugs before calling `parse_ticket`. Pre-matched slugs shall appear first in the merged `feature_slugs` list.

---

## Graph Traversal

- [x] **DRE-TRAV-001** [U]: For each feature slug anchor, the system shall first attempt to traverse `Feature -[:IMPLEMENTED_BY]-> Module -[:DEFINED_IN]-> File` and `Feature -[:HAS_TEST]-> TestFile`. If no files are found via Feature, the system shall fall back to treating the slug as a Module slug and traverse `Module -[:DEFINED_IN]-> File`. Source files found shall receive `feature_anchor` evidence (score 7.0); test files shall receive `test_coverage` evidence (score 8.0).
- [x] **DRE-TRAV-002** [U]: For each error signature anchor, the system shall traverse `ErrorSignature <-[:HAS_ERROR]- KnownIssue` within the project and accumulate matching `KnownIssue` nodes by match count.
- [x] **DRE-TRAV-003** [U]: For each `KnownIssue` found via error signature traversal, the system shall traverse `KnownIssue -[:RESOLVED_BY]-> Fix` and accumulate matching `Fix` nodes by match count.
- [x] **DRE-TRAV-004** [U]: The system shall traverse `CustomerIssue -[:HAS_SIMILARITY_MATCH]-> SimilarityMatch -[:MATCHES]-> KnownIssue` and include `KnownIssue` nodes where `SimilarityMatch.review_status` is `"candidate"` or `"confirmed"`. Nodes where `review_status` is `"rejected"` shall be excluded.
- [x] **DRE-TRAV-005** [U]: All traversal queries shall include `project_slug` as a parameter. The DRE shall not return nodes from a different project even if reachable via graph traversal.
- [x] **DRE-TRAV-006** [U]: All traversals shall use `QuineClient.query()` with explicit Cypher. The DRE shall not use `QuineClient.traverse()`.
- [x] **DRE-TRAV-007** [U]: After the preliminary file list is established from feature/module traversal, the system shall query recent `Commit` nodes that have `TOUCHES` edges to those files, sorted by timestamp descending, capped at 10. Each result shall include the commit's `sha`, `timestamp`, `author_name`, `message`, and `files_touched`. The `file_hunks` JSON property on each `Commit` node shall be parsed into `file_hunk_data` for use in function anchor matching.

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

- [x] **DRE-TOKEN-001** [U]: `_tokenize(name)` shall split a camelCase, snake_case, or kebab-case identifier into lowercase tokens of length greater than 2. CamelCase boundaries shall be detected via regex; underscore, hyphen, and whitespace are split points.
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

- [x] **DRE-CAND-001** [U]: Each file's evidence items shall be scored by `_score_candidate`: positive items grouped by type, summed within each type group using geometric decay (first item at full value, second at ×0.5, third at ×0.25, etc.), plus a diversity bonus of 3.0 per unique positive evidence type beyond the first (capped at 4 types). Penalty items are summed directly.
- [x] **DRE-CAND-002** [U]: Non-source files (files whose extension is not in the source extension set) shall receive a `doc_penalty` evidence item equal to `raw_score × (0.25 - 1.0)`, computed after all other evidence is scored.
- [x] **DRE-CAND-003** [U]: Each `ScoredCandidate` shall carry a `confidence` label: `"high"` for score ≥ 20.0, `"medium"` for score ≥ 10.0, `"low"` otherwise.
- [x] **DRE-CAND-004** [U]: Source candidates and test candidates shall be built and sorted separately (cap 20 each). `scored_candidates` shall be the merged and re-sorted list. `relevant_files` and `relevant_tests` shall be the ordered paths from each respective list.

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

- [x] **DRE-PKT-001** [U]: `retrieve` shall return a `DebugPacket` containing `issue`, `affected_areas`, `relevant_files`, `relevant_tests`, `known_issues`, `prior_fixes`, `recent_commits`, `scored_candidates`, and `summary`.
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
