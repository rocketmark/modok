# High-Level Design: MODOK

*Mechanical Oracle Designed Only for Knowledge*

## Problem

Diagnosing a customer issue in a software project requires orienting across many artifacts — design docs, code, tests, known issues, prior fixes, deployment events — before any useful inspection can begin. This orientation work is slow, lossy, and not retained between incidents. Agents (Claude, ChatGPT, local models, VS Code agents) repeat the same traversal from scratch every session, with no memory of what was relevant last time.

MODOK solves this by maintaining a persistent, graph-structured diagnostic memory for a project. Given a customer issue, MODOK returns a focused debug packet: the relevant docs, code areas, tests, known issues, prior fixes, and operational signals — so the agent skips orientation and starts inspecting.

## Approach

MODOK is a **Quine-backed diagnostic memory graph** with a mechanical ingestion pipeline and an LLM-agnostic query interface.

Three disciplines combine:

- **Graph (Quine):** stores typed, source-backed relationships — feature → module → file → test → known issue → fix. Deterministic IDs. No inferred facts stored as truth.
- **Vector index (optional):** fuzzy recall for vague natural-language tickets. Candidates from vector search are always validated or expanded through Quine before being treated as matches.
- **LLM (pluggable):** used only for proposals — parsing unstructured ticket text, suggesting missing metadata, proposing similarity. LLM output is never written to Quine without validation.

The guiding invariant:

```
Explicit metadata is truth.
LLM output is a proposal.
Quine stores validated structure.
Files are the source of truth.
Tests verify the diagnosis.
```

## Target Users

**Primary: software agents** — Claude, ChatGPT, local LLMs (Ollama, llama.cpp), VS Code agents, Visual Studio agents. MODOK is a tool they call before debugging, not an interface humans navigate directly.

**Secondary: developers** — use the CLI to ingest docs, validate registries, record resolutions, and inspect what MODOK knows about a feature.

**Tertiary: on-call engineers** — in a future stream mode, MODOK enriches live incidents with graph context before a human investigates.

## Goals

- Given any customer issue (structured or freeform), return a debug packet that points the agent to the right docs, code, tests, known issues, and prior fixes in under 5 seconds.
- Support multiple projects in a single MODOK instance from day one. Each project is a named, isolated namespace.
- Mechanical ingestion: design docs, testing docs, known issues, code maps, and resolved tickets become trusted graph structure without LLM involvement in the write path.
- LLM-agnostic: any model (local or remote) can drive MODOK. Claude and GPT-4 are optional escalation targets, not hard dependencies.
- Stagehand is the first target project. MODOK must be useful for tracking issues against specific code changes and faster diagnosis before the stream-mode work begins.

## Non-Goals

- MODOK does not store full source files, full doc text, raw logs, raw ticket transcripts, secrets, or customer PII.
- MODOK does not replace reading current repo files or running tests. It points; the agent reads.
- MODOK does not produce a diagnosis. It produces a debug packet. The agent reasons.
- Stream mode (AWS/Kinesis/CloudWatch) is a future vision item, not a v1 requirement. The schema accommodates event nodes but the ingestion pipeline for them is not built in v1.
- MODOK does not enforce access control in v1. It is a single-user or trusted-team tool.

## System Design

```
┌─────────────────────────────────────────────────────────────────┐
│                        Agents                                   │
│   Claude · ChatGPT · local LLM · VS Code agent · CLI user      │
└─────────────────────────┬───────────────────────────────────────┘
                          │  MCP tools  /  CLI commands
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                         MODOK                                   │
│                                                                 │
│  ┌──────────────────┐    ┌──────────────────────────────────┐   │
│  │   CLI / MCP      │───▶│   Diagnostic Retrieval Engine    │   │
│  │   (entry points) │    │   builds debug packets           │   │
│  └──────────────────┘    └──────────────┬───────────────────┘   │
│                                         │                       │
│  ┌──────────────────┐                   │                       │
│  │   LLM Gateway    │◀──────────────────┤                       │
│  │   (pluggable)    │    proposal only  │                       │
│  │  local · remote  │                   │                       │
│  └──────────────────┘                   │                       │
│                                         ▼                       │
│  ┌──────────────────┐    ┌──────────────────────────────────┐   │
│  │  Static          │───▶│   Quine Memory Graph             │   │
│  │  Ingestion       │    │   (persistent, versioned)        │   │
│  └──────────────────┘    └──────────────┬───────────────────┘   │
│                                         │                       │
│  ┌──────────────────┐                   │                       │
│  │  Optional Vector │───────────────────┘                       │
│  │  Index           │   recall booster only                     │
│  └──────────────────┘                                           │
└─────────────────────────────────────────────────────────────────┘
                          │
                          ▼
                    Debug Packet
```

### Major components

**CLI / MCP server** — two surfaces, one behavior. The CLI is the primary development interface and the MCP server exposes the same operations to agents. Both are thin entry points; logic lives in core.

**LLM Gateway** — an abstract interface with pluggable backends. Local model (Ollama/llama.cpp) is the default. Remote models (Claude, GPT-4) are optional escalation targets configured per-project or per-call. The gateway is used only for: (a) parsing unstructured ticket text into structured YAML, (b) proposing missing doc metadata, (c) proposing similarity candidates. It never writes to Quine directly.

**Static Ingestion Layer** — the mechanical pipeline. Discovers, parses, validates, and writes docs, code maps, tickets, and resolution records to Quine. Schema-driven. Fails loudly on invalid references. LLM is invoked only when a doc is missing required metadata and a proposal is needed; the proposal is surfaced for human review before being written.

**Quine Memory Graph** — the persistent store. Typed nodes with deterministic IDs (`idFrom(type, projectSlug, ...)`). Multi-project from day one — `projectSlug` is a first-class namespace in every ID. No broad property scans; all traversals follow explicit edge types.

**Diagnostic Retrieval Engine** — given a ticket (structured or freeform), extracts anchors (feature, error, environment), queries Quine for related nodes, optionally boosts recall with vector search, assembles and returns a ranked debug packet.

**Optional Vector Index** — fuzzy recall for natural-language ticket text. Candidates from vector search are always expanded through Quine before inclusion in the debug packet. Not required for Phase 4 functionality; graph-anchor similarity (shared ErrorSignature, Feature, FailureMode, etc.) is sufficient for most cases.

## Key Design Decisions

### 1. Quine as the graph store

Quine is chosen because it supports graph-oriented ingestion, event-driven graph updates, and standing queries — which are needed for the future stream-mode vision even if not used in v1. Alternatives (Neo4j, ArangoDB, a plain SQLite adjacency table) were considered; Quine's event-streaming model is the differentiator.

### 2. LLM-agnostic gateway

The LLM interface is an abstract boundary with local-first defaults. A local model (Ollama) handles ticket parsing and metadata suggestion on the Mac mini without network calls. Claude or GPT-4 are invoked only when configured and when the local model's output confidence is below threshold. This makes MODOK usable offline, cost-predictable, and portable to any agent environment.

No LLM SDK is a hard dependency. The gateway communicates over a common interface (OpenAI-compatible chat completions endpoint, which both Ollama and the major remote providers support).

### 3. Explicit metadata is truth; LLM output is a proposal

The ingestion pipeline is mechanical. Facts are written to Quine only when they are explicitly declared in validated source metadata (frontmatter, MODOK blocks, registry entries). LLM proposals go through a review gate before being promoted to trusted facts. This prevents hallucinated relationships from accumulating in the graph.

### 4. SimilarityMatch as a node, not an edge

Similarity between a `CustomerIssue` and a `KnownIssue` is a computed claim with evidence, method, score, and review status — not a simple fact. It is modeled as:

```
(:CustomerIssue)-[:HAS_SIMILARITY_MATCH]->(:SimilarityMatch)-[:MATCHES]->(:KnownIssue)
```

The `SimilarityMatch` node records: match method (graph-anchor overlap, vector similarity, hybrid, manual), score, evidence anchors, and review status. Once a match is confirmed, a stronger `[:INSTANCE_OF]` edge is added. This keeps the graph honest about the difference between "we think this might be related" and "we know this is the same issue."

### 5. Multi-project key space from day one

Every Quine node ID is namespaced by `projectSlug`. The CLI and MCP tools require a `--project` flag (or project context from config). Registries (features, modules, errors) are per-project files. This makes a single shared MODOK instance on the Mac mini serve multiple projects without ID collisions.

### 6. Typed nodes for trusted knowledge; DiagnosticNote for provisional claims

`Memory` is not a core node type. Trusted knowledge uses specific typed nodes:

- `KnownIssue` — repeatable or recognizable problem
- `Fix` — workaround, patch, config change, or remediation
- `ResolutionEvent` — a specific issue was resolved at a time by applying a fix
- `Decision` — source-backed architecture/design choice
- `Risk` — known risky area or failure mode
- `ObservationEvent` — timestamped signal from logs, tickets, deployments, or tests
- `DocSection` — source-backed evidence from docs

`DiagnosticNote` is the only provisional node type — for agent or human notes that have not yet been validated into a typed node.

### 7. Quine lifecycle and data management

Quine runs as a standalone JAR (not Docker) on both dev machines and the shared Mac mini. The JAR is the deployment unit — no container daemon required, no build step. RocksDB is the persistence backend.

All MODOK data and config lives under `~/.modok/`:
- `~/.modok/config.toml` — Quine endpoint URL, project registry paths, LLM gateway config
- `~/.modok/data/quine.db` — RocksDB graph store
- `~/.modok/quine.conf` — Quine HOCON config (webserver address, store path, persistence settings)

Quine lifecycle is manual: the developer (or launchd on the Mac mini) starts Quine before running MODOK. MODOK `ping()`s Quine on startup and gives a clear actionable error if it's unreachable. The CLI provides a `modok quine start/stop/status` convenience subgroup that wraps the JAR process, but does not require it — operators who manage Quine themselves can ignore it.

On the shared Mac mini, a launchd plist keeps Quine running as a persistent background service across reboots.

### 8. Python implementation

Python is chosen for iteration speed, natural LLM SDK integration, and consistency with the stagehand codebase (the first target project). The modular layout (`modok.core`, `modok.quine`, `modok.ingestion`, `modok.mcp`, `modok.cli`) mirrors the logical component split and allows future replacement of performance-critical pieces without rewriting the whole system. `pydantic` v2 enforces schema correctness at runtime. `ruff` + `mypy` enforce style and types statically.

## Success Metrics

- A new stagehand issue can be processed into a debug packet (relevant docs, code areas, tests, known issues, prior fixes) in under 5 seconds on the local machine.
- Doc ingestion for the stagehand project (all design docs, testing docs, known issues) completes without manual schema corrections.
- An agent using the debug packet identifies the correct feature area and relevant files on first attempt for at least 80% of test tickets drawn from stagehand's issue history.
- A new developer (or agent in a fresh session) can orient to an unfamiliar stagehand issue faster with MODOK than without it.

## References

- `docs/modok-setup-brainstorm.md` — original architecture brainstorm
- `docs/quine-setup.md` — Quine installation, config, and Mac mini launchd setup
- Quine documentation: https://docs.quine.io
- OpenAI-compatible chat completions API (used by Ollama and remote providers)
