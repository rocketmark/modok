# High-Level Design: MODOK

*Mechanical Oracle Designed Only for Knowledge*

## Problem

Diagnosing a customer issue in a software project requires orienting across many artifacts — design docs, code, tests, known issues, prior fixes, deployment events — before any useful inspection can begin. This orientation work is slow, lossy, and not retained between incidents. Agents (Claude, ChatGPT, local models, VS Code agents) repeat the same traversal from scratch every session, with no memory of what was relevant last time.

MODOK solves this by maintaining a persistent, graph-structured diagnostic memory for a project. Given a customer issue, MODOK returns a focused debug packet: the relevant docs, code areas, tests, known issues, prior fixes, and operational signals — so the agent skips orientation and starts inspecting.

## Approach

MODOK is a **Quine-backed diagnostic memory graph** with a mechanical ingestion pipeline, an LLM-agnostic query interface, and incremental pattern detection over the accumulating graph.

Four disciplines combine:

- **Graph (Quine):** stores typed, source-backed relationships — feature → module → file → test → known issue → fix. Deterministic IDs. No inferred facts stored as truth.
- **Incremental pattern detection (Quine standing queries):** Quine evaluates registered graph patterns continuously as nodes and edges are written — not on a poll or a caller-triggered traversal. When connected evidence completes a pattern (e.g., a customer issue's error signature is already covered by a known issue that already has a fix), Quine fires the moment the last piece of evidence lands, in whichever order the evidence arrived, and emits an enriched result. This is what makes Quine authoritative for *when* a workflow becomes actionable, not just for *what* is connected.
- **Vector index (optional):** fuzzy recall for vague natural-language tickets. Candidates from vector search are always validated or expanded through Quine before being treated as matches.
- **LLM (pluggable):** used only for proposals — parsing unstructured ticket text, suggesting missing metadata, proposing similarity. LLM output is never written to Quine without validation.

The guiding invariant:

```
Convention + registries are truth for structure.
Explicit frontmatter overrides convention.
LLM output is a proposal.
Quine stores validated structure.
Quine detects when that structure becomes actionable.
Files are the source of truth.
Tests verify the diagnosis.
```

### Authority model

Source systems remain authoritative for their own domain of record; MODOK is authoritative only for the investigation layer built on top of them:

| Domain | Authoritative system |
|---|---|
| Source code | Git |
| Original tickets | The ticketing/CRM system that raised them |
| Test execution | The CI system that ran them |
| Deployment events | The deployment system |
| Investigation state, evidence relationships, trigger history, workflow transitions, and the explanation of *why* a workflow advanced | **MODOK** |

MODOK never becomes the source of record for code, tickets, tests, or deployments — it ingests references to them (paths, ticket IDs, commit SHAs) and stores the graph of relationships and triggering evidence between them. This is why `Investigation` (see Key Design Decisions) records *which* standing query fired and *what evidence* completed the match, rather than duplicating the underlying issue or fix content.

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
- When connected evidence in the graph completes a registered pattern, MODOK records an investigation-ready result the moment that pattern becomes true — without any caller re-running `retrieve`/`diagnose` to notice it.

## Non-Goals

- MODOK does not store full source files, full doc text, raw logs, raw ticket transcripts, secrets, or customer PII.
- MODOK does not replace reading current repo files or running tests. It points; the agent reads.
- MODOK does not produce a diagnosis. It produces a debug packet. The agent reasons.
- Live incident streaming from external systems (AWS/Kinesis/CloudWatch) is a future vision item, not a v1 requirement — the ingestion pipeline for those sources is not built in v1. Standing-query-based pattern detection *over already-ingested graph state* is in scope; ingesting new external event sources is not.
- A generalized workflow engine, multi-agent orchestration, and arbitrary user-authored standing queries are out of scope. MODOK installs a small, fixed set of maintained standing queries — it does not expose a query-authoring surface to agents or users in v1.
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

### Detection / Trigger Path (standing queries)

The read and write paths above are both caller-initiated: something asks Quine a question, or writes a fact. This third path is not caller-initiated — it fires from *inside* Quine the moment a graph write makes a registered pattern true, regardless of which write (ticket ingestion, doc ingestion, GitHub PR merge) completes it last:

```
      Quine Memory Graph
      standing queries evaluate incrementally on every node/edge write
              │
              │ pattern match: CypherQuery enrichment (andThen) fetches
              │ the matched CustomerIssue/KnownIssue/Fix identifiers —
              │ enrichment happens inside Quine, not by MODOK re-polling
              ▼
      PostToEndpoint output
              │
              ▼
      MODOK Standing Query Adapter
      POST /webhook/{project}/standing-query
              │
    ┌─────────┴──────────────────────────┐
    ▼                                    ▼
writes Investigation node       if CustomerIssue.source_system == "github":
+ INVESTIGATES edge             posts an immediate "triggered" comment first —
(idempotent — deterministic     no LLM call, just graph-first anchors and the
 investigation_id via idFrom)   registry's declared primary files per feature —
                                 then calls the Diagnostic Retrieval Engine to
                                 assemble the full debug packet, including its
                                 existing LLM Gateway summary step (local-first,
                                 e.g. Ollama; falls back to issue.summary on
                                 failure), and posts a second "results" comment
                                 with the full packet
                                            │
                                            ▼
                              both comments are independent best-effort posts
                              (a failure in either is logged, never blocks the
                              Investigation write or the other comment)
```

The DRE's LLM involvement here is unchanged from how it already works: LLM output enriches the *summary* of already-validated graph facts, never decides *what matched*. The match itself is already settled — mechanically, by the standing query — before the DRE or its LLM Gateway is invoked at all. This keeps the "LLM output is a proposal, never the source of a written edge" invariant intact even though a local LLM call now sits in the write-back path.

This path is what makes Quine authoritative for *when* a workflow becomes actionable — not merely a durable store that MODOK polls. See Key Design Decisions §11.

### Major components

**Code Map Extractor** — a deterministic, LLM-free command (`modok extract-code-map`) that walks the repo and extracts file facts: repo-relative path, SHA-256 hash, language, role (source / test / config / docs / generated / ignored), line count, and test-to-source coverage by mirrored path convention. For Python files, symbol and import facts are extracted via `ast` (classes, functions, methods, line ranges, imports). The output is `.modok/code-map.yml` — a sorted, stable YAML artifact. The same repo state always produces the same code map. `modok ingest-docs` auto-generates the code map if one does not exist. The code map is the foundation against which doc ingestion validates source file and module claims; it is not required for registry validation or ticket ingestion.

**Demo UI** — a local-only web console for demonstrating MODOK's core workflow. A Next.js app (`ui/`) that presents a seeded customer ticket inbox, ticket detail view with notes, and a MODOK analysis panel. The UI calls `modok ingest` and `modok retrieve` via `child_process.spawn` from Next.js API routes. Ticket and note state persists in local JSON files under `ui/data/`. A top navigation bar provides MODOK branding and a freeform search that calls `modok search` with a project slug configured in `ui/config.json`. A mock mode (`MODOK_MOCK=1`) returns fixture debug packets when Quine is not running. The Demo UI is not a production surface — it has no auth, no database, and no deployment target. It exists to make MODOK's debug-packet workflow tangible to engineers and stakeholders.

**CLI / MCP server** — two surfaces, one behavior. The CLI is the primary development interface and the MCP server exposes the same operations to agents. Both are thin entry points; logic lives in core.

**LLM Gateway** — an abstract interface with pluggable backends. Local model (Ollama/llama.cpp) is the default. Remote models (Claude, GPT-4) are optional escalation targets configured per-project or per-call. The gateway is used only for: (a) parsing unstructured ticket text into structured YAML, (b) proposing missing doc metadata, (c) proposing similarity candidates, (d) per-section registry enrichment and per-field normalisation during registry bootstrapping. It never writes to Quine directly.

**Ingestion Pipeline Layer** — the mechanical pipeline. Discovers, parses, validates, and writes docs, code maps, git history, tickets, and resolution records to Quine. Schema-driven. Fails loudly on invalid references. Doc discovery uses a three-tier approach: (1) arrow-index-driven — walk `docs/arrows/index.yaml` and follow registered LLD/spec paths, inferring all metadata from the index and registries; (2) path-based inference — scan `docs/` for remaining files, infer `doc_type` from directory and `feature` from stem; (3) `unregistered` doc type — docs that don't resolve to a known feature are ingested as bare `Doc` nodes without Feature edges, surfaced as a discovery signal in the ingestion report. Frontmatter is override-only: any field can be overridden explicitly, but none are required when convention applies. LLM is invoked only when metadata cannot be inferred and a proposal is needed. Git commit history is ingested as `Commit` nodes with `TOUCHES` edges to `File` nodes via `ingest-git` (local git log, no auth). GitHub issues and merged PRs are ingested separately via `ingest-github` (GitHub REST API, requires token): issues become `CustomerIssue` nodes; merged PRs become `Fix` nodes linked to their merge commit, with `RESOLVED_BY` edges written when a PR's closing references are detected. A `GitHubPollAdapter` (implementing the existing `PullAdapter` protocol — see Standing Query Engine below and `docs/llds/webhook-receiver.md § Pull adapter`) runs the same incremental fetch as `ingest-github` on a configurable interval while `modok serve` is running, for projects that opt in. This means a real GitHub issue, opened with no webhook tunnel configured, is ingested within one poll cycle — no ngrok or public endpoint required for a live demo. It reuses `GithubIngester` and the same `last_github_sync` tracking `ingest-github` already persists; it is not a new ingestion code path, only a new caller of the existing one. Every path that writes a `CustomerIssue` node (webhook push, `ingest-github` pull, or `modok ingest <ticket_file>`) also runs a mechanical, LLM-free anchor-linking step: `raw_text` is substring/token-matched against `ErrorSignature.normalized_error` values and registered `Feature` slugs/names that already exist in the project's graph, and `HAS_ERROR` / `AFFECTS` edges are written only to matches that are already validated nodes — no anchor is ever invented. This is what gives standing queries something to watch. When mechanical linking finds nothing at all — the common case, since exact/token hits on organically-written ticket text are rare — ingestion itself invokes the Diagnostic Retrieval Engine's `parse_ticket` LLM fallback synchronously, applies the same validation (registry membership and node existence) to whatever it returns, and persists any resulting `HAS_ERROR` / `AFFECTS` edges before ingestion completes. This is the only point where LLM output is written to Quine directly from the ingestion path, and it never bypasses the validation gate used everywhere else.

**Registry Proposal Engine** — an LLM-assisted bootstrap tool, used when starting a project with no registry and no code map to derive one from. Not part of normal ingestion. Split across two CLI commands. `modok init --assisted` handles the enrichment pass: discovers all eligible docs, splits each into sections mechanically (H2 boundaries), sends sections to the LLM gateway one at a time for typed node extraction (features, modules, error signatures, failure modes, decisions, known issues), prints a `N/total` progress counter to stderr per section, and writes raw candidates to `features.raw.yml`, `modules.raw.yml`, and `errors.raw.yml` in `{repo}/registries/`. `modok normalise --project <slug>` is then run separately: reads the raw files, normalises each field type independently (separate LLM call per field to keep context small), applies a CEGIS loop to verify no new concepts were introduced, and overwrites the final `features.yml`, `modules.yml`, and `errors.yml`. No Quine interaction in either pass — this is a pre-ingestion step. The more docs the repo contains, the more complete the registry output.

**Registry Import (Arrow-Based)** — a structured alternative to the Registry Proposal Engine for projects that maintain arrow docs (`docs/arrows/index.yaml` and per-arrow `.md` files). `modok import-arrow --project <slug>` extracts features and modules mechanically from the structured arrow index and arrow doc `### Code` / `### Key Components` sections, validates all file paths against the code map, and writes `features.yml` and `modules.yml` directly. LLM is used in two narrow passes only: generating human-readable names and descriptions for modules where the slug is ambiguous, and resolving duplicate module candidates (same source files, different names) by first confirming they represent the same concept, then picking the more user-facing label. This approach is preferred over `init --assisted` when structured arrow docs exist — the output is more accurate, faster, and requires fewer LLM calls. For projects without arrow docs, the proposal engine remains the bootstrap path.

**Quine Memory Graph** — the persistent store. Typed nodes with deterministic IDs (`idFrom(type, projectSlug, ...)`). Multi-project from day one — `projectSlug` is a first-class namespace in every ID. No broad property scans; all traversals follow explicit edge types. `Investigation` is the one workflow-tracking node type: it records that a standing query fired for a `CustomerIssue`, not the underlying diagnosis (which the Diagnostic Retrieval Engine still owns).

**Standing Query Engine** — a small, fixed set of Quine standing queries, each installed idempotently by name via `modok stream install` (`POST /api/v1/query/standing/{name}`; a name that already exists is a no-op). A standing query pattern is a maintained Cypher artifact (not an embedded string) that Quine evaluates incrementally against every graph write — no polling, no caller-triggered traversal. Each standing query's output pipeline runs a `CypherQuery` enrichment stage inside Quine itself before `PostToEndpoint` delivers the match to MODOK's webhook server. This is deliberately narrow: MODOK does not expose standing-query authoring to agents or users in v1 — see Non-Goals.

**Standing Query Adapter (write-back)** — a webhook push adapter (`POST /webhook/{project}/standing-query`) that receives a fired standing query's enriched match and writes the `Investigation` node and its `INVESTIGATES` edge (idempotent — `investigation_id` is deterministic). If the triggering `CustomerIssue` came from GitHub (`source_system == "github"`), it then posts **two** independent comments to the originating issue, using the same `GITHUB_TOKEN` + `github_repo` config `ingest-github` already uses: first an immediate "triggered" comment (no LLM call — just graph-first anchors and the registry's declared primary files per feature, so it posts in about the time of a couple of Quine round-trips), then the Diagnostic Retrieval Engine is called to assemble the same debug packet a human would get from `retrieve`/`diagnose` — including that engine's existing LLM Gateway summary step (local-first, e.g. Ollama; falls back to `issue.summary` on failure) — and a second "results" comment with the full packet. The LLM only ever enriches the *prose summary* of facts the standing query already settled mechanically; it is never consulted on whether a match occurred. Best-effort throughout: either comment failing is logged and never blocks or rolls back the `Investigation` write (MODOK's authoritative record of the trigger) or the other comment.

**Diagnostic Retrieval Engine** — given a `CustomerIssue` node ID, extracts anchors (feature, error, environment), traverses Quine for related nodes, and assembles a debug packet. Candidate files accumulate typed evidence (graph traversal matches, element/function token matches, ticket mentions, recent commits, commit-message matches) which is combined into a numeric score per candidate — direct, specific evidence (a feature's own declared primary files, an exact element or ticket match) outweighs broad or undifferentiated evidence (a file merely reachable via the feature's module graph, or touched by an unrelated recent commit), and a diversity bonus rewards multiple independent corroborating signals. See `docs/scoring-brainstorm.md` for the underlying rubric. Reused as-is by the Standing Query Adapter — the full debug packet content is identical whether it was requested on demand or assembled automatically after a standing-query match (the adapter's separate immediate "triggered" comment uses a different, deliberately non-scored fast path — see Standing Query Adapter above).

**Optional Vector Index** — fuzzy recall for natural-language ticket text. Candidates from vector search are always expanded through Quine before inclusion in the debug packet. Not required for Phase 4 functionality; graph-anchor similarity (shared ErrorSignature, Feature, FailureMode, etc.) is sufficient for most cases.

## Key Design Decisions

### 1. Quine as the graph store

Quine is chosen because the core problem is storing and traversing relationships between artifacts — and Quine is purpose-built for that. Typed nodes, deterministic IDs, explicit edges, and Cypher traversal are all available without the operational overhead of a full database cluster. Alternatives (Neo4j, ArangoDB, a plain SQLite adjacency table) were considered; Quine's graph model and local JAR deployment are the differentiators for a single-user or small-team tool.

### 2. LLM-agnostic gateway

The LLM interface is an abstract boundary with local-first defaults. A local model (Ollama) handles ticket parsing, metadata suggestion, and registry proposal on the Mac mini without network calls. Claude or GPT-4 are invoked only when configured and when the local model's output fails validation. This makes MODOK usable offline, cost-predictable, and portable to any agent environment.

No LLM SDK is a hard dependency. For local calls, the gateway uses Ollama's native `/api/chat` endpoint directly (which supports `think: false` and `format: json` natively). For remote calls, the OpenAI-compatible `/v1/chat/completions` endpoint is used.

### 3. Explicit metadata is truth; LLM output is a proposal, validated before it's trusted

The ingestion pipeline is mechanical wherever possible: facts are written to Quine when they are explicitly declared in validated source metadata (frontmatter, MODOK blocks, registry entries), or when they are derived by direct token/keyword matching against registry entries that already exist as graph nodes (e.g., `CustomerIssue` anchor linking). Where mechanical matching finds nothing — free-form ticket text with no exact keyword hit — LLM output may be persisted, but only after the same validation gate applied everywhere else: the proposed value must already exist both as a registry entry and as a node in the graph. LLM output is never license to invent a new node or edge target. This prevents hallucinated relationships from accumulating in the graph while still letting genuinely paraphrased tickets get anchored automatically.

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

When a project maintains structured arrow index docs (`index.yaml`), `modok import-arrow` is the preferred bootstrap path — it produces more accurate output than LLM-from-docs extraction and validates file claims against the code map before writing.

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

### 10. Standing queries, not polling, for pattern detection

Considered: (a) Quine standing queries with Quine-side `CypherQuery` enrichment before `PostToEndpoint` delivery (chosen); (b) standing queries that emit only a bare matched node ID, with MODOK re-querying Quine to enrich (rejected for v1 — enrichment would be ordinary, more easily unit-tested Python, but would make the demo's central claim weaker: a skeptical observer could reasonably ask what Quine did beyond flag an ID); (c) a `modok stream status` polling loop with no standing-query API involved (rejected — this is the exact behavior the whole increment exists to move away from).

Consequence of (a): the enrichment stage is Quine-native Cypher/JSON configuration, which MODOK's `DummyQuine` hifi harness (fingerprint-dispatch on Cypher strings) cannot simulate. That slice of behavior is covered by a contract test against a real local Quine instance rather than the fast mocked suite — an accepted gap, consistent with how `[C]`-level specs already work elsewhere in this project (see `docs/specs/quine-client.md § Test Level Convention`).

Standing queries are a small, fixed, MODOK-maintained set — not a capability exposed to agents or end users. This keeps the graph's trigger surface auditable: every `Investigation` traces back to one of a known, reviewed set of patterns, never an ad hoc query an agent constructed at runtime.

### 11. Python implementation

Python is chosen for iteration speed, natural LLM SDK integration, and consistency with the stagehand codebase (the first target project). The modular layout (`modok.core`, `modok.quine`, `modok.ingestion`, `modok.mcp`, `modok.cli`) mirrors the logical component split and allows future replacement of performance-critical pieces without rewriting the whole system. `pydantic` v2 enforces schema correctness at runtime. `ruff` + `mypy` enforce style and types statically.

## Success Metrics

- A new stagehand issue can be processed into a debug packet (relevant docs, code areas, tests, known issues, prior fixes) in under 5 seconds on the local machine.
- Doc ingestion for the stagehand project (all design docs, testing docs, known issues) completes without manual schema corrections.
- An agent using the debug packet identifies the correct feature area and relevant files on first attempt for at least 80% of test tickets drawn from stagehand's issue history.
- A new developer (or agent in a fresh session) can orient to an unfamiliar stagehand issue faster with MODOK than without it.
- Opening a real GitHub issue whose text mentions a known error signature already covered by a `KnownIssue` + `Fix` results in an `Investigation` node and a debug-packet comment on that issue, with no caller invoking `retrieve`/`diagnose` — falsified if a manual query is required to surface the match, or if reordering the underlying evidence writes (issue first vs. fix first) changes the outcome.

## References

- `docs/modok-setup-brainstorm.md` — original architecture brainstorm
- `docs/setup.md` — platform bootstrap guide (Quine, LLM backend, `modok` install)
- `docs/project-setup.md` — adding a project repo, registry bootstrap, first ingestion
- `docs/standing-query-demo.md` — step-by-step demonstration of the Detection / Trigger Path
- Quine documentation: https://docs.quine.io
- Quine standing queries: https://docs.quine.io/components/writing-standing-queries.html
- Quine REST API (v1, matches the `/api/v1/query/cypher` endpoint this project already targets at Quine 1.10.0): https://docs.quine.io/reference/rest-api.html
- OpenAI-compatible chat completions API (used by Ollama and remote providers)
