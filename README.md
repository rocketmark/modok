# M.O.D.O.K

<img align="right" width="320" src="docs/assets/MODOK-Project-Image.png" alt="MODOK"/>

**Mechanized Oracle Designed Only for Knowledge**

MODOK gives engineers, operators, and AI agents the context to act, not just the text to read.

It shows:

- what changed
- what is affected
- where to look
- what tests matter
- what happened before
- what fixed similar issues last time

Support and debugging issues rarely arrive with all the context needed to investigate them. The evidence is scattered across code, docs, tests, tickets, incidents, logs, change history, and prior fixes. MODOK turns that scattered context into a focused **debug packet**: a structured, traceable starting point for understanding an issue.

MODOK is not trying to replace source control, observability, tests, or documentation. Those remain the systems of record. MODOK stores the relationships between them so an engineer or agent can follow the trail instead of reconstructing it from scratch every time.

<br clear="right"/>

---

## The problem

Debugging is mostly **orientation**.

Before anyone can fix a problem, they need to answer questions like:

- What feature or subsystem is this in?
- What code is involved?
- What tests cover this behavior?
- Has this happened before?
- What changed recently?
- What fixed similar issues last time?
- Which docs, tickets, incidents, or logs are relevant?

Humans rebuild this context manually. Agents rebuild it every session. It's slow, repetitive, expensive, and lossy.

MODOK exists to make that orientation step explicit, reusable, and inspectable.

---

## What MODOK does

MODOK turns a support or debugging issue into a **debug starting point**.

```text
Issue
   ↓
Extract anchors
  - feature names
  - errors
  - symptoms
  - affected systems
  - customer or environment clues
   ↓
Traverse the relationship graph
   ↓
Find connected:
  - docs
  - code areas
  - tests
  - tickets
  - incidents
  - change history
  - known issues
  - prior fixes
   ↓
Return a debug packet
```

The output is not “the answer." It is the context needed to act.

---

## Example debug packet

Given an issue like:

```text
Customer reports shtp receiver fails after upgrade with version mismatch error.
```

MODOK might return:

```json
{
  "issue": {
    "summary": "shtp receiver fails after upgrade",
    "anchors": {
      "features": ["shtp-receiver"],
      "errors": ["shtp-version-mismatch"],
      "symptoms": ["fails after upgrade"]
    }
  },

  "affected_areas": [
    {
      "type": "feature",
      "id": "feature:shtp-receiver",
      "name": "shtp-receiver"
    }
  ],

  "relevant_files": [
    "agent/src/shtp.c",
    "client/shtp_receiver.py"
  ],

  "relevant_tests": [
    "agent/tests/test_shtp.c"
  ],

  "known_issues": [
    {
      "id": "known-issue:shtp-version-offset",
      "summary": "Client misreads version field offset"
    }
  ],

  "prior_fixes": [
    {
      "id": "fix:shtp-version-offset",
      "commit": "a3f9c12",
      "summary": "Fix SHTP version offset parsing"
    }
  ],

  "next_steps": [
    "Inspect SHTP receiver parsing code",
    "Review prior version offset fix",
    "Run SHTP receiver tests",
    "Check recent changes touching SHTP protocol handling"
  ]
}
```

The agent starts here — not from zero.

---

## Why not just use RAG?

Typical vector-based RAG is useful for finding text that looks semantically related to a question.

That is not the same thing as understanding how a system is connected.

MODOK’s differentiator is not simply that it uses a graph. It is that the graph is shaped around support and troubleshooting workflows. Powered by Quine’s streaming ingestion and standing queries, MODOK can continuously watch operational relationships as they change and turn them into proactive, enriched tickets before a user even opens one.

MODOK maps an issue to the relationships that matter:

```text
feature → module → file → test → known issue → fix

---

## Is this just documentation?

No.

Documentation explains things to humans. MODOK stores inspectable relationships between operational artifacts.

For example, a document might say:

```text
The SHTP receiver validates protocol versions during connection setup.
```

MODOK stores relationships like:

```text
feature:shtp-receiver
  uses file:agent/src/shtp.c
  covered_by test:agent/tests/test_shtp.c
  has_known_issue known-issue:shtp-version-offset
  fixed_by commit:a3f9c12
```

That structure lets an agent ask:

- What files implement this feature?
- What tests cover this behavior?
- What known issues are connected to this error?
- What fixes resolved this class of problem before?

The point is not to replace docs. It's to make the relationships the docs imply explicit enough to retrieve.

---

## How MODOK avoids becoming a stale second source of truth

This is the main design constraint.

MODOK should not become a parallel universe that competes with source control, logs, docs, tickets, or tests.

Instead:

```text
Source control is truth for code.
Tests are truth for behavior.
Logs and observability are truth for runtime state.
Tickets and incidents are truth for operational history.
Docs are truth for human-facing explanation.

MODOK is memory for relationships.
```

MODOK stores links, identifiers, and metadata that help connect those systems.

It should be possible to trace every useful fact in a debug packet back to a real artifact: a file, test, ticket, incident, commit, doc, or log source.

---

## Accuracy model

MODOK is intentionally conservative about what gets stored.

```text
Explicit metadata is truth.
LLM output is a proposal.
Only validated structure is stored.
```

LLMs may help parse messy tickets, suggest missing links, or extract candidate anchors.

They do not write directly to the graph.

The graph should be populated from deterministic ingestion, validated metadata, or human-reviewed structure. If MODOK is wrong, the relationship should be inspectable and fixable.

---

## Core idea

MODOK is a **persistent memory of how your system is structured and how it fails**.

It remembers relationships like:

```text
feature → module → file → test
feature → known issue → prior fix
ticket → symptom → affected system
incident → change → mitigation
error → component → runbook
```

So instead of searching blindly, engineers and agents navigate a map of the system.

---

## Design principles

```text
Memory is for orientation.
Files are for truth.
Tests are for verification.

The graph should point to evidence.
The graph should not replace evidence.

LLM output is a proposal.
Validated structure is memory.

A debug packet is a starting point.
It is not a conclusion.
```

---

## How it works

### 1. Ingestion

MODOK ingests structured metadata from sources like:

- docs
- registries
- tickets
- incidents
- resolutions
- code ownership metadata
- test metadata
- change history

The ingestion path turns these inputs into typed nodes and edges.

Examples:

```text
Feature
Module
File
Test
KnownIssue
Fix
Ticket
Incident
Change
```

and:

```text
IMPLEMENTS
COVERED_BY
MENTIONS_ERROR
AFFECTS
FIXED_BY
RELATED_TO
CHANGED_BY
```

---

### 2. Graph

Relationships are stored in a graph with:

- deterministic IDs
- typed nodes
- typed edges
- project isolation
- traceable links back to source artifacts

MODOK uses **Quine** as the underlying graph store.

---

### 3. Retrieval

Given an issue, MODOK:

1. extracts anchors from the issue
2. finds matching graph nodes
3. traverses relevant relationships
4. ranks connected artifacts
5. assembles a debug packet

The result is structured context that can be used by:

- humans
- agents
- CLIs
- MCP tools
- support workflows
- incident workflows

---

### 4. LLM usage

LLMs are optional and bounded.

They may be used for:

- parsing freeform tickets
- extracting possible anchors
- suggesting missing metadata
- summarizing a debug packet

They are not used as the system of record.

They do not directly mutate the graph.

---

## What MODOK is — and is not

### MODOK is

- A **support and troubleshooting context engine**
- A **persistent map of system relationships**
- A **debug starting point** for engineers, operators, and agents
- A way to connect issues to the code, docs, tests, tickets, incidents, changes, and prior fixes that matter
- A retrieval layer for operational context

### MODOK is not

- A code search engine
- A log store or observability platform
- A replacement for source control, logs, docs, tickets, or tests
- An autonomous debugger
- A source of truth for live system state
- A system that magically understands your codebase without metadata

---

## What makes this useful for agents?

Agents are good at using context, but bad at knowing which context matters.

Without memory, an agent starts every debugging session by rediscovering the system:

```text
Search files.
Read docs.
Guess ownership.
Look for tests.
Search old tickets.
Infer what changed.
Maybe find the prior fix.
Maybe miss it.
```

MODOK gives the agent a structured starting point:

```text
This issue mentions this feature.
This feature maps to these files.
These tests cover it.
These known issues are nearby.
These fixes resolved similar symptoms before.
These changes may be relevant.
```

That does not make the agent correct by default.

It makes the agent less blind.

---

## Current scope

MODOK currently focuses on static, inspectable support/debugging context:

- structured ingestion
- typed graph relationships
- deterministic IDs
- debug packet generation
- bounded LLM-assisted parsing
- Quine-backed graph storage

---

## Not in scope yet

These are intentionally not part of the initial version:

- live log ingestion
- real-time observability storage
- autonomous remediation
- automatic code changes
- production incident orchestration
- replacing existing monitoring or ticketing systems

---

## Future direction

Potential future work:

- live event ingestion
- deployment/change correlation
- standing queries
- streaming incident enrichment
- MCP integration
- richer agent workflows
- confidence scoring for relationships
- stale relationship detection
- source artifact validation

---

## Implementation

- Python
- pydantic v2
- Quine

---

## Project status

Early development.

The goal of the current version is to prove the core idea:

> A support or debugging issue can be mapped to a traceable packet of relevant system context using explicit relationships instead of loose document retrieval.

APIs, schemas, and commands may change.

---

## Docs

- [High-Level Design](docs/high-level-design.md)
- [Architecture Brainstorm](docs/modok-setup-brainstorm.md)

---

## Inspiration

At AWS, my team built an internal tool called Hyperion to help our on-call engineers get oriented during high-severity incidents. When a Sev1 or Sev2 came in, Hyperion gathered the context that usually lived in a dozen different places: recent commits, ongoing large-scale events, related tickets, impacted customers, and other signals about the state of the system.

It helped the on-call respond faster because they were no longer starting from a blank page.

MODOK comes from the same belief: debugging is easier when the system can explain where to look first.

This implementation is inspired by and directly builds on [Jess Szmajda’s LID project](https://github.com/jszmajda/lid).
