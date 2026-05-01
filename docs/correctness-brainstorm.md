# MODOK Correctness Plan

This document outlines the areas where formal modeling (Lean, TLA+) can provide strong correctness guarantees for MODOK, along with the invariants that must hold true.

---

## Core Philosophy

MODOK does **not** guarantee that retrieved context is sufficient to fix a bug.

MODOK **does** guarantee:

> Every returned piece of context is **typed, bounded, explainable, and traceable to source evidence**.

---

## System Boundary: Quine vs MODOK

MODOK is built on top of :contentReference[oaicite:0]{index=0}, which provides its own correctness guarantees.

### Quine guarantees

- Graph operations execute correctly
- Data is persisted according to configuration
- Standing queries behave according to Quine semantics
- Internal graph consistency is maintained

### MODOK guarantees

- Node identities are stable
- Only valid domain relationships are written
- Edge types conform to the MODOK ontology
- Returned context follows approved traversal semantics
- Every returned item has provenance

> MODOK is responsible for **domain correctness**, not graph engine correctness.

---

## 1. Ingestion Idempotency

### Guarantee

> Re-ingesting the same source artifact produces the same graph shape and does not create duplicate semantic entities.

### Assumptions

Stable identifiers exist for core entities:

- Ticket(id)
- Commit(sha)
- File(path, repo)
- Doc(id/path/url)
- Test(name/path)
- Fix(id/sha)

### Why it matters

- Prevents graph bloat
- Ensures consistent retrieval results
- Enables safe retries in ingestion pipelines

### Suggested modeling

- **TLA+** if ingestion is concurrent or retry-based
- Lean optional for identity constraints

---

## 2. MODOK Ontology & Domain Edge Validity

### Guarantee

> Every MODOK-authored node and edge conforms to the MODOK ontology, even if the underlying graph system would allow invalid relationships.

### Example valid relationships

- Ticket -> mentions -> File
- Fix -> changes -> File
- Ticket -> resolved_by -> Fix
- Test -> covers -> File
- Doc -> explains -> Component

### Example invalid (but technically possible) relationships

- Ticket -> covers -> Commit
- Doc -> resolved_by -> Test
- File -> explains -> Customer

### Why it matters

- Prevents semantic corruption of the graph
- Ensures predictable traversal behavior
- Preserves meaning of relationships across the system

### Suggested modeling

- **Lean (strong fit)** for type-level guarantees and invariants

---

## 3. Read-Path Soundness (Explainability)

### Guarantee

> A debug packet only contains evidence reachable from the query seed through approved relationship paths.

### Example path
