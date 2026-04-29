# MODOK

**Mechanical Oracle Designed Only for Knowledge**

MODOK is a Quine-backed diagnostic memory graph that helps AI agents — Claude, ChatGPT, local LLMs, VS Code agents, Visual Studio agents — quickly move from a customer issue to the most relevant docs, code areas, tests, known issues, prior fixes, and operational signals.

## The problem

Diagnosing a software issue requires orienting across many artifacts before any useful inspection can begin. Agents repeat this traversal from scratch every session, with no memory of what was relevant last time.

## What MODOK does

Given a customer issue (structured or freeform), MODOK returns a focused **debug packet**:

```
Customer ticket
   ↓
MODOK extracts symptoms, errors, product area, and context
   ↓
Quine graph finds related docs, code, tests, known issues, and prior fixes
   ↓
Agent inspects the current repo with a running start
   ↓
Focused diagnosis and starting point
```

## Design principles

```
Memory is for orientation.
Files are for truth.
Tests are for verification.

Explicit metadata is truth.
LLM output is a proposal.
Quine stores validated structure.
```

## Architecture

- **Quine** — persistent graph store for typed, source-backed relationships (feature → module → file → test → known issue → fix)
- **Static ingestion** — mechanical pipeline that ingests design docs, code maps, tickets, and resolution records without LLM involvement in the write path
- **LLM Gateway** — pluggable, local-first (Ollama); Claude or GPT-4 as optional escalation targets. No LLM SDK is a hard dependency.
- **Diagnostic Retrieval Engine** — builds ranked debug packets from graph traversal, optionally boosted by vector search
- **MCP server + CLI** — agents call MODOK via MCP tools; developers use the CLI directly

## Modes

| Mode | Data sources |
|---|---|
| **Static** | Design docs, testing docs, code maps, tickets, known issues |
| **Stream** *(future)* | AWS logs, deployments, config changes, feature-flag events, live issue patterns |

## Implementation

Python · pydantic v2 · ruff · mypy · Quine (graph) · optional vector index

Multi-project from day one — a single MODOK instance serves multiple projects, each in its own namespace.

## Status

Early development. First target project: [stagehand](https://github.com/marks/stagehand).

## Docs

- [High-Level Design](docs/high-level-design.md)
- [Architecture Brainstorm](docs/modok-setup-brainstorm.md)
