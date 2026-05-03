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

- [x] **DRE-IFACE-001** [U]: `retrieve(issue_id, project_slug)` shall fetch the `CustomerIssue` node for `issue_id` and verify that its `project_slug` matches the argument. If the node is not found or the `project_slug` does not match, the system shall raise `DRENotFoundError` without performing any traversal.
- [x] **DRE-IFACE-002** [U]: When `project_slug` does not match the fetched node, the system shall raise `DRENotFoundError` before issuing any Cypher query. `project_slug` is always the caller-supplied argument; it is never derived from graph state.
- [x] **DRE-IFACE-003** [U]: When Quine is unreachable, `retrieve` shall raise `DREGraphUnavailableError`.

---

## Anchor Extraction

- [x] **DRE-ANCH-001** [U]: When the `CustomerIssue` node has outbound `AFFECTS` edges to `Feature` nodes in the same project, the system shall use those feature slugs as anchors and shall not call the LLM gateway.
- [x] **DRE-ANCH-002** [U]: When the `CustomerIssue` node has outbound `HAS_ERROR` edges to `ErrorSignature` nodes in the same project, the system shall use those normalized error strings as anchors and shall not call the LLM gateway.
- [x] **DRE-ANCH-003** [U]: When graph anchors are found — at least one feature slug or one error signature after project-scoped filtering — the LLM fallback shall be skipped entirely, regardless of the `backend` parameter. Edges that exist but point to nodes in a different project do not count toward sufficiency.
- [x] **DRE-ANCH-004** [U]: When no graph anchors are found and `CustomerIssue.raw_text` is present, the system shall call `gateway.parse_ticket(raw_text, project_slug, backend=backend)` and use the returned `feature_slug` and `error_signatures` as anchors.
- [x] **DRE-ANCH-005** [U]: When no graph anchors are found and `CustomerIssue.raw_text` is `None`, the system shall raise `DREAnchorError`.
- [x] **DRE-ANCH-006** [U]: When `parse_ticket` raises `LLMResponseError`, the system shall raise `DREAnchorError`.
- [x] **DRE-ANCH-007** [U]: When `parse_ticket` raises `LLMUnavailableError`, the system shall raise `DRELLMUnavailableError`.
- [x] **DRE-ANCH-008** [U]: `symptoms` returned by `parse_ticket` shall be included in `AnchorSet.symptoms` but shall not be used in graph traversal, match count scoring, or evidence anchors. `symptoms` shall not appear as an `anchor_type` in any `EvidenceAnchor`.

---

## Graph Traversal

- [x] **DRE-TRAV-001** [U]: For each feature slug anchor, the system shall traverse `Feature -[:IMPLEMENTED_BY]-> Module -[:DEFINED_IN]-> File` within the project and add matching `File` nodes to `relevant_files`.
- [x] **DRE-TRAV-002** [U]: For each error signature anchor, the system shall traverse `ErrorSignature <-[:HAS_ERROR]- KnownIssue` within the project and add matching `KnownIssue` nodes to `known_issues`.
- [x] **DRE-TRAV-003** [U]: For each `KnownIssue` found via error signature traversal, the system shall traverse `KnownIssue -[:RESOLVED_BY]-> Fix` and add matching `Fix` nodes to `recent_fixes`.
- [x] **DRE-TRAV-004** [U]: The system shall traverse `CustomerIssue -[:HAS_SIMILARITY_MATCH]-> SimilarityMatch -[:MATCHES]-> KnownIssue` and include `KnownIssue` nodes where `SimilarityMatch.review_status` is `"candidate"` or `"confirmed"`. Nodes where `review_status` is `"rejected"` shall be excluded.
- [x] **DRE-TRAV-005** [U]: All traversal queries shall include `project_slug` as a parameter. The DRE shall not return nodes from a different project even if reachable via graph traversal.
- [x] **DRE-TRAV-006** [U]: All traversals shall use `QuineClient.query()` with explicit Cypher. The DRE shall not use `QuineClient.traverse()`.

---

## Weighted Match Count and Prioritization

- [x] **DRE-SCORE-001** [U]: Each result item shall carry a `match_count` initialised to `1` on first appearance. Each additional anchor that produces the same item shall increment `match_count` by `1`. Match count accumulates across all traversal sources with no upper bound other than the result cap.
- [x] **DRE-SCORE-002** [U]: A `KnownIssue` reached via a `confirmed` `SimilarityMatch` shall have its `match_count` incremented by `2`. A `KnownIssue` reached via a `candidate` `SimilarityMatch` shall have its `match_count` incremented by `1`.
- [x] **DRE-SCORE-003** [P]: After all traversals complete, each result list shall be sorted descending by `match_count`. For any input, the output sequence shall be non-increasing by `match_count`. Items with equal `match_count` preserve insertion order (first-found).
- [x] **DRE-SCORE-004** [P]: `known_issues` shall be capped at 10 items after sorting. `recent_fixes` shall be capped at 10 items. `relevant_files` shall be capped at 20 items. For any input, every retained item shall have `match_count` greater than or equal to every dropped item.
- [x] **DRE-SCORE-005** [P]: For any two result items A and B where A was matched by more anchors than B, A shall appear before B in its result list.
- [x] **DRE-SCORE-006** [P]: `Fix` nodes shall have their `match_count` incremented by `1` for each `KnownIssue -[:RESOLVED_BY]-> Fix` hop that fires during traversal. If N `KnownIssue` nodes resolve to the same `Fix`, the `Fix` shall have `match_count` equal to N from those hops alone.

---

## Confidence

- [x] **DRE-CONF-001** [U]: `confidence` shall be computed as the number of anchor instances that produced at least one result divided by the total number of anchor instances. Each feature slug counts as one instance; each error signature string counts as one instance.
- [x] **DRE-CONF-002** [U]: When the total number of anchor instances is zero — whether because no anchors were extracted or because anchor extraction succeeded but returned no feature slugs and no error signatures — `confidence` shall be `0.0`. The implementation raises `DREAnchorError` when zero anchor instances result, which enforces this invariant by preventing a zero-confidence debug packet from being returned.
- [x] **DRE-CONF-003** [P]: For any combination of anchor count and match count, `confidence` shall be a float in the range `[0.0, 1.0]`.

---

## Debug Packet Structure

- [x] **DRE-PKT-001** [U]: `retrieve` shall return a `DebugPacket` containing `issue_summary`, `anchors`, `anchor_count`, `known_issues`, `recent_fixes`, `relevant_files`, `evidence`, and `confidence`.
- [x] **DRE-PKT-002** [U]: Result sections with no matches shall be returned as empty lists, not omitted from the packet.
- [x] **DRE-PKT-003** [U]: `DebugPacket.issue_summary` shall be set to the `summary` field of the fetched `CustomerIssue` node.
- [x] **DRE-PKT-004** [U]: `DebugPacket.evidence` shall contain one `EvidenceAnchor` per anchor instance that produced at least one result, recording the anchor type, anchor value, and list of matched node IDs.
- [x] **DRE-PKT-005** [U]: `DebugPacket.anchor_count` shall be set to the total number of anchor instances (feature slugs + error signature strings) used during traversal, including those that produced no results.

- [x] **DRE-PKT-006** [U]: Each `FixRef` in `recent_fixes` shall include a `pr_url` field set to the `pr_url` property of the corresponding `Fix` node, or `None` if the property is absent.

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
