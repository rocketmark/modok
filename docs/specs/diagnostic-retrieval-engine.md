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

- [ ] **DRE-IFACE-001** [U]: `retrieve(issue_id, project_slug)` shall fetch the `CustomerIssue` node for `issue_id` and verify that its `project_slug` matches the argument. If the node is not found or the `project_slug` does not match, the system shall raise `DRENotFoundError` without performing any traversal.
- [ ] **DRE-IFACE-002** [U]: When `project_slug` does not match the fetched node, the system shall raise `DRENotFoundError` before issuing any Cypher query. `project_slug` is always the caller-supplied argument; it is never derived from graph state.
- [ ] **DRE-IFACE-003** [U]: When Quine is unreachable, `retrieve` shall raise `DREGraphUnavailableError`.

---

## Anchor Extraction

- [ ] **DRE-ANCH-001** [U]: When the `CustomerIssue` node has outbound `AFFECTS` edges to `Feature` nodes in the same project, the system shall use those feature slugs as anchors and shall not call the LLM gateway.
- [ ] **DRE-ANCH-002** [U]: When the `CustomerIssue` node has outbound `HAS_ERROR` edges to `ErrorSignature` nodes in the same project, the system shall use those normalized error strings as anchors and shall not call the LLM gateway.
- [ ] **DRE-ANCH-003** [U]: When graph anchors are found — at least one feature slug or one error signature after project-scoped filtering — the LLM fallback shall be skipped entirely, regardless of the `backend` parameter. Edges that exist but point to nodes in a different project do not count toward sufficiency.
- [ ] **DRE-ANCH-004** [U]: When no graph anchors are found and `CustomerIssue.raw_text` is present, the system shall call `gateway.parse_ticket(raw_text, project_slug, backend=backend)` and use the returned `feature_slug` and `error_signatures` as anchors.
- [ ] **DRE-ANCH-005** [U]: When no graph anchors are found and `CustomerIssue.raw_text` is `None`, the system shall raise `DREAnchorError`.
- [ ] **DRE-ANCH-006** [U]: When `parse_ticket` raises `LLMResponseError`, the system shall raise `DREAnchorError`.
- [ ] **DRE-ANCH-007** [U]: When `parse_ticket` raises `LLMUnavailableError`, the system shall raise `DRELLMUnavailableError`.
- [ ] **DRE-ANCH-008** [U]: `symptoms` returned by `parse_ticket` shall be included in `AnchorSet.symptoms` but shall not be used in graph traversal, match count scoring, or evidence anchors. `symptoms` shall not appear as an `anchor_type` in any `EvidenceAnchor`.

---

## Graph Traversal

- [ ] **DRE-TRAV-001** [U]: For each feature slug anchor, the system shall traverse `Feature -[:IMPLEMENTED_BY]-> Module -[:DEFINED_IN]-> File` within the project and add matching `File` nodes to `relevant_files`.
- [ ] **DRE-TRAV-002** [U]: For each error signature anchor, the system shall traverse `ErrorSignature <-[:HAS_ERROR]- KnownIssue` within the project and add matching `KnownIssue` nodes to `known_issues`.
- [ ] **DRE-TRAV-003** [U]: For each `KnownIssue` found via error signature traversal, the system shall traverse `KnownIssue -[:RESOLVED_BY]-> Fix` and add matching `Fix` nodes to `recent_fixes`.
- [ ] **DRE-TRAV-004** [U]: The system shall traverse `CustomerIssue -[:HAS_SIMILARITY_MATCH]-> SimilarityMatch -[:MATCHES]-> KnownIssue` and include `KnownIssue` nodes where `SimilarityMatch.review_status` is `"candidate"` or `"confirmed"`. Nodes where `review_status` is `"rejected"` shall be excluded.
- [ ] **DRE-TRAV-005** [U]: All traversal queries shall include `project_slug` as a parameter. The DRE shall not return nodes from a different project even if reachable via graph traversal.
- [ ] **DRE-TRAV-006** [U]: All traversals shall use `QuineClient.query()` with explicit Cypher. The DRE shall not use `QuineClient.traverse()`.

---

## Weighted Match Count and Prioritization

- [ ] **DRE-SCORE-001** [U]: Each result item shall carry a `match_count` initialised to `1` on first appearance. Each additional anchor that produces the same item shall increment `match_count` by `1`. Match count accumulates across all traversal sources with no upper bound other than the result cap.
- [ ] **DRE-SCORE-002** [U]: A `KnownIssue` reached via a `confirmed` `SimilarityMatch` shall have its `match_count` incremented by `2`. A `KnownIssue` reached via a `candidate` `SimilarityMatch` shall have its `match_count` incremented by `1`.
- [ ] **DRE-SCORE-003** [P]: After all traversals complete, each result list shall be sorted descending by `match_count`. For any input, the output sequence shall be non-increasing by `match_count`. Items with equal `match_count` preserve insertion order (first-found).
- [ ] **DRE-SCORE-004** [P]: `known_issues` shall be capped at 10 items after sorting. `recent_fixes` shall be capped at 10 items. `relevant_files` shall be capped at 20 items. For any input, every retained item shall have `match_count` greater than or equal to every dropped item.
- [ ] **DRE-SCORE-005** [P]: For any two result items A and B where A was matched by more anchors than B, A shall appear before B in its result list.
- [ ] **DRE-SCORE-006** [P]: `Fix` nodes shall have their `match_count` incremented by `1` for each `KnownIssue -[:RESOLVED_BY]-> Fix` hop that fires during traversal. If N `KnownIssue` nodes resolve to the same `Fix`, the `Fix` shall have `match_count` equal to N from those hops alone.

---

## Confidence

- [ ] **DRE-CONF-001** [U]: `confidence` shall be computed as the number of anchor instances that produced at least one result divided by the total number of anchor instances. Each feature slug counts as one instance; each error signature string counts as one instance.
- [ ] **DRE-CONF-002** [U]: When the total number of anchor instances is zero — whether because no anchors were extracted or because anchor extraction succeeded but returned no feature slugs and no error signatures — `confidence` shall be `0.0`.
- [ ] **DRE-CONF-003** [P]: For any combination of anchor count and match count, `confidence` shall be a float in the range `[0.0, 1.0]`.

---

## Debug Packet Structure

- [ ] **DRE-PKT-001** [U]: `retrieve` shall return a `DebugPacket` containing `issue_summary`, `anchors`, `anchor_count`, `known_issues`, `recent_fixes`, `relevant_files`, `evidence`, and `confidence`.
- [ ] **DRE-PKT-002** [U]: Result sections with no matches shall be returned as empty lists, not omitted from the packet.
- [ ] **DRE-PKT-003** [U]: `DebugPacket.issue_summary` shall be set to the `summary` field of the fetched `CustomerIssue` node.
- [ ] **DRE-PKT-004** [U]: `DebugPacket.evidence` shall contain one `EvidenceAnchor` per anchor instance that produced at least one result, recording the anchor type, anchor value, and list of matched node IDs.
- [ ] **DRE-PKT-005** [U]: `DebugPacket.anchor_count` shall be set to the total number of anchor instances (feature slugs + error signature strings) used during traversal, including those that produced no results.

---

## Write Boundary

- [ ] **DRE-WRITE-001** [U]: `retrieve` shall not call `upsert_node`, `write_edge`, `replace_edges`, or any Quine write method.
- [ ] **DRE-WRITE-002** [U]: `retrieve` shall not write to any file on disk.

---

## Error Types

- [ ] **DRE-ERR-001** [U]: When the `CustomerIssue` node is not found in Quine, the system shall raise `DRENotFoundError`.
- [ ] **DRE-ERR-002** [U]: When the fetched `CustomerIssue.project_slug` does not match the `project_slug` argument, the system shall raise `DRENotFoundError`.
- [ ] **DRE-ERR-003** [U]: When anchor extraction fails (no graph anchors, no `raw_text`, LLM parse failure, or LLM returns zero anchor instances), the system shall raise `DREAnchorError`.
- [ ] **DRE-ERR-004** [U]: When Quine is unreachable at any point during retrieval, the system shall raise `DREGraphUnavailableError`.
- [ ] **DRE-ERR-005** [U]: When the LLM gateway is unreachable during the fallback anchor extraction path, the system shall raise `DRELLMUnavailableError`.
