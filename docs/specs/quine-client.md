# Quine Client Specs

Specs for `modok.quine` — the typed, multi-project-aware interface to Quine's HTTP API.

LLD: `docs/llds/quine-client.md`

---

## Test Level Convention

Every spec carries a test level annotation in brackets after the ID:

- **[U]** — Unit test. At least one `@spec`-annotated test directly exercises the specified behavior with mocked dependencies.
- **[P]** — Property test (`hypothesis`). The invariant must hold across arbitrary inputs, not just handpicked examples. Applied to specs containing "shall always", "shall never", or whose trigger space is too large for exhaustive examples.
- **[C]** — Contract test. Runs against a live local Quine instance (Docker). Applied to specs whose correctness depends on Quine's actual wire behavior, not just our model of it.

Levels are cumulative: `[P]` implies `[U]`; `[C]` implies `[U]`. A spec marked `[P, C]` requires all three.

---

## ID Scheme

- [ ] **QC-ID-001** [P, C]: The system shall compute all Quine node IDs deterministically via `idFrom(*parts)`, where parts are joined with a null-byte separator and hashed with SHA-256, taking the first 8 bytes as a signed int64.
- [ ] **QC-ID-002** [P]: The system shall use the node type name as the first element of every `idFrom()` tuple, such that two node types with identical remaining parts always produce different IDs.
- [ ] **QC-ID-003** [P]: The system shall include `project_slug` as a tuple element in the ID of every node type except `CustomerIssue` and `SimilarityMatch`.
- [ ] **QC-ID-004** [U]: The system shall identify `CustomerIssue` nodes by `('customer-issue', source_system, ticket_id)`, without `project_slug`, because tickets arrive from external systems before project linkage is established.
- [ ] **QC-ID-005** [U]: The system shall identify `ResolutionEvent` nodes by `('resolution', project_slug, source_system, ticket_id, fix_id)`, including `source_system` to disambiguate ticket IDs that collide across source systems.

---

## Node Writes

- [ ] **QC-NW-001** [U, C]: When `upsert_node` is called for a node that does not exist in Quine, the system shall create the node with all properties from the pydantic model.
- [ ] **QC-NW-002** [P, C]: When `upsert_node` is called for a node that already exists in Quine, the system shall replace all node properties with the current pydantic model's values, removing any properties not present in the current model.
- [ ] **QC-NW-003** [P, C]: When `upsert_node` is called, the system shall not modify any edges on the node.

---

## Node Reads

- [ ] **QC-NR-001** [U, C]: When `get_node` is called for a node ID that exists in Quine, the system shall return the node deserialized into the requested pydantic model type.
- [ ] **QC-NR-002** [U, C]: When `get_node` is called for a node ID that does not exist in Quine, the system shall raise `QuineNodeNotFoundError`.
- [ ] **QC-NR-003** [U]: The system shall provide `node_exists(node_id)` as the opt-in check for callers that need to test node presence without triggering an exception.

---

## Edge Writes

- [ ] **QC-EW-001** [U, C]: When `write_edge` is called for an edge that does not exist, the system shall create the directed edge from `from_id` to `to_id` with the given `edge_type`.
- [ ] **QC-EW-002** [P, C]: When `write_edge` is called for an edge that already exists, the system shall treat the call as a no-op without raising an error.

---

## Traversal

- [ ] **QC-TR-001** [U, C]: When `traverse` is called, the system shall execute the traversal as a Cypher query against Quine's `/api/v1/query/cypher` endpoint and return hydrated `QuineNode` instances with all properties populated, without requiring a separate per-node fetch.
- [ ] **QC-TR-002** [U]: The system shall provide a raw `query(cypher, params)` escape hatch for complex traversals used internally by the Diagnostic Retrieval Engine; this method shall not be exposed via the MCP server.

---

## Connection and Reliability

- [ ] **QC-CN-001** [P, C]: The system shall retry failed Quine HTTP requests up to 3 times with exponential backoff on 5xx responses and timeouts.
- [ ] **QC-CN-002** [P, C]: The system shall not retry on 4xx responses.
- [ ] **QC-CN-003** [U]: The system shall apply a default per-request timeout of 10 seconds.
- [ ] **QC-CN-004** [U]: The system shall provide a `ping()` method that returns `True` if Quine is reachable and `False` otherwise, without raising.

---

## Multi-Project Isolation

- [ ] **QC-MP-001** [P]: The system shall include `project_slug` in every node ID where specified by the ID scheme, such that nodes from different projects with identical slugs cannot collide in the graph.
