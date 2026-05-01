
# M.O.D.O.K

<img align="right" width="320" src="docs/assets/modok.png" alt="MODOK"/>

**Mechanized Oracle Designed Only for Knowledge**

MODOK shows engineers and agents what changed, what’s affected, where to look, and what worked before.

Support and debugging issues rarely arrive with all the context needed to investigate them. The evidence is scattered across code, docs, tests, tickets, incidents, logs, change history, and prior fixes. MODOK turns that scattered context into a focused **debug packet**: a structured, traceable map from the issue to the operational evidence that matters.

Unlike generic Graph RAG systems that improve retrieval over documents, MODOK is shaped around support and troubleshooting workflows. It maps an issue to connected systems, artifacts, and past resolutions so an engineer, operator, or agent can start from context instead of reconstructing it from scratch.

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

## Why I built this

At AWS, my team built an internal tool called Hyperion to help on-call engineers get oriented during high-severity incidents. When a Sev1 or Sev2 came in, Hyperion gathered the context that usually lived in a dozen different places: recent commits, ongoing large-scale events, related tickets, impacted customers, and other signals about the state of the system. It helped us move faster because we were no longer starting from a blank page.

MODOK comes from the same belief: debugging is easier when the system can explain where to look first. Instead of rebuilding context from scratch for every issue, MODOK keeps a persistent memory of how a system is structured and how it has failed before, linking features, code, tests, known issues, change history, and prior fixes into a map that engineers and agents can follow.

I’m building it because I expect to operate complex systems largely on my own, and I want the kind of mechanical support that makes debugging faster, more consistent, and less dependent on memory.

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
