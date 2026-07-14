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

- [x] **QC-ID-001** [C]: The system shall address all Quine nodes using Quine's built-in `idFrom()` Cypher function, embedded directly in Cypher query strings. MODOK shall never compute or store Quine node IDs in Python.
- [x] **QC-ID-002** [C]: The system shall use the node type name as the first string argument to `idFrom()` in every Cypher pattern, such that two node types with identical remaining arguments always produce different UUIDs.
- [x] **QC-ID-003** [C]: The system shall include `project_slug` as an `idFrom()` argument for every node type, such that nodes from different projects cannot share a Quine address.
- [x] **QC-ID-004** [U]: The system shall address `CustomerIssue` nodes with `idFrom('customer-issue', $project_slug, $source_system, $ticket_id)` in Cypher. `project_slug` is required to prevent cross-project ID collisions.
- [x] **QC-ID-005** [U]: The system shall address `ResolutionEvent` nodes with `idFrom('resolution', $project_slug, $source_system, $ticket_id, $fix_id)` in Cypher, including `source_system` to disambiguate ticket IDs that collide across source systems.
- [x] **QC-ID-006** [C]: The system shall include `project_slug` as an `idFrom()` argument for `SimilarityMatch` so that two projects whose `CustomerIssue` and `KnownIssue` nodes have identical remaining parts cannot share a `SimilarityMatch` node across project boundaries.

---

## Node Writes

- [x] **QC-NW-001** [U, C]: When `upsert_node` is called for a node that does not exist in Quine, the system shall create the node with all properties from the pydantic model, using a `MATCH (n) WHERE id(n) = idFrom(...) SET n += {...}` Cypher pattern.
- [x] **QC-NW-002** [P, C]: When `upsert_node` is called for a node that already exists in Quine, the system shall set all properties present in the current pydantic model to their current values using `SET n += {...}`. Properties from prior writes that are no longer in the model are not removed (ghost properties); this is accepted in v1.
- [x] **QC-NW-003** [P, C]: When `upsert_node` is called, the system shall not modify any edges on the node.
- [x] **QC-NW-004** [P]: The system shall never infer edge changes from property changes; edge lifecycle is managed exclusively via `write_edge`, regardless of which properties are added, changed, or removed from a node.

---

## Node Reads

- [x] **QC-NR-001** [U, C]: When `get_node` is called for a node ID that exists in Quine, the system shall return the node deserialized into the requested pydantic model type.
- [x] **QC-NR-002** [U, C]: When `get_node` is called for a node ID that does not exist in Quine, the system shall raise `QuineNodeNotFoundError`.
- [x] **QC-NR-003** [U]: The system shall provide `node_exists(node_id)` as the opt-in check for callers that need to test node presence without triggering an exception.
- [x] **QC-NR-004** [U, C]: The system shall provide `node_exists_by_parts(parts)`, which embeds Quine's built-in `idFrom()` Cypher function directly in the query text (the same pattern `write_edge_by_parts` already uses) rather than requiring the caller to have already resolved a real Quine node ID. Callers that only know a node's logical `idFrom()` parts — not a UUID obtained from a prior query result — must use this instead of `node_exists`, since `modok.quine.ids.idFrom()` (a Python-side SHA-256 int64) is not a valid Quine node ID and a `node_exists` call given one will always return `False` regardless of whether the node exists (see § ID Scheme; this was found live — `docs/llds/standing-queries.md § Live Verification Findings` — as the root cause of mechanical anchor linking never actually matching against real Quine data).

---

## Edge Writes

- [x] **QC-EW-001** [U, C]: When `write_edge` is called for an edge that does not exist, the system shall create the directed edge from `from_id` to `to_id` with the given `edge_type`.
- [x] **QC-EW-002** [P, C]: When `write_edge` is called for an edge that already exists, the system shall treat the call as a no-op without raising an error.
- [x] **QC-EW-003** [U]: write_edge is permitted to reference node IDs that have not yet been upserted. The ingestion pipeline validates all node references before writing edges; shell nodes are not part of the intended ingestion state.
- [x] **QC-EW-004** [U]: The system shall provide a replace_edges(from_id, edge_type, to_ids) operation that deletes all edges of the given type from from_id before recreating the specified set. This is used on re-ingest to eliminate stale edges from removed metadata.
- [x] **QC-EW-005** [U, C]: The system shall provide `write_edge_by_parts(from_parts, edge_type, to_parts, properties=None)`, which embeds Quine's built-in `idFrom()` Cypher function for both endpoints directly in the query text, for callers that know only the logical `idFrom()` parts of both nodes and have no prior query result carrying a real Quine ID for either one.
- [x] **QC-EW-006** [U, C]: The system shall provide `replace_edges_by_parts(from_parts, edge_type, to_parts_list)`, the by-parts equivalent of `replace_edges` (QC-EW-004): it deletes all edges of the given type from the node addressed by `from_parts`, then writes one edge (via `write_edge_by_parts`) to each address in `to_parts_list`. Used by callers — mechanical anchor linking chief among them — that only ever have logical parts for both the source and target nodes, never a previously-resolved Quine ID.

---

## Traversal

- [x] **QC-TR-001** [U, C]: When `traverse` is called, the system shall execute the traversal as a Cypher query against Quine's `POST /api/v1/query/cypher` endpoint with body `{"text": "<cypher>", "parameters": {...}}` and return hydrated `QuineNode` instances with all properties populated, without requiring a separate per-node fetch.
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
