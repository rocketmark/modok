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
- When a customer-reported error signature is independently corroborated by a CI test failure carrying the same canonical error signature — regardless of whether the issue or the failing test run was ingested first — MODOK records the corroboration and comments on the originating ticket, without a caller re-running `retrieve`/`diagnose` to notice it.
- MODOK maps a project's GitHub dependency topology — current package/version state, manifest and lockfile provenance, and historical dependency-version changes — into the graph, linked to the commits, pull requests, and source files that changed or use them, so a dependency update becomes a first-class, inspectable engineering change rather than an invisible side effect of a merged PR.
- MODOK distinguishes "a test structurally covers this ticket's affected area" from "this test is evidence for this ticket" — bare test coverage is informational context, never a ranked signal on its own; a test that also carries its own recent CI execution/failure history is scored, corroborating evidence, and MODOK never conflates the two into one signal.
- When three or more customer issues independently flag the same file as a high-confidence debug-packet candidate since that file's last commit, MODOK escalates mechanically by opening a new GitHub issue linking the contributing tickets, and keeps that issue current as further qualifying tickets arrive — surfacing a repeat-offender signal a human would otherwise only notice by re-reading tickets one at a time.
- When three or more currently-open customer issues independently affect the same feature, MODOK opens a single parent GitHub issue grouping the contributing tickets, so research on a shared root cause accumulates in one place instead of being repeated per ticket.

## Non-Goals

- MODOK does not store full source files, full doc text, raw logs, raw ticket transcripts, secrets, or customer PII.
- MODOK does not replace reading current repo files or running tests. It points; the agent reads.
- MODOK does not produce a diagnosis. It produces a debug packet. The agent reasons.
- Live incident streaming from external systems via a dedicated broker (Kafka, Kinesis, SQS, AWS/CloudWatch eventing) remains a future vision item, not a v1 requirement, and this project does not add one. GitHub Actions CI activity (workflow runs, jobs, test results) is ingested through the same existing 30-second GitHub poller already used for issues and PRs — an extension of an existing polled source, not a new streaming ingestion architecture. Standing-query-based pattern detection *over already-ingested graph state* remains in scope regardless of which poller wrote the evidence.
- A generalized workflow engine, multi-agent orchestration, and arbitrary user-authored standing queries are out of scope. MODOK installs a small, fixed set of maintained standing queries — it does not expose a query-authoring surface to agents or users in v1.
- MODOK does not enforce access control in v1. It is a single-user or trusted-team tool.
- The Demo UI is not a production-grade application. It has no authentication, no persistent database, and no multi-user support.
- Dependency-graph ingestion (package/version topology, manifest and lockfile facts, and historical dependency changes) is ingested through the same existing 30-second GitHub poller, on its own cursor — not a new broker, polling service, or standing query, and it does not depend on the `Investigation`/standing-query machinery. It surfaces graph facts a human or agent can inspect alongside ordinary code changes; it does not perform automated regression diagnosis, multi-issue aggregation, root-cause confirmation, or claim causation from recency alone.

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

**Code Map Extractor** — a deterministic, LLM-free command (`modok extract-code-map`) that walks the repo and extracts file facts: repo-relative path, SHA-256 hash, language, role (source / test / config / docs / generated / ignored), line count, and test-to-source coverage by mirrored path convention. For Python files, symbol and import facts are extracted via `ast` (classes, functions, methods, line ranges, imports). The output is `.modok/code-map.yml` — a sorted, stable YAML artifact. The same repo state always produces the same code map. `modok ingest` auto-generates the code map if one does not exist. The code map is the foundation against which doc ingestion validates source file and module claims; it is not required for registry validation or ticket ingestion.

**Demo UI** — a local-only web console for demonstrating MODOK's core workflow. A Next.js app (`ui/`) that presents a seeded customer ticket inbox, ticket detail view with notes, and a MODOK analysis panel. The UI calls `modok ingest` and `modok retrieve` via `child_process.spawn` from Next.js API routes. Ticket and note state persists in local JSON files under `ui/data/`. A top navigation bar provides MODOK branding and a freeform search that calls `modok search` with a project slug configured in `ui/config.json`. A mock mode (`MODOK_MOCK=1`) returns fixture debug packets when Quine is not running. The Demo UI is not a production surface — it has no auth, no database, and no deployment target. It exists to make MODOK's debug-packet workflow tangible to engineers and stakeholders.

**CLI / MCP server** — two surfaces, one behavior. The CLI is the primary development interface and the MCP server exposes the same operations to agents. Both are thin entry points; logic lives in core.

**LLM Gateway** — an abstract interface with pluggable backends. Local model (Ollama/llama.cpp) is the default. Remote models (Claude, GPT-4) are optional escalation targets configured per-project or per-call. The gateway is used only for: (a) parsing unstructured ticket text into structured YAML, (b) proposing missing doc metadata, (c) proposing similarity candidates, (d) per-section registry enrichment and per-field normalisation during registry bootstrapping. It never writes to Quine directly.

**Ingestion Pipeline Layer** — the mechanical pipeline. Discovers, parses, validates, and writes docs, code maps, git history, tickets, and resolution records to Quine. Schema-driven. Fails loudly on invalid references. Doc discovery uses a three-tier approach: (1) arrow-index-driven — walk `docs/arrows/index.yaml` and follow registered LLD/spec paths, inferring all metadata from the index and registries; (2) path-based inference — scan `docs/` for remaining files, infer `doc_type` from directory and `feature` from stem; (3) `unregistered` doc type — docs that don't resolve to a known feature are ingested as bare `Doc` nodes without Feature edges, surfaced as a discovery signal in the ingestion report. Frontmatter is override-only: any field can be overridden explicitly, but none are required when convention applies. LLM is invoked only when metadata cannot be inferred and a proposal is needed. Git commit history is ingested as `Commit` nodes with `TOUCHES` edges to `File` nodes via `ingest-git` (local git log, no auth). GitHub issues and merged PRs are ingested separately via `ingest-github` (GitHub REST API, requires token): issues become `CustomerIssue` nodes; merged PRs become `Fix` nodes linked to their merge commit, with `RESOLVED_BY` edges written when a PR's closing references are detected. A `GitHubPollAdapter` (implementing the existing `PullAdapter` protocol — see Standing Query Engine below and `docs/llds/webhook-receiver.md § Pull adapter`) runs the same incremental fetch as `ingest-github` on a configurable interval while `modok serve` is running, for projects that opt in. This means a real GitHub issue, opened with no webhook tunnel configured, is ingested within one poll cycle — no ngrok or public endpoint required for a live demo. It reuses `GithubIngester` and the same `last_github_sync` tracking `ingest-github` already persists. Every path that writes a `CustomerIssue` node also runs a mechanical, LLM-free anchor-linking step: `raw_text` is substring/token-matched against `ErrorSignature.normalized_error` values and registered `Feature` slugs/names that already exist in the project's graph, and `HAS_ERROR` / `AFFECTS` edges are written only to matches that are already validated nodes — no anchor is ever invented. This is what gives standing queries something to watch. When mechanical linking finds nothing at all — the common case, since exact/token hits on organically-written ticket text are rare — ingestion itself invokes the Diagnostic Retrieval Engine's `parse_ticket` LLM fallback synchronously, applies the same validation (registry membership and node existence) to whatever it returns, and persists any resulting `HAS_ERROR` / `AFFECTS` edges before ingestion completes. This is the only point where LLM output is written to Quine directly from the ingestion path, and it never bypasses the validation gate used everywhere else.

**GitHub Event Normalization (unified)** — both the real-time webhook push adapter and the 30-second `GitHubPollAdapter` produce the same typed, normalized event models (`IngestEvent` and its data variants) before any graph write happens, and both route through the same `run_ingest_event` entry point. This was previously a fork: the poll adapter called `GithubIngester`'s issue/PR ingestion functions directly, bypassing the normalized-event boundary the webhook path already used, with its own separately-maintained anchor-linking implementation duplicating the webhook path's. Unifying this was a routing refactor, not a graph-schema change — node IDs, graph shape, anchor-linking behavior, cursor behavior, investigation triggering, and GitHub comment behavior are unchanged from before, proven by characterization/parity tests written against the pre-unification behavior before the routing changed. The two anchor-linking implementations were consolidated into one shared function only once those parity tests covered both paths. This unification is a prerequisite for Continuous CI Ingestion below, not an independent cleanup — adding a third, CI-only normalized-event pattern alongside two already-inconsistent GitHub paths would have defeated the purpose of introducing one.

**Continuous CI Ingestion** — the same 30-second GitHub poller that already ingests issues and PRs is extended, not duplicated, to also discover and ingest GitHub Actions workflow runs, their attempts, jobs, and steps, and — for completed runs with configured test-result artifacts — parsed JUnit test executions and failures. Each poll cycle advances its own cursor per resource type only after that resource type's batch succeeds, with a small overlap window so re-polling cannot lose updates; a failure processing one workflow run does not block unrelated issues, PRs, or other workflow runs in the same cycle. No new transport, broker, or second polling service is introduced — this is additional resource-type coverage on the existing polling and normalization path, not new ingestion architecture. `WorkflowRun -[:TESTED_COMMIT]-> Commit`, `TestExecution -[:RAN_IN]-> WorkflowRun`, and `TestFailure -[:OCCURRED_IN]-> TestExecution` are new typed edges; `TestFailure -[:HAS_ERROR]-> ErrorSignature` reuses the existing `ErrorSignature` node type — the same one customer-issue anchor linking already points to (see ErrorSignatureMatcher below).

**ErrorSignatureMatcher (shared)** — a single deterministic, registry-backed matcher used by both the customer-issue anchor-linking path and the new JUnit test-failure ingestion path, rather than two independent attempts to normalize free text into the same `normalized_error` string space and hope they coincide. It accepts a set of candidate text fields — a ticket's title, body, and explicitly-extracted error text; a JUnit failure's type, message, assertion text, stack trace, and bounded stderr — and returns canonical, already-registered `ErrorSignature` IDs plus match provenance (matcher rule, source field, matched fragment, canonical ID). No match is a valid, common result — it never invents a new `ErrorSignature`, the same never-invent-a-node discipline anchor-linking already follows elsewhere (Key Design Decision #3). This is what makes the CI-corroboration standing query's result trustworthy: a customer issue and a test failure connect only when the matcher independently and correctly resolved both to the *same* node, not because two separate normalization attempts happened to produce matching strings by luck.

**GitHub Dependency-Graph Ingestion** — the same 30-second GitHub poller used for issues, PRs, and CI activity is extended, on its own cursor, to ingest a project's dependency topology: current package/version state, manifest and lockfile provenance, and historical dependency-version changes (added, removed, upgraded, downgraded), as a distinct set of typed nodes and edges — linked to the commits and pull requests that changed them and to the source files that import them. This is additional resource-type coverage on the existing polling and normalization path, not new ingestion architecture, and it is independent of the `Investigation`/standing-query machinery — dependency topology and historical dependency changes are useful on their own, without any ticket or investigation ever existing. It does not introduce a standing query, a dependency-specific incident workflow, or automated causation claims; a dependency change becomes relevant to an issue only through an inspectable graph path (issue → feature → file → dependency), never from recency alone. Full design in `docs/llds/dependency-graph-ingestion.md`.

**Test-Coverage CI Linking** — `TestFile` nodes are linked to their own `TestExecution`/`TestFailure` history (already ingested by Continuous CI Ingestion, above) via a new `TestExecution -[:EXECUTES]-> TestFile` edge, resolved mechanically from a JUnit `<testcase>` element's `classname` attribute: dot-to-slash conversion against pytest's default dotted-module-path convention (`tests.test_output_consistency` → `tests/test_output_consistency.py`), written only when the derived path resolves to a `TestFile` node that already exists — never inventing one, the same discipline governing every other mechanical linking step in this project. No LLM, no cross-ecosystem guessing: a JUnit result whose `classname` doesn't follow this convention (non-pytest runners — this project's own C/C++ `agent/tests/`, for example) simply doesn't link, a visible, safe gap rather than a wrong link. This is what lets the Diagnostic Retrieval Engine's `covered_tests` field (informational: "a test exists for this area," never scored on its own) distinguish itself from a test that also recently failed in CI — real, corroborating evidence, not mere structural coverage. Full design in `docs/llds/test-coverage-ci-linking.md`.

**File Escalation Pattern** — when a debug packet's high-confidence candidate files are written back to the graph (a new `CustomerIssue -[:FLAGS]-> File` edge, written at the same write-back point that already resolves `retrieve()`'s output into an `Investigation` — see Standing Query Adapter below), a new standing query fires per newly-written `FLAGS` edge, keyed on the flagging `CustomerIssue`, not the target `File` — the same per-new-evidence keying `ci-corroboration-pattern` already establishes, since `DistinctId` fires at most once per id and keying on the stable `File` would silently prevent a second or third ticket from ever re-triggering evaluation. The pattern itself only detects that a new `FLAGS` edge exists — Quine's standing-query pattern grammar rejects `WITH` clauses and aggregate functions outright (live-verified, see Key Design Decision #15) — so the actual decision, a count of qualifying `CustomerIssue`s since the file's most recent `Commit -[:TOUCHES]-> File` (found via `ORDER BY ... DESC LIMIT 1`), runs entirely inside the pattern's `CypherQuery` enrichment stage, which does support `WITH`, aggregation, and ordering (also live-verified). This keeps the threshold-and-recency decision itself inside Quine, consistent with this project's standing-query philosophy that Quine, not MODOK-side polling, is authoritative for *when* something becomes actionable (`docs/llds/standing-queries.md § Context and Design Philosophy`) — the split is between the pattern (existence) and the enrichment (aggregation), not between Quine and Python. Because a fourth, fifth, etc. qualifying issue still independently re-fires and re-passes the threshold, a `FileEscalation` node — keyed on `(file, since_commit)`, mirroring `Investigation`'s node-exists-first idempotency — accumulates rather than flatly dedups: the first qualifying delivery creates the node, opens the escalation GitHub issue, and writes an `INCLUDES` edge to each contributing `CustomerIssue`; every subsequent qualifying delivery for the same `(file, since_commit)` key adds an `INCLUDES` edge for the newly-qualifying issue (if not already present) and posts an update comment on the existing GitHub issue, rather than creating a duplicate issue or being silently dropped — the same accumulate-under-one-stable-node shape `Investigation`/`InvestigationMilestone` already establishes for CI corroboration (`docs/llds/continuous-ci-ingestion.md § Investigation and Milestone Model`). A later commit touching the file changes `since_commit`, opening a fresh escalation window with its own `FileEscalation` node — the prior window's node and issue are not reopened or merged into the new one. **The `id(ci)` pattern keying is best-effort, not a completeness guarantee**: a second live-verification pass (Key Design Decision #15) found Quine's standing-query pattern grammar cannot bind an edge to a variable at all, so no node-based key can distinguish a ticket's first `FLAGS` edge from its second — a file can cross the threshold and never receive a delivery if the crossing flag comes from a ticket whose own key already fired elsewhere. A poll-cycle reconciliation sweep, reusing the exact same processing function the standing-query branch uses, closes this every cycle — the same fast-path-plus-sweep-backstop shape `docs/llds/continuous-ci-ingestion.md`'s `TestExecution` linking already establishes for a structurally different reason (late `TestFile` registration, not a Quine grammar limit). Full design in `docs/llds/file-escalation-pattern.md`.

**Root-Cause Escalation Pattern** — a second application of the pattern-detects-existence/enrichment-computes-and-decides split established by the File Escalation Pattern above, grouping tickets by *feature* rather than by *file*, and requiring no new write-back edge at all: `CustomerIssue -[:AFFECTS]-> Feature` is already written by mechanical/LLM anchor linking on every ticket (`docs/llds/standing-queries.md § Mechanical Anchor Linking`). The standing query fires per newly-written `AFFECTS` edge, keyed on the flagging `CustomerIssue` for the same reason `file-escalation-pattern` is (a stable-target key would silently block a 2nd/3rd ticket from re-triggering evaluation) — and inherits the identical live-verified limitation (Key Design Decision #15): a ticket's *second* `AFFECTS` edge, to a different feature, can fail to re-fire, so a poll-cycle reconciliation sweep is the correctness backstop here too. The threshold counts only *currently-open* `CustomerIssue`s (`status == "open"`) that are not already linked to *any* `RootCauseEscalation` for that feature — a closed ticket no longer represents active, un-researched effort, and a ticket already accounted for under a prior escalation (open or closed) is never double-counted toward a new one. **Reset is human-driven, not time-based** (Key Design Decision #16): closing the escalation's GitHub issue is the signal that batch is handled — the next currently-open, not-yet-linked ticket that completes a fresh count of 3 opens a new `RootCauseEscalation` for the same feature, addressed by an incrementing sequence number rather than a commit-like timestamp anchor, since a feature has no equivalent to a file's last commit. Checking whether an escalation's issue is still open is a live GitHub API call, made only when a `RootCauseEscalation` already exists for the feature being evaluated — bounded by the number of currently-open escalations, not by ticket volume. Full design in `docs/llds/root-cause-escalation-pattern.md`.

**Deleted Ticket Detection** — found live during Root-Cause Escalation testing: `GithubIngester`'s incremental sync (`state=all` + `since=<updated_at>`) only ever returns issues that still exist — a genuinely *deleted* GitHub issue (distinct from a closed one) simply stops appearing in the list, with no tombstone or negative signal MODOK can observe from the incremental fetch alone. Every `status == "open"` check across the project (`RootCauseEscalation`'s threshold among them) was silently trusting a `CustomerIssue.status` that never gets corrected once its source ticket is deleted. A new poll-cycle sweep fetches the *full* current set of issue numbers from GitHub (unfiltered `state=all`, no `since` — the only way to positively confirm absence) and marks any `CustomerIssue` no longer in that set `status = "deleted"` — a new status value, distinct from `"open"`/`"closed"`, chosen specifically so every existing `status == "open"` consumer excludes deleted tickets automatically, with no changes needed at any call site. Full design in `docs/llds/github-ingestion.md § Deleted Ticket Detection`.

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

The registry proposal pass (`init --assisted` / `normalise`) runs first and is a prerequisite for ingestion; `modok ingest` itself has no separate metadata-proposal flag today — this paragraph originally described one (`--fix`), which was never implemented as a CLI option (found stale during a documentation accuracy pass; corrected here rather than left as an aspirational claim).

### 9. Code extraction before doc ingestion (Option A)

The repo is the primary source of truth for what files, modules, and symbols exist. Docs make claims against that known universe — they do not define it.

`modok ingest` requires a code map. If one does not exist it is generated automatically before ingestion proceeds (equivalent to running `modok extract-code-map` first). (This paragraph previously also described a `--no-code-map` flag to skip generation — no such flag exists on `modok ingest` today; corrected during the same accuracy pass as the `--fix` note above, rather than left as an aspirational claim.)

Consequences:
- A `source_files` claim in a doc frontmatter that is absent from the code map produces a warning. This catches stale or mistyped file references. (This bullet previously also described an `--strict` flag escalating the warning to an error — no such flag exists on `modok ingest` today; corrected during the same accuracy pass as the `--fix`/`--no-code-map` notes above.)
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

### 12. Unify GitHub event routing before extending it

Two shapes were considered for the new CI ingestion's normalized event boundary: (a) a CI-only event boundary, added new alongside the existing webhook-only `IngestEvent` path and the poll adapter's pre-existing direct-call bypass — three ingestion patterns to maintain; (b) unify first — migrate the poll adapter's issue/PR ingestion onto the same `IngestEvent`/`run_ingest_event` boundary the webhook path already uses, then add CI event types onto that one boundary. (b) was chosen: adding a third pattern to remove a two-pattern inconsistency defeats the purpose of introducing a normalized boundary at all.

This is scoped as a routing refactor, not a rewrite: the underlying `GithubIngester` graph mutations are preserved where practical, and node IDs, graph shape, anchor-linking behavior, cursor behavior, investigation triggering, and GitHub comment behavior are provably unchanged, verified by characterization/parity tests written against the pre-unification behavior before the routing changed. Consolidating the two duplicated anchor-linking implementations into one is gated on those parity tests passing first, not done opportunistically alongside the routing change. The issue/PR graph schema itself is explicitly not touched by this work.

### 13. One shared error-signature matcher, not two independent normalizations

Ticket text and JUnit failure output are categorically different (structured prose vs. stack traces and assertion output). Independently normalizing each into the same `normalized_error` string space and hoping they coincide is fragile and silently unfalsifiable — a near-miss would either never match (a missed corroboration) or accidentally match unrelated errors (a false corroboration), with no way to tell which happened after the fact.

A single `ErrorSignatureMatcher`, used by both the customer-issue anchor-linking path and the new JUnit test-failure ingestion path, with recorded match provenance and a strict no-match-means-no-edge rule, makes the matching decision inspectable and keeps the CI-corroboration standing query's result trustworthy. This extends the same never-invent-a-node discipline already governing every other mechanical linking step in this project (Key Design Decision #3) to this new evidence source.

### 14. Mechanical TestFile-to-TestExecution linking, no cross-ecosystem guessing

`classname` → repo-relative path derivation (dot-to-slash conversion, matching pytest's default dotted-module-path convention) was chosen over two alternatives considered and rejected for v1: an explicit per-project override config mapping non-standard classname shapes to paths (mirrors `.modok/dependency-map.yml`'s import-override precedent, but nothing today proves the mechanical case is insufficient — premature configuration surface), and cross-referencing the code map's extracted symbols for disambiguation (meaningfully more complexity, still needs the same derivation as its foundation, for edge cases not yet observed). The chosen approach covers pytest-convention Python tests — the majority of what a live ticket's ranked candidates actually surface today — and fails safe everywhere else: a `classname` that doesn't resolve to an existing `TestFile` node simply produces no link, never a wrong one, consistent with the never-invent-a-node discipline (Key Design Decision #3).

### 15. Aggregation lives in the standing-query enrichment stage, not the pattern — and a reconciliation sweep backstops what neither stage can guarantee

Live-verified against a running local Quine 1.10.0 (`~/.modok/quine.jar`) before any LLD or code was written for the File Escalation Pattern above, isolating the specific technical risk that determines whether a threshold-and-recency pattern (as opposed to the four existing simple existential-join patterns) is even expressible as a standing query at all.

Two hard, immediate rejections at the standing-query *pattern* compiler:

1. A `WITH` clause anywhere in the pattern — even a no-op `WITH f` — fails: `CompileError ... Wrong format for a standing query (expected `MATCH ... WHERE ... RETURN ...`)`. This is a strict single-clause grammar, not a general Cypher parser; the identical multi-`MATCH`/`WITH` shape compiles and runs fine as an ordinary one-shot query via `/api/v1/query/cypher`.
2. `count()` in a pattern's `RETURN` (no `WITH` involved) fails differently and more fundamentally: `CompileError ... Failed to resolve function 'count'` — aggregate functions are not in the pattern-evaluation engine's function registry at all.

The same `WITH`/`count(distinct ...)`/`ORDER BY ... LIMIT 1` combination registers and runs correctly inside a pattern's `enrichment_query` (the `CypherQuery` `andThen` stage) — confirmed by a full live end-to-end run: seeding one file, one commit, and issues one at a time showed no delivery after the 1st or 2nd flagging issue, a delivery with the correct `n: 3` after the 3rd, a second delivery (`n: 4`) after a 4th (confirming redelivery is possible once threshold is crossed and must be deduplicated downstream), and — after a new commit landed and three more issues arrived — a fresh delivery keyed to the new commit (`since_commit` switched, count correctly restarted from zero), proving the "since last edit" reset falls out of `ORDER BY ... DESC LIMIT 1` for free with no separate reset bookkeeping.

Consequence: standing queries in this project split cleanly into "pattern detects existence, enrichment computes and decides" — already true in spirit for `actionable-issue-pattern`'s multi-hop enrichment traversal, now confirmed to extend to aggregation and ordering as well. This is chosen over one rejected alternative: attempting the count/threshold check as a pure pattern — ruled out, not merely risky, by the live compiler rejections above.

**A third rejection, found in a second live-verification pass during Phase 2 edge-case probing, means a pure standing-query solution is incomplete, not just differently-shaped.** `DistinctId` mode can only key on the `id()`/`strId()` of a *node* bound in the pattern — attempting to key on the completing `FLAGS` relationship itself (the fix that would let every new edge, not just every new `CustomerIssue`, independently re-trigger evaluation) fails outright: `CompileError ... Assigning edges to variables is not yet supported in standing query patterns`. Live-verified consequence: a ticket's *second* `FLAGS` edge (to a different file, after that ticket's first `FLAGS` edge already fired the pattern once under `DistinctId`'s once-per-id constraint) produced no delivery at all. No available node-based key can distinguish "this ticket's first flag" from "this same ticket's later flag to a different file," so a file can genuinely cross the threshold and never receive a delivery. This reopens alternative (b) from the original decision above — a poll-based reconciliation sweep — not as a replacement for the standing query, but as a bounded-cost correctness backstop layered under it: the standing query still gives near-instant detection for the common case (preserving Key Design Decision #10's rationale where the pattern *can* express the check), and the sweep, reusing the identical processing function, guarantees no qualifying file is ever silently missed. See `docs/llds/file-escalation-pattern.md § Reconciliation Sweep` for the query and cost analysis.

### 16. Root-cause grouping key is Feature, not ErrorSignature; reset is closing the escalation issue, not a time window

Three options were considered for what "common root" means when grouping tickets into a parent investigation: group by shared `Feature` (broadest — every anchored ticket has an `AFFECTS` edge, including ones anchored only via the LLM fallback with no literal error text); group by shared `ErrorSignature` (narrower and structurally stronger, mirroring this project's existing evidence-strength hierarchy where a shared error outranks a shared feature — `docs/llds/standing-queries.md`'s pattern-strength table); or both, as two independent standing queries at different strengths, mirroring `actionable-issue-pattern`/`error-flagged-pattern`/`new-bug-report-pattern`'s existing three-tier precedent.

User-selected: **Feature only**, for v1. Coverage was the deciding factor — `HAS_ERROR` requires a ticket to mechanically or LLM-match a *registered* error signature, which a meaningful fraction of organically-written tickets never produce, while `AFFECTS` is populated on nearly every anchored ticket today. `ErrorSignature`-level grouping remains a natural, additive follow-up (not a redesign) if feature-level grouping proves too coarse in practice — deferred, not rejected.

**Reset is driven by the human closing the escalation's GitHub issue, not a heuristic.** A file has a natural recency anchor (its last commit) that `FileEscalation` uses for `since_commit`; a feature has no equivalent. Two mechanical alternatives were considered and rejected first: no reset at all (accumulate into one parent issue per feature forever) and a literal calendar window (e.g., "3 tickets within 30 days") — the latter was specifically ruled out for reintroducing the recency-alone triggering this project has avoided elsewhere (`docs/llds/dependency-graph-ingestion.md`'s stated non-goal), and for needing an arbitrary, unjustified constant. User-selected instead: closing the GitHub issue *is* the reset signal — once a human marks a batch handled, the next 3 currently-open, not-yet-linked tickets open a fresh `RootCauseEscalation` for the same feature. This requires MODOK to check an existing escalation's issue state (open/closed) via a live GitHub API call before deciding whether to append to it or open a new one — a real, new operational dependency this component introduces (`FileEscalation` never needed to read back GitHub issue state), bounded by the number of currently-open escalations per poll cycle, not by ticket volume.

**Only currently-open tickets not already linked to any prior escalation (open or closed) for the feature count toward a new threshold**, using `CustomerIssue.status` (already populated from GitHub, no new field) — the "not already linked to any prior escalation" clause is what prevents a single ticket that stays open indefinitely from being recounted every time a new escalation window opens. A ticket already included in an existing `RootCauseEscalation` is never retroactively removed if it later closes — matches `FileEscalation`'s established non-retroactive discipline (`FESC-SCOPE-003`).

## Success Metrics

- A new stagehand issue can be processed into a debug packet (relevant docs, code areas, tests, known issues, prior fixes) in under 5 seconds on the local machine.
- Doc ingestion for the stagehand project (all design docs, testing docs, known issues) completes without manual schema corrections.
- An agent using the debug packet identifies the correct feature area and relevant files on first attempt for at least 80% of test tickets drawn from stagehand's issue history.
- A new developer (or agent in a fresh session) can orient to an unfamiliar stagehand issue faster with MODOK than without it.
- Opening a real GitHub issue whose text mentions a known error signature already covered by a `KnownIssue` + `Fix` results in an `Investigation` node and a debug-packet comment on that issue, with no caller invoking `retrieve`/`diagnose` — falsified if a manual query is required to surface the match, or if reordering the underlying evidence writes (issue first vs. fix first) changes the outcome.
- After the GitHub event routing unification, issue and PR ingestion via the webhook push path and the poll path produce identical graph state, anchor-linking results, and GitHub comment behavior for the same input — falsified by any parity-test divergence between the two paths.
- A registered error signature appearing independently in both a customer-reported GitHub issue and a JUnit test failure — in either ingestion order — produces exactly one CI-corroboration milestone and one GitHub comment, using the same canonical `ErrorSignature` node the shared matcher resolved both to — falsified if reordering the two ingestions changes the outcome, if a near-match (similar but not canonically identical error text) incorrectly triggers it, or if a repeated poll cycle reprocessing the same data duplicates the milestone or comment.
- A dependency version change merged via commit or pull request appears in existing recent-change retrieval as a ranked candidate when the affected feature's files import that package — falsified if it is ranked highly based on recency alone with no inspectable feature/file/dependency usage path, or if repeated polling duplicates the same dependency change.
- A test file that both covers a ticket's affected area and recently failed in CI is surfaced as scored, corroborating evidence in that ticket's debug packet; a test file that only covers the area, with no recent failure, remains informational and never contributes to a candidate's rank — falsified if a covering-but-never-failing test outranks a candidate with genuine ticket-specific evidence, or if a `classname` that doesn't resolve to an existing `TestFile` node produces a linked (rather than absent) result.
- Three customer issues independently flagging the same file as a high-confidence debug-packet candidate, since that file's last commit, produce exactly one new escalation GitHub issue linking the contributing tickets, and a fourth or later qualifying issue updates that same escalation issue with the newly-contributing ticket rather than creating a duplicate or being silently dropped — falsified if a fourth or later qualifying issue creates a duplicate escalation issue or fails to appear on the existing one, if the qualifying count fails to reset after a new commit touches the file, or if an issue predating the file's last commit ever contributes toward the threshold.
- Three currently-open customer issues independently affecting the same feature produce exactly one new parent GitHub issue grouping the contributing tickets; a fourth or later qualifying open issue updates that same parent issue while it remains open, and three more not-yet-linked qualifying issues open a fresh parent issue once the prior one is closed — falsified if a closed ticket ever counts toward a threshold, if a duplicate parent issue is created for a feature that already has one open, if a ticket already grouped into any parent for the feature is recounted toward a new one, or if a new parent issue is opened for a feature whose existing escalation is still open.
- A GitHub ticket deleted (not closed) from the source repo has its `CustomerIssue.status` marked `"deleted"` within one poll cycle, so every existing `status == "open"` check across the project (root-cause escalation's threshold among them) automatically stops counting it — falsified if a deleted ticket keeps counting as open past one poll cycle, or if a merely-closed ticket is ever mismarked as deleted.

## References

- `docs/modok-setup-brainstorm.md` — original architecture brainstorm
- `docs/setup.md` — platform bootstrap guide (Quine, LLM backend, `modok` install)
- `docs/project-setup.md` — adding a project repo, registry bootstrap, first ingestion
- `docs/standing-query-demo.md` — step-by-step demonstration of the Detection / Trigger Path
- Quine documentation: https://docs.quine.io
- Quine standing queries: https://docs.quine.io/components/writing-standing-queries.html
- Quine REST API (v1, matches the `/api/v1/query/cypher` endpoint this project already targets at Quine 1.10.0): https://docs.quine.io/reference/rest-api.html
- OpenAI-compatible chat completions API (used by Ollama and remote providers)
