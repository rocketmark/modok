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
- Deterministic code extraction runs before doc ingestion. The repo is the first source of truth for what files, modules, and symbols exist. Docs make claims against that known code universe — they do not define it.
- LLM-agnostic: any model (local or remote) can drive MODOK. Claude and GPT-4 are optional escalation targets, not hard dependencies.
- Stagehand is the first target project. MODOK must be useful for tracking issues against specific code changes and faster diagnosis before the stream-mode work begins.

## Non-Goals

- MODOK does not store full source files, full doc text, raw logs, raw ticket transcripts, secrets, or customer PII.
- MODOK does not replace reading current repo files or running tests. It points; the agent reads.
- MODOK does not produce a diagnosis. It produces a debug packet. The agent reasons.
- Stream mode (AWS/Kinesis/CloudWatch) is a future vision item, not a v1 requirement. The schema accommodates event nodes but the ingestion pipeline for them is not built in v1.
- MODOK does not enforce access control in v1. It is a single-user or trusted-team tool.
- The Demo UI is not a production-grade application. It has no authentication, no persistent database, and no multi-user support.

## System Design

```
             Agents / CLI / MCP / Demo UI
                           │
                           ▼
                  ┌─────────────────┐
                  │   MODOK Core    │
                  └───────┬─────────┘
                          │
          ┌───────────────┴────────────────┐
          │                                │
          ▼                                ▼
   Read / Query Path                 Write / Ingest Path
          │                                │
          ▼                                ▼
┌──────────────────────┐        ┌──────────────────────┐
│ Diagnostic Retrieval │        │ 1. Code Map          │
│ Engine               │        │    Extractor         │
│ builds debug packets │        │    (deterministic)   │
└──────────┬───────────┘        └──────────┬───────────┘
           │                               │
           │                               ▼
           │                    ┌──────────────────────┐
           │                    │ 2. Registry          │
           │                    │    Validation        │
           │                    └──────────┬───────────┘
           │                               │
           │                               ▼
           │                    ┌──────────────────────┐
           │                    │ 3. Doc / Ticket /    │
           │                    │    Resolution        │
           │                    │    Ingestion         │
           │                    │    (validates vs     │
           │                    │     code map)        │
           └───────────────────►└──────────┬───────────┘
           │                               │
           │                               ▼
           │                    ┌──────────────────────┐
           │                    │ Validation / Review  │
           │                    │ promotes trusted     │
           │                    │ structure only       │
           │                    └──────────┬───────────┘
           │                               │
           ▼                               ▼
      ┌────────────────────────────────────────┐
      │          Quine Memory Graph            │
      │ typed nodes · explicit edges · IDs     │
      └────────────────────────────────────────┘
           ▲
           │
┌──────────┴────────────┐
│ Optional Vector Index │
│ candidate recall only │
└───────────────────────┘


                    ┌──────────────────────┐
                    │     LLM Gateway      │
                    │ optional sidecar     │
                    │ proposals only       │
                    └──────────┬───────────┘
                               │
        ┌──────────────────────┴──────────────────────┐
        │                                             │
        ▼                                             ▼
Read path assistance                       Write path assistance
- parse freeform ticket text               - propose missing metadata
- extract anchors                          - structure untyped docs/tickets
- summarize / interpret results            - suggest similarity candidates
- propose similarity candidates            - never writes to Quine directly
```

### Major components

**Code Map Extractor** — a deterministic, LLM-free command (`modok extract-code-map`) that walks the repo and extracts file facts: repo-relative path, SHA-256 hash, language, role (source / test / config / docs / generated / ignored), line count, and test-to-source coverage by mirrored path convention. For Python files, symbol and import facts are extracted via `ast` (classes, functions, methods, line ranges, imports). The output is `.modok/code-map.yml` — a sorted, stable YAML artifact. The same repo state always produces the same code map. `modok ingest-docs` auto-generates the code map if one does not exist. The code map is the foundation against which doc ingestion validates source file and module claims; it is not required for registry validation or ticket ingestion.

**Demo UI** — a local-only web console for demonstrating MODOK's core workflow. A Next.js app (`ui/`) that presents a seeded customer ticket inbox, ticket detail view with notes, and a MODOK analysis panel. The UI calls `modok ingest` and `modok retrieve` via `child_process.spawn` from Next.js API routes. Ticket and note state persists in local JSON files under `ui/data/`. A top navigation bar provides MODOK branding and a freeform search that calls `modok search` with a project slug configured in `ui/config.json`. A mock mode (`MODOK_MOCK=1`) returns fixture debug packets when Quine is not running. The Demo UI is not a production surface — it has no auth, no database, and no deployment target. It exists to make MODOK's debug-packet workflow tangible to engineers and stakeholders.

**CLI / MCP server** — two surfaces, one behavior. The CLI is the primary development interface and the MCP server exposes the same operations to agents. Both are thin entry points; logic lives in core.

**LLM Gateway** — an abstract interface with pluggable backends. Local model (Ollama/llama.cpp) is the default. Remote models (Claude, GPT-4) are optional escalation targets configured per-project or per-call. The gateway is used only for: (a) parsing unstructured ticket text into structured YAML, (b) proposing missing doc metadata, (c) proposing similarity candidates, (d) per-section registry enrichment and per-field normalisation during registry bootstrapping. It never writes to Quine directly.

**Ingestion Pipeline Layer** — the mechanical pipeline. Discovers, parses, validates, and writes docs, code maps, tickets, and resolution records to Quine. Schema-driven. Fails loudly on invalid references. Doc ingestion validates `source_files` and `test_files` frontmatter claims against the code map — a claimed file that is absent from the code map produces a warning (or error in `--strict` mode). LLM is invoked only when a doc is missing required metadata and a proposal is needed; the proposal is surfaced for human review before being written.

**Registry Proposal Engine** — an LLM-assisted bootstrap tool, used when starting a project with no registry and no code map to derive one from. Not part of normal ingestion. Split across two CLI commands. `modok init --assisted` handles the enrichment pass: discovers all eligible docs, splits each into sections mechanically (H2 boundaries), sends sections to the LLM gateway one at a time for typed node extraction (features, modules, error signatures, failure modes, decisions, known issues), prints a `N/total` progress counter to stderr per section, and writes raw candidates to `features.raw.yml`, `modules.raw.yml`, and `errors.raw.yml` in `{repo}/registries/`. `modok normalise --project <slug>` is then run separately: reads the raw files, normalises each field type independently (separate LLM call per field to keep context small), applies a CEGIS loop to verify no new concepts were introduced, and overwrites the final `features.yml`, `modules.yml`, and `errors.yml`. No Quine interaction in either pass — this is a pre-ingestion step. The more docs the repo contains, the more complete the registry output.

**Quine Memory Graph** — the persistent store. Typed nodes with deterministic IDs (`idFrom(type, projectSlug, ...)`). Multi-project from day one — `projectSlug` is a first-class namespace in every ID. No broad property scans; all traversals follow explicit edge types.

**Diagnostic Retrieval Engine** — given a `CustomerIssue` node ID, extracts anchors (feature, error, environment), traverses Quine for related nodes, and assembles a debug packet. Results are prioritized by anchor match count: items matched by more anchors appear first. No numeric scoring or vector search in v1.

**Optional Vector Index** — fuzzy recall for natural-language ticket text. Candidates from vector search are always expanded through Quine before inclusion in the debug packet. Not required for Phase 4 functionality; graph-anchor similarity (shared ErrorSignature, Feature, FailureMode, etc.) is sufficient for most cases.

## Key Design Decisions

### 1. Quine as the graph store

Quine is chosen because the core problem is storing and traversing relationships between artifacts — and Quine is purpose-built for that. Typed nodes, deterministic IDs, explicit edges, and Cypher traversal are all available without the operational overhead of a full database cluster. Alternatives (Neo4j, ArangoDB, a plain SQLite adjacency table) were considered; Quine's graph model and local JAR deployment are the differentiators for a single-user or small-team tool.

### 2. LLM-agnostic gateway

The LLM interface is an abstract boundary with local-first defaults. A local model (Ollama) handles ticket parsing, metadata suggestion, and registry proposal on the Mac mini without network calls. Claude or GPT-4 are invoked only when configured and when the local model's output fails validation. This makes MODOK usable offline, cost-predictable, and portable to any agent environment.

No LLM SDK is a hard dependency. For local calls, the gateway uses Ollama's native `/api/chat` endpoint directly (which supports `think: false` and `format: json` natively). For remote calls, the OpenAI-compatible `/v1/chat/completions` endpoint is used.

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
- `Risk` — known risky area or failure mode

`DiagnosticNote` is the only provisional node type — for agent or human notes that have not yet been validated into a typed node.

### 7. Quine lifecycle and data management

Quine runs as a standalone JAR (not Docker) on both dev machines and the shared Mac mini. The JAR is the deployment unit — no container daemon required, no build step. RocksDB is the persistence backend.

All MODOK data and config lives under `~/.modok/`:
- `~/.modok/config.toml` — Quine endpoint URL, project registry paths, LLM gateway config
- `~/.modok/data/quine.db` — RocksDB graph store
- `~/.modok/quine.conf` — Quine HOCON config (webserver address, store path, persistence settings)

Quine lifecycle is manual: the developer (or launchd on the Mac mini) starts Quine before running MODOK. MODOK `ping()`s Quine on startup and gives a clear actionable error if it's unreachable. The CLI provides a `modok quine start/stop/status` convenience subgroup that wraps the JAR process, but does not require it — operators who manage Quine themselves can ignore it.

On the shared Mac mini, a launchd plist keeps Quine running as a persistent background service across reboots.

### 8. Registry bootstrapping is a pre-ingestion LLM pass

Registries (`features.yml`, `modules.yml`, `errors.yml`) must exist before ingestion can run — the ingestion pipeline validates all doc frontmatter slugs against them. For a new project, manually authoring these files requires knowing the project's taxonomy upfront.

Registry bootstrapping is split across two commands so that an hour-long enrichment run is never lost to a normalisation timeout:

**`modok init --assisted`** — the enrichment pass:

1. **Mechanical section parse** — splits each eligible doc on H2 headings. No LLM involved; deterministic and fast.
2. **Per-section LLM enrichment** — each section is sent to the LLM gateway independently. The LLM extracts typed node candidates (features, modules, error signatures, failure modes, decisions, known issues, observation events). Prints `N/total` to stderr after each section so progress is visible. Smaller context per call means lower timeout risk and more focused output.
3. **Raw write** — merged candidates are written immediately to `features.raw.yml`, `modules.raw.yml`, and `errors.raw.yml` in `{repo}/registries/`. These are the checkpoint; normalisation has not run yet.

**`modok normalise --project <slug>`** — the normalisation pass:

1. **Per-field normalisation** — reads the raw files and sends each field type (features, modules, errors) to the LLM gateway as a separate call. Smaller per-call context avoids timeouts on large candidate lists.
2. **CEGIS verification** — after each field is normalised, a verifier checks that no new concepts were introduced (RP-NORM-005) and that format constraints hold (e.g. error codes are SCREAMING_SNAKE_CASE). If verification fails, a repair call is made with counterexamples, up to `cegis_max_repairs` attempts.
3. **Write** — final `features.yml`, `modules.yml`, and `errors.yml` are written. The raw files are left in place for reference.

The more docs the repo contains, the more accurate and complete the output. The registry files are the source of truth after normalisation — neither pass re-runs automatically.

This is distinct from the `--fix` metadata proposal pass in `modok ingest`, which fills in missing frontmatter fields on individual docs after registries exist. The registry proposal pass runs first and is a prerequisite for ingestion.

### 9. Code extraction before doc ingestion (Option A)

The repo is the primary source of truth for what files, modules, and symbols exist. Docs make claims against that known universe — they do not define it.

`modok ingest-docs` requires a code map. If one does not exist it is generated automatically before ingestion proceeds (equivalent to running `modok extract-code-map` first). Passing `--no-code-map` skips generation and disables code-map validation for that run.

Consequences:
- A `source_files` claim in a doc frontmatter that is absent from the code map produces a warning by default and an error under `--strict`. This catches stale or mistyped file references.
- A `module` claim that conflicts with the code map's registry-based mapping produces a warning.
- Docs with no source file claims (HLDs, runbooks, conceptual docs) are unaffected — not every doc must reference code.
- The Registry Proposal Engine (LLM-from-docs) is demoted to a one-time bootstrap hint for projects with no code map and no existing registry. It is not invoked during normal ingestion.

The code map is language-agnostic at the file level and Python-specific at the symbol level (via `ast`). Tree-sitter for other languages is deferred.

### 10. Python implementation

Python is chosen for iteration speed, natural LLM SDK integration, and consistency with the stagehand codebase (the first target project). The modular layout (`modok.core`, `modok.quine`, `modok.ingestion`, `modok.mcp`, `modok.cli`) mirrors the logical component split and allows future replacement of performance-critical pieces without rewriting the whole system. `pydantic` v2 enforces schema correctness at runtime. `ruff` + `mypy` enforce style and types statically.

## Success Metrics

- A new stagehand issue can be processed into a debug packet (relevant docs, code areas, tests, known issues, prior fixes) in under 5 seconds on the local machine.
- Doc ingestion for the stagehand project (all design docs, testing docs, known issues) completes without manual schema corrections.
- An agent using the debug packet identifies the correct feature area and relevant files on first attempt for at least 80% of test tickets drawn from stagehand's issue history.
- A new developer (or agent in a fresh session) can orient to an unfamiliar stagehand issue faster with MODOK than without it.

## References

- `docs/modok-setup-brainstorm.md` — original architecture brainstorm
- `docs/setup.md` — full new-machine bootstrap guide (clone, Quine, config, init, first ingest)
- Quine documentation: https://docs.quine.io
- OpenAI-compatible chat completions API (used by Ollama and remote providers)
