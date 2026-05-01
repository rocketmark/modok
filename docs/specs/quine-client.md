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

- [x] **QC-ID-001** [P, C]: The system shall compute all Quine node IDs deterministically via `idFrom(*parts)`, where parts are joined with a null-byte separator and hashed with SHA-256, taking the first 8 bytes as a signed int64.
- [x] **QC-ID-002** [P]: The system shall use the node type name as the first element of every `idFrom()` tuple, such that two node types with identical remaining parts always produce different IDs.
- [x] **QC-ID-003** [P]: The system shall include `project_slug` as a tuple element in the ID of every node type.
- [x] **QC-ID-004** [U]: The system shall identify `CustomerIssue` nodes by `('customer-issue', project_slug, source_system, ticket_id)`. project_slug is required to prevent cross-project ID collisions.
- [x] **QC-ID-005** [U]: The system shall identify `ResolutionEvent` nodes by `('resolution', project_slug, source_system, ticket_id, fix_id)`, including `source_system` to disambiguate ticket IDs that collide across source systems.
- [x] **QC-ID-006** [P]: The system shall include `project_slug` in the `SimilarityMatch` ID tuple so that two projects whose `CustomerIssue` and `KnownIssue` nodes hash identically cannot share a `SimilarityMatch` node across project boundaries.

---

## Node Writes

- [x] **QC-NW-001** [U, C]: When `upsert_node` is called for a node that does not exist in Quine, the system shall create the node with all properties from the pydantic model.
- [x] **QC-NW-002** [P, C]: When `upsert_node` is called for a node that already exists in Quine, the system shall replace all node properties with the current pydantic model's values, removing any properties not present in the current model.
- [x] **QC-NW-003** [P, C]: When `upsert_node` is called, the system shall not modify any edges on the node.
- [x] **QC-NW-004** [P]: The system shall never infer edge changes from property changes; edge lifecycle is managed exclusively via `write_edge`, regardless of which properties are added, changed, or removed from a node.

---

## Node Reads

- [x] **QC-NR-001** [U, C]: When `get_node` is called for a node ID that exists in Quine, the system shall return the node deserialized into the requested pydantic model type.
- [x] **QC-NR-002** [U, C]: When `get_node` is called for a node ID that does not exist in Quine, the system shall raise `QuineNodeNotFoundError`.
- [x] **QC-NR-003** [U]: The system shall provide `node_exists(node_id)` as the opt-in check for callers that need to test node presence without triggering an exception.

---

## Edge Writes

- [x] **QC-EW-001** [U, C]: When `write_edge` is called for an edge that does not exist, the system shall create the directed edge from `from_id` to `to_id` with the given `edge_type`.
- [x] **QC-EW-002** [P, C]: When `write_edge` is called for an edge that already exists, the system shall treat the call as a no-op without raising an error.
- [x] **QC-EW-003** [U]: write_edge is permitted to reference node IDs that have not yet been upserted. The ingestion pipeline validates all node references before writing edges; shell nodes are not part of the intended ingestion state.
- [x] **QC-EW-004** [U]: The system shall provide a replace_edges(from_id, edge_type, to_ids) operation that deletes all edges of the given type from from_id before recreating the specified set. This is used on re-ingest to eliminate stale edges from removed metadata.

---

## Traversal

- [x] **QC-TR-001** [U, C]: When `traverse` is called, the system shall execute the traversal as a Cypher query against Quine's `/api/v1/query/cypher` endpoint and return hydrated `QuineNode` instances with all properties populated, without requiring a separate per-node fetch.
- [x] **QC-TR-002** [U]: The system shall provide a raw `query(cypher, params)` escape hatch for complex traversals used internally by the Diagnostic Retrieval Engine; this method shall not be exposed via the MCP server. — *Escape hatch implemented and tested; MCP exclusion half is untestable until the MCP server module exists.*
- [x] **QC-TR-003** [U]: If a node returned by `traverse` fails deserialization (e.g., missing a required field), the system shall raise `QuineDeserializationError` identifying the malformed node ID rather than returning a partial result list.

---

## Connection and Reliability

- [x] **QC-CN-001** [P, C]: The system shall retry failed Quine HTTP requests up to 3 times with exponential backoff on 5xx responses and timeouts.
- [x] **QC-CN-002** [P, C]: The system shall not retry on 4xx responses.
- [x] **QC-CN-003** [U]: The system shall apply a default timeout of 10 seconds per attempt, such that each retry attempt receives a fresh 10-second window independent of prior attempts.
- [x] **QC-CN-004** [U]: The system shall provide a `ping()` method that returns `True` if Quine is reachable and `False` otherwise, without raising.

---

## Multi-Project Isolation

- [x] **QC-MP-001** [P]: The system shall include `project_slug` in every node ID where specified by the ID scheme, such that nodes from different projects with identical slugs cannot collide in the graph.
