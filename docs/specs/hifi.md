# HiFi Specs

Specs for the HiFi differential test harness — `tests/hifi/`.

LLD: `docs/llds/hifi.md`

---

## Test Level Convention

- **[U]** — Unit test with mocked dependencies.
- **[P]** — Property test (`hypothesis`). Implies [U].
- **[G]** — Golden scenario test (data-driven YAML fixture). Implies [U].
- **[M]** — Metamorphic test (transformation-based).

---

## DummyQuine — Node Writes

- [ ] **DQ-NW-001** [U]: When `upsert_node` is called for a node that does not exist in DummyQuine, the system shall store the node in `_nodes` keyed by the node's `idFrom`-derived ID.
- [ ] **DQ-NW-002** [U]: When `upsert_node` is called for a node that already exists, the system shall overwrite the stored node with the new value (last write wins).

---

## DummyQuine — Node Reads

- [ ] **DQ-NR-001** [U]: When `get_node` is called for a node ID that exists in `_nodes`, the system shall return the stored node cast to the requested type.
- [ ] **DQ-NR-002** [U]: When `get_node` is called for a node ID that does not exist in `_nodes`, the system shall raise `QuineNodeNotFoundError`.
- [ ] **DQ-NR-003** [U]: When `node_exists` is called for a node ID that exists in `_nodes`, the system shall return `True`.
- [ ] **DQ-NR-004** [U]: When `node_exists` is called for a node ID that does not exist in `_nodes`, the system shall return `False`.

---

## DummyQuine — Edge Writes

- [ ] **DQ-EW-001** [U]: When `write_edge` is called for an edge that does not exist in `_edges`, the system shall append `(from_id, edge_type, to_id)` to `_edges`.
- [ ] **DQ-EW-002** [U]: When `write_edge` is called for an edge that already exists in `_edges`, the system shall not append a duplicate tuple.
- [ ] **DQ-EW-003** [U]: When `replace_edges` is called, the system shall remove all tuples `(from_id, edge_type, *)` from `_edges` before appending new tuples for each supplied `to_id`.
- [ ] **DQ-EW-004** [U]: When `replace_edges` is called with an empty `to_ids` list, the system shall remove all matching tuples and append nothing.
- [ ] **DQ-EW-005** [U]: When `edge_exists` is called for a `(from_id, edge_type, to_id)` tuple present in `_edges`, the system shall return `True`.
- [ ] **DQ-EW-006** [U]: When `edge_exists` is called for a `(from_id, edge_type, to_id)` tuple not present in `_edges`, the system shall return `False`.

---

## DummyQuine — Query Dispatch

- [ ] **DQ-QD-001** [U]: When `query` is called with a Cypher string containing `"AFFECTS]->(f:Feature"`, the system shall return rows of the form `[{"id": node_id, "properties": {...}}]` for all `Feature` nodes reachable from the `issue_id` parameter via an `AFFECTS` edge, filtered to `project_slug`.
- [ ] **DQ-QD-002** [U]: When `query` is called with a Cypher string containing `"HAS_ERROR]->(e:ErrorSignature"`, the system shall return rows for all `ErrorSignature` nodes reachable from `issue_id` via a `HAS_ERROR` edge, filtered to `project_slug`.
- [ ] **DQ-QD-003** [U]: When `query` is called with a Cypher string containing `"IMPLEMENTED_BY]->(m:Module)-[:DEFINED_IN]->(file:File)"`, the system shall return rows for all `File` nodes reachable from the named `Feature` via `IMPLEMENTED_BY` then `DEFINED_IN` edges.
- [ ] **DQ-QD-004** [U]: When `query` is called with a Cypher string containing `"HAS_ERROR]-(ki:KnownIssue)"`, the system shall return rows for all `KnownIssue` nodes that have an outbound `HAS_ERROR` edge to the named `ErrorSignature` (reverse walk — edges stored as KnownIssue→ErrorSignature).
- [ ] **DQ-QD-005** [U]: When `query` is called with a Cypher string containing `"RESOLVED_BY]->(fix:Fix"`, the system shall return rows for all `Fix` nodes reachable from `ki_node_id` via a `RESOLVED_BY` edge, filtered to `project_slug`.
- [ ] **DQ-QD-006** [U]: When `query` is called with a Cypher string containing `"HAS_SIMILARITY_MATCH]->(sm:SimilarityMatch)-[:MATCHES]->(ki:KnownIssue"`, the system shall return rows for all `KnownIssue` nodes reachable from `issue_id` via `HAS_SIMILARITY_MATCH` then `MATCHES` edges, including the `review_status` of the intermediate `SimilarityMatch` node, filtered to `review_status IN ['candidate', 'confirmed']`.
- [ ] **DQ-QD-007** [U]: When `query` is called with a Cypher string that matches no registered fingerprint, the system shall emit a warning identifying the unrecognized Cypher string and return an empty list in the same call.
- [ ] **DQ-QD-008** [U]: When `query` returns node rows, each row shall be a list containing one dict with keys `"id"` (the integer node ID) and `"properties"` (the node's model fields as a dict).

---

## DummyQuine — Lifecycle

- [ ] **DQ-LC-001** [U]: `ping()` shall always return `True`.

---

## Reference Model — Ingest

- [ ] **REF-ING-001** [U]: When `ReferenceModok.ingest(nodes, edges)` is called, the system shall store each node keyed by its `idFrom`-derived ID.
- [ ] **REF-ING-002** [U]: When `ReferenceModok.ingest` is called with the same node twice, the system shall store exactly one entry for that node (idempotent).
- [ ] **REF-ING-003** [U]: When `ReferenceModok.ingest` is called with the same edge twice, the system shall store exactly one entry for that edge (idempotent).
- [ ] **REF-ING-004** [U]: `ReferenceModok.ingest` shall apply the same `idFrom` ID scheme as the production ingestion pipeline for all node types present in HiFi scenarios.

---

## Reference Model — Retrieval

- [ ] **REF-RET-001** [U]: When `ReferenceModok.retrieve(issue_id, project_slug)` is called and the `CustomerIssue` node does not exist, the system shall raise `DRENotFoundError`.
- [ ] **REF-RET-002** [U]: When `ReferenceModok.retrieve` is called and the `CustomerIssue` belongs to a different project, the system shall raise `DRENotFoundError`.
- [ ] **REF-RET-003** [U]: When `ReferenceModok.retrieve` is called, the system shall extract feature slugs from `AFFECTS` edges and error signatures from `HAS_ERROR` edges on the `CustomerIssue`.
- [ ] **REF-RET-004** [U]: When `ReferenceModok.retrieve` is called, the system shall follow `Feature -IMPLEMENTED_BY-> Module -DEFINED_IN-> File` to populate `relevant_files`.
- [ ] **REF-RET-005** [U]: When `ReferenceModok.retrieve` is called, the system shall follow `ErrorSignature <-HAS_ERROR- KnownIssue` to populate `known_issues`.
- [ ] **REF-RET-006** [U]: When `ReferenceModok.retrieve` is called, the system shall follow `KnownIssue -RESOLVED_BY-> Fix` to populate `recent_fixes`.
- [ ] **REF-RET-007** [U]: When `ReferenceModok.retrieve` is called, the system shall rank `known_issues`, `recent_fixes`, and `relevant_files` by match count descending, applying the same caps as the DRE (`_KI_CAP=10`, `_FIX_CAP=10`, `_FILE_CAP=20`).
- [ ] **REF-RET-008** [U]: When `ReferenceModok.retrieve` is called and at least one anchor matched a graph node, the returned `DebugPacket` shall have `confidence > 0`.
- [ ] **REF-RET-009** [U]: `ReferenceModok.retrieve` shall not return nodes belonging to a project other than `project_slug`.

---

## Harness — Scenario Loading

- [ ] **HFI-LOAD-001** [U]: When `load_scenario(yaml_path)` is called, the system shall parse the YAML file and return a `Scenario` with `nodes`, `edges`, `query`, and `expected` fields populated.
- [ ] **HFI-LOAD-002** [U]: When loading a scenario, the system shall compute integer node IDs for all node and edge references using `idFrom` applied to the tuple form declared in the YAML.
- [ ] **HFI-LOAD-003** [U]: When loading a scenario, the system shall resolve edge `from` and `to` references to the same integer IDs computed for the corresponding node declarations.

---

## Harness — Scenario Execution

- [ ] **HFI-RUN-001** [U]: When `run_scenario(scenario)` is called, the system shall construct a fresh `DummyQuine` instance internally; callers shall not supply or share a `DummyQuine` across calls.
- [ ] **HFI-RUN-002** [U]: When `run_scenario(scenario)` is called, the system shall write all nodes to the internally constructed DummyQuine via `upsert_node` before writing any edges via `write_edge`.
- [ ] **HFI-RUN-003** [U]: When `run_scenario(scenario)` is called, the system shall supply precomputed integer node IDs (computed once at load time by `load_scenario`) to both DummyQuine and the reference model; neither system shall recompute IDs independently from raw field values.
- [ ] **HFI-RUN-004** [U]: When `run_scenario(scenario)` is called, the system shall call `retrieve(issue_id, project_slug, dummy_quine)` to produce the actual debug packet.
- [ ] **HFI-RUN-005** [U]: When `run_scenario(scenario)` is called, the system shall call `ReferenceModok.retrieve(issue_id, project_slug)` (after loading the same nodes and edges into the reference model) to produce the expected debug packet.
- [ ] **HFI-RUN-006** [U]: Because `run_scenario` constructs a fresh `DummyQuine` per call, nodes and edges from one scenario run shall not be visible to any subsequent scenario run.

---

## Harness — Comparison

- [ ] **HFI-CMP-001** [U]: `assert_packets_equivalent(expected, actual)` shall pass when every ID in `expected.known_issues` is present in `actual.known_issues`, every ID in `expected.recent_fixes` is present in `actual.recent_fixes`, and every path in `expected.relevant_files` is present in `actual.relevant_files`.

---

## Golden Scenarios

- [ ] **GS-001** [G]: `feature_to_files` — given a `CustomerIssue` with an `AFFECTS` edge to a `Feature` that has `IMPLEMENTED_BY` and `DEFINED_IN` edges to a `Module` and `File`, both the reference model and real MODOK shall produce a `DebugPacket` whose `relevant_files` includes the file's `repo_path`, and `assert_packets_equivalent` shall pass.
- [ ] **GS-002** [G]: `error_to_known_issue` — given a `CustomerIssue` with a `HAS_ERROR` edge to an `ErrorSignature` that has an inbound `HAS_ERROR` edge from a `KnownIssue`, both the reference model and real MODOK shall produce a `DebugPacket` whose `known_issues` includes the known issue's `issue_id`, and `assert_packets_equivalent` shall pass.
- [ ] **GS-003** [G]: `known_issue_to_fix` — given the graph from GS-002 extended with a `RESOLVED_BY` edge from the `KnownIssue` to a `Fix`, both systems shall produce a `DebugPacket` whose `recent_fixes` includes the fix's `fix_id`, and `assert_packets_equivalent` shall pass.
- [ ] **GS-004** [G]: `idempotent_reingest` — running `run_scenario` twice with the same scenario shall produce `DebugPacket` outputs where `assert_packets_equivalent(actual_run1, actual_run2)` passes (the two actual packets agree with each other, verifying idempotence rather than reference-model agreement).
- [ ] **GS-005** [G]: `cross_project_isolation` — given a scenario with two projects sharing the same `feature_slug`, queried against project A, both systems shall produce a `DebugPacket` containing only project-A nodes, and `assert_packets_equivalent` shall pass with project-B nodes absent.

---

## Property Tests

- [ ] **PT-001** [P]: For any set of valid `QuineNode` instances written to DummyQuine via `upsert_node`, `node_exists(idFrom(...))` shall return `True` for every written node.
- [ ] **PT-002** [P]: For any valid ingest scenario, writing the same set of nodes and edges twice shall produce the same `_nodes` and `_edges` state as writing them once (idempotence).
- [ ] **PT-003** [P]: For any `DebugPacket` returned by `retrieve` using DummyQuine, every `known_issue_id` string in `packet.known_issues` shall have a corresponding entry in DummyQuine `_nodes` at the integer key `idFrom("known-issue", project_slug, known_issue_id)`.
- [ ] **PT-004** [P]: For any `DebugPacket` returned by `retrieve` using DummyQuine with `project_slug="A"`, the `known_issue_id`, `fix_id`, and `repo_path` values in all packet sections shall each resolve (via `idFrom`) to a node in DummyQuine `_nodes` whose `project_slug` field equals `"A"`.

---

## Metamorphic Tests

- [ ] **MT-001** [M]: Shuffling the order of nodes and edges in a scenario's input shall not change the set of IDs in the `DebugPacket` produced by real MODOK via DummyQuine.
- [ ] **MT-002** [M]: Adding a duplicate node or edge entry to a scenario's input shall not change the `DebugPacket` produced by real MODOK via DummyQuine (idempotence under duplication).
- [ ] **MT-003** [M]: Adding a node and edges from an unrelated project to a scenario shall not change the `DebugPacket` produced by real MODOK via DummyQuine for the original project.
- [ ] **MT-004** [M]: Adding a new `Fix` node with a `RESOLVED_BY` edge from an existing `KnownIssue` in a scenario shall cause the fix's `fix_id` to appear in the `DebugPacket`'s `recent_fixes` when run through real MODOK via DummyQuine.
- [ ] **MT-005** [M]: Adding a new `AFFECTS` edge from the `CustomerIssue` to an additional `Feature` shall cause files reachable from that feature to appear in `relevant_files` when run through real MODOK via DummyQuine.
