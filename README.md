
# M.O.D.O.K

<img align="right" width="320" src="docs/assets/modok.png" alt="MODOK"/>

**Mechanized Oracle Designed Only for Knowledge**

MODOK gives AI agents a *running start* when debugging software issues.

Instead of rediscovering context from scratch, MODOK returns a focused **debug packet**: 
- the exact docs
- code
- tests
- known issues
- and prior fixes that matter for a given problem.

MODOK’s differentiator is not simply that it uses a graph; it’s that the graph is shaped around support and troubleshooting. Unlike generic Graph RAG systems that use graphs to improve document retrieval or answer quality, MODOK is issue-centered: it maps a problem to the operational evidence needed to investigate it, including affected systems, relevant code, docs, tickets, incidents, change history, logs, and prior fixes. The result is a structured, traceable debug packet that an engineer, operator, or agent can follow. Not a loose set of related documents that must be interpreted from scratch.

<br clear="right"/>

---

## The problem

Diagnosing a software issue is mostly **orientation**:

- What feature is this in?
- Where is the code?
- What tests cover it?
- Has this happened before?
- What fixed it last time?

Humans rebuild this context manually. Agents rebuild it every session. It’s slow, repetitive, expensive, and lossy.

MODOK shows what changed, what’s affected, where to look, and what worked before.

## Why I built this

At AWS, one of my teams built a tool called Hyperion that helped our oncall engineers quickly understand high-severity incidents by automatically pulling together relevant context like recent commits, ongoing large-scale events, related tickets, and impacted customers. It let us answer “what is the state of the system?” within minutes and start debugging with the right information already in hand. 

MODOK is a continuation of that idea, but focused on building a persistent memory of how a system is structured and how it fails — linking features, code, tests, known issues, and prior fixes — so that I can move from a new issue to the right place without redoing the same orientation work every time. 

I’m building this because I expect to operate a system largely on my own, and I need that kind of mechanical support to debug effectively and consistently. 

This implementation is inspired by and directly builds on [Jess Szmajda’s LID project](https://github.com/jszmajda/lid).

---

## What MODOK does

MODOK turns a customer issue into a **debug starting point**.

```
Customer issue
   ↓
MODOK extracts anchors (feature, errors, symptoms)
   ↓
Graph lookup finds related:
  - docs
  - code areas
  - tests
  - known issues
  - prior fixes
   ↓
Returns a debug packet
```

### Example (simplified)

```json
{
  "feature": "shtp-receiver",
  "errors": ["shtp-version-mismatch"],

  "relevant_files": [
    "agent/src/shtp.c",
    "client/shtp_receiver.py"
  ],

  "relevant_tests": [
    "agent/tests/test_shtp.c"
  ],

  "known_issues": [
    "Client misreads version field offset"
  ],

  "recent_fixes": [
    "fix-shtp-version-offset (commit a3f9c12)"
  ]
}
```

The agent starts here — not from zero.

---

## Core idea

MODOK is a **persistent memory of how your system is structured and how it fails**.

It stores relationships like:

```
feature → module → file → test → known issue → fix
```

So instead of searching blindly, agents navigate a **map of the system**.

---

## Design principles

```
Memory is for orientation.
Files are for truth.
Tests are for verification.

Explicit metadata is truth.
LLM output is a proposal.
Only validated structure is stored.
```

---

## How it works

### 1. Ingestion (mechanical, trusted)

Docs, registries, tickets, and resolutions are parsed into structured metadata:

- features, modules, files
- error signatures
- known issues and fixes

No LLM writes to the system of record.

---

### 2. Graph (persistent memory)

Relationships are stored in a graph:

- deterministic IDs
- typed nodes and edges
- multi-project isolation

MODOK uses **Quine** as the underlying graph store.

---

### 3. Retrieval

Given an issue:

- extract anchors (feature, errors, symptoms)
- traverse the graph
- rank relevant nodes
- assemble a debug packet

---

### 4. LLM (optional, bounded)

LLMs are used only for:

- parsing freeform tickets
- suggesting missing metadata

They **never write directly** to the graph.

---

## What MODOK is (and isn’t)

### ✔️ MODOK is

- A **debug context engine**
- A **relationship memory for your system**
- A **tool agents call before debugging**

### ❌ MODOK is not

- A code search engine
- A log storage system
- A replacement for reading code or running tests
- An autonomous debugger

---

## Current scope (v1)

- Static ingestion (docs, registries, tickets, resolutions)
- Deterministic graph of system relationships
- Debug packet generation from graph traversal

---

## Future (not in v1)

- Live event ingestion (logs, deployments)
- Streaming / standing queries
- Real-time incident enrichment

---

## Implementation

- Python
- pydantic v2
- Quine (graph store)

---

## Status

Early development.

---

## Docs

- [High-Level Design](docs/high-level-design.md)
- [Architecture Brainstorm](docs/modok-setup-brainstorm.md)
