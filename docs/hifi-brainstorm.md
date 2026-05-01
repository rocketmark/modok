# HiFI for MODOK

HiFI is a differential test suite for MODOK.

The goal is to compare:

1. A simple monolithic reference model of MODOK behavior  
2. The real MODOK implementation  
3. Using the same inputs  
4. With Quine replaced by a deterministic dummy graph  

HiFI does not test Quine correctness. It tests whether MODOK correctly parses, normalizes, writes, reads, ranks, and assembles debug context across its own boundaries.

---

## Core idea

For every test scenario, HiFI sends the same traffic to:

```text
                ┌────────────────────┐
Input events ──▶│ Reference Model     │
                │ in-memory MODOK     │
                └─────────┬──────────┘
                          │
                          ▼
                 Expected debug packet


                ┌────────────────────┐
Input events ──▶│ Real MODOK          │
                │ real ingest/read    │
                └─────────┬──────────┘
                          │
                          ▼
                Dummy Quine / Fake Graph
                          │
                          ▼
                  Actual debug packet
```

Then HiFI compares the expected result against the actual result.

---

## What HiFI should test

HiFI should cover MODOK’s full paths in and out:

```text
Write path:

raw input
  → parser
  → canonical MODOK entities
  → edge construction
  → graph write calls
  → stored dummy graph state

Read path:

query/problem
  → diagnostic retrieval engine
  → graph queries
  → candidate collection
  → ranking/filtering
  → debug packet assembly
```

The dummy graph should be dumb but deterministic. It should accept writes, store nodes and edges in memory, and answer the subset of graph queries MODOK expects.

---

## What HiFI should not test

HiFI should not test:

- Quine persistence  
- Quine standing query behavior  
- Quine Cypher correctness  
- RocksDB behavior  
- distributed graph behavior  
- graph engine performance  
- vector search quality  

Those belong in separate integration or compatibility tests.

HiFI is for testing MODOK’s semantics, not Quine’s implementation.

---

## Components

### 1. Reference model

The reference model is a small, boring, in-memory implementation of MODOK’s expected behavior.

It should not share implementation code with real MODOK except for stable public schemas.

It should model things like:

- how raw tickets become entities  
- how docs, code, tests, tickets, services, and incidents relate  
- which edges should exist  
- what a query should retrieve  
- what a debug packet should contain  
- what ordering or ranking rules should apply  

Example:

```python
class ReferenceModok:
    def ingest_ticket(self, ticket):
        # Parse ticket into canonical entities
        # Store expected nodes and edges in simple dicts/lists
        pass

    def query(self, problem):
        # Return the expected debug packet
        # using simple deterministic rules
        pass
```

The reference model should be deliberately less clever than real MODOK. It should encode the contract, not the implementation.

---

### 2. Dummy Quine

Dummy Quine is a fake graph backend that implements the graph-facing interface MODOK already uses.

It should support:

- node upsert  
- edge upsert  
- lookup by stable ID  
- lookup by type  
- neighborhood expansion  
- reverse edge lookup  
- simple query templates used by the DRE  

Example:

```python
class DummyQuine:
    def __init__(self):
        self.nodes = {}
        self.edges = []

    def upsert_node(self, node_id, node_type, properties):
        self.nodes[node_id] = {
            "id": node_id,
            "type": node_type,
            "properties": properties,
        }

    def upsert_edge(self, from_id, edge_type, to_id, properties=None):
        self.edges.append({
            "from": from_id,
            "type": edge_type,
            "to": to_id,
            "properties": properties or {},
        })

    def neighbors(self, node_id, edge_types=None, direction="out"):
        # Return deterministic neighboring nodes
        pass
```

The key rule: **Dummy Quine should not be smart.**

It should not infer edges, rewrite queries, rank results, or hide MODOK bugs.

---

### 3. Graph port / adapter

MODOK should talk to Quine through a narrow interface.

HiFI swaps the real Quine adapter with Dummy Quine.

```text
MODOK Core
   │
   ▼
GraphStore interface
   ├── RealQuineGraphStore
   └── DummyQuineGraphStore
```

Suggested interface:

```python
class GraphStore:
    def upsert_node(self, node): ...
    def upsert_edge(self, edge): ...
    def get_node(self, node_id): ...
    def find_nodes(self, node_type=None, properties=None): ...
    def expand(self, node_id, edge_types=None, depth=1): ...
    def run_query(self, query_name, params): ...
```

The real implementation can translate these into Quine calls.

The dummy implementation stores everything in memory.

---

## Test shape

Each HiFI test should look like this:

```python
def test_ticket_links_to_docs_and_prior_fix():
    scenario = Scenario(
        inputs=[
            customer_ticket(...),
            doc(...),
            prior_incident(...),
            code_owner(...),
        ],
        query=debug_query("customer timeout in checkout service"),
    )

    expected = run_reference_model(scenario)
    actual = run_real_modok_with_dummy_quine(scenario)

    assert_debug_packets_equivalent(expected, actual)
```

---

## Scenario format

HiFI scenarios should be data-driven.

Example:

```yaml
name: checkout_timeout_prior_fix

inputs:
  - type: ticket
    id: TICKET-123
    title: Checkout timeout for customer A
    body: Payment requests timeout after deploy 8f3c9a.
    service: checkout
    customer: customer-a

  - type: commit
    id: 8f3c9a
    repo: checkout-service
    files:
      - payment/client.py

  - type: doc
    id: DOC-9
    title: Checkout payment timeout runbook
    service: checkout

  - type: incident
    id: INC-44
    title: Prior checkout payment timeout
    service: checkout
    fix_commit: 91aa2

query:
  text: checkout payment timeout
  service: checkout

expected:
  debug_packet:
    must_include:
      docs:
        - DOC-9
      incidents:
        - INC-44
      commits:
        - 8f3c9a
    must_not_include:
      docs:
        - unrelated-doc
```

---

## Comparison rules

Do not compare raw JSON byte-for-byte unless the output is fully deterministic.

Instead compare semantic properties:

Required:
- expected entities are present  
- expected relationships are present  
- forbidden entities are absent  
- packet sections are correct  
- stable IDs are preserved  
- source attribution is present  
- ranking is within acceptable bounds  

Optional:
- exact ordering  
- exact wording  
- exact score values  

Example:

```python
assert_contains_entity(actual, type="doc", id="DOC-9")
assert_contains_entity(actual, type="incident", id="INC-44")
assert_not_contains_entity(actual, id="unrelated-doc")
assert_rank_before(actual, "DOC-9", "unrelated-doc")
```

---

## Invariants HiFI should check

### Ingestion invariants

- Every ingested object gets a stable canonical ID.  
- Re-ingesting the same object is idempotent.  
- Duplicate inputs do not create duplicate graph facts.  
- Known entity types map to the expected MODOK node types.  
- Known relationships map to the expected edge types.  
- Missing optional fields do not crash ingestion.  
- Invalid required fields fail cleanly.  
- Source provenance is preserved.  

---

### Graph-boundary invariants

- MODOK only writes valid node types.  
- MODOK only writes valid edge types.  
- Edges only reference existing or intentionally stubbed nodes.  
- All graph writes are deterministic for the same input.  
- Query templates receive the expected parameters.  
- Read path does not depend on Quine-only side effects.  

---

### Retrieval invariants

- Querying by service retrieves service-local context.  
- Querying by ticket retrieves linked docs, code, tests, and prior incidents.  
- Querying by symptom retrieves known related incidents.  
- Querying by commit retrieves related files, services, tickets, and tests.  
- Unrelated context is filtered out.  
- Ambiguous queries return multiple candidates rather than inventing certainty.  

---

### Debug packet invariants

- Debug packets include stable entity IDs.  
- Debug packets include source references.  
- Debug packets separate docs, code, tests, tickets, incidents, and fixes.  
- Debug packets explain why each item was included.  
- Debug packets do not include orphaned or provenance-free facts.  
- Debug packets are deterministic for deterministic inputs.  

---

## Recommended HiFI test layers

### Layer 1: Golden scenarios

Hand-authored fixtures for important cases.

Use these for:

- customer ticket ingestion  
- runbook retrieval  
- prior incident retrieval  
- code/test linkage  
- service ownership  
- ambiguous symptoms  
- duplicate ingestion  
- stale or superseded docs  

These should be readable and checked into the repo.

---

### Layer 2: Property tests

Generate many small MODOK worlds.

Example generated world:

```text
N services
M tickets
K docs
P commits
Q tests
R incidents
random but valid links between them
```

Then assert invariants:

- reingestion is idempotent  
- all edges are valid  
- every returned entity is reachable by an allowed path  
- unrelated services do not leak into the packet  
- provenance is never lost  

---

### Layer 3: Metamorphic tests

Apply transformations that should or should not change the result.

Examples:

Should not change result:
- input order changes  
- duplicate event appears  
- irrelevant unrelated ticket is added  
- whitespace changes in ticket body  

Should change result:
- service name changes  
- new prior fix is added  
- doc is marked deprecated  
- ticket gets linked to a different commit  

---

### Layer 4: Contract tests for real Quine adapter

Separate from HiFI.

These tests verify that `RealQuineGraphStore` and `DummyQuineGraphStore` agree on the graph interface.

This is where you check:

- upsert behavior  
- query template parameters  
- response shape  
- edge direction  
- missing-node behavior  

But this is not the main HiFI suite.

---

## Proposed repo layout

```text
tests/
  hifi/
    README.md

    scenarios/
      checkout_timeout.yaml
      duplicate_ticket_ingestion.yaml
      stale_runbook_filtered.yaml
      prior_fix_retrieval.yaml
      ambiguous_service_query.yaml

    reference/
      model.py
      graph.py
      retrieval.py
      debug_packet.py

    dummy_quine/
      graph_store.py
      query_templates.py

    harness/
      runner.py
      compare.py
      fixtures.py

    test_golden_scenarios.py
    test_properties.py
    test_metamorphic.py

  contract/
    test_graph_store_contract.py

  integration/
    test_real_quine_smoke.py
```

---

## The important design choice

HiFI should compare:

```text
Reference MODOK behavior
vs.
Real MODOK behavior using Dummy Quine
```

Not:

```text
Real MODOK + Real Quine
vs.
Reference model
```

That keeps failures actionable.

If HiFI fails, the bug is probably in MODOK.

If Quine contract tests fail, the bug is probably in the Quine adapter, query shape, or graph backend assumptions.

---

## Minimal first version

The first useful HiFI can be small:

1. Define GraphStore interface  
2. Implement DummyQuineGraphStore  
3. Implement tiny ReferenceModok  
4. Create 5 golden YAML scenarios  
5. Run each scenario through both systems  
6. Compare debug packets semantically  

Start with these five scenarios:

- ticket → service → runbook  
- ticket → service → prior incident → fix commit  
- commit → files → tests → service  
- duplicate ticket ingestion is idempotent  
- unrelated service context is excluded  

That is enough to prove the harness shape before expanding into property tests.

---

## Summary

HiFI for MODOK should be a differential semantic test suite.

It should use:

- a monolithic in-memory reference model  
- the real MODOK ingest and read paths  
- a deterministic Dummy Quine  
- scenario-driven tests  
- semantic debug packet comparison  

Its job is to answer:

> Given the same world and the same debug query, does real MODOK produce the same meaningful debug packet as the reference model?

Not:

> Does Quine work?

That separation is what makes HiFI useful.
