# Continuous CI Ingestion

## Context and Design Philosophy

See `docs/high-level-design.md § Continuous CI Ingestion`, `§ ErrorSignatureMatcher (shared)`, `§ Key Design Decisions #12–13` for why this exists and the two forks it resolved (unify-first over a CI-only boundary; one shared error matcher over two independent normalizations).

This component extends the existing 30-second `GitHubPollAdapter` to also discover and ingest GitHub Actions workflow runs, jobs, steps, and JUnit test results — no new transport, no new poller. It depends on a prerequisite: the poll adapter's issue/PR ingestion must route through the same normalized `IngestEvent` boundary the webhook push adapter already uses, before any CI-specific event types are added onto it. Building CI ingestion as its own, third, parallel pattern (alongside the pre-existing poll-bypass and the webhook path) would have preserved exactly the inconsistency this increment exists to remove.

```
Unify existing GitHub event routing
    → add workflow/job/test event models
    → extend the 30-second poller
    → ingest CI entities and relationships
    → add CI-corroboration standing query
```

The prerequisite is complete once issue and PR ingestion behave identically through the normalized boundary, proven by parity tests. It is scoped as a routing refactor, not an open-ended rewrite — see below.

Two principles carry over from the rest of MODOK's ingestion discipline and apply here without exception:

**Never invent a node.** A `TestFailure` gets a `HAS_ERROR` edge to an `ErrorSignature` only when the shared `ErrorSignatureMatcher` resolves it to one that already exists as a registered, graph-present node — same discipline as ticket-side anchor linking (`docs/llds/standing-queries.md § Mechanical Anchor Linking`).

**MODOK is not the source of record for CI facts.** Per the HLD's Authority model, the CI system remains authoritative for test execution; MODOK stores references (run IDs, commit SHAs, test names, bounded failure excerpts) and the relationships between them, not full logs or raw artifacts.

**This slice establishes a general pattern, not a one-off schema.** CI corroboration is the first concrete implementation of:

```
independent source observation
    → normalized graph evidence
    → standing-query correlation
    → idempotent investigation milestone
```

Every future evidence source MODOK ever ingests — a second related customer issue, a repeated CI failure, a deployment event, a regression candidate, a known-fix discovery, a fix-verification signal, a recovery confirmation — is expected to follow this same shape: an independent observation gets normalized into typed graph structure, a standing query correlates it against existing evidence, and a match produces one more idempotent milestone under a *stable, accumulating* `Investigation` for the affected ticket. None of that beyond CI corroboration is implemented in this slice. What matters now is that the `Investigation`/`InvestigationMilestone` identities introduced here (§ Investigation and Milestone Model) are shaped so a future milestone kind is just another row in the same pattern, not a reason to redesign `Investigation` again.

---

## Prerequisite: Unified GitHub Event Routing

### What was inconsistent

Three independent findings, verified directly against the running code (not assumed from prior LLD text, which had drifted — see below):

1. **The poll adapter bypasses `IngestEvent` entirely.** `GitHubPollAdapter._poll_once` calls `GithubIngester.run(since=...)` → `ingest_issue`/`ingest_pr`, which build `CustomerIssue`/`Fix` nodes and write edges inline, never constructing an `IngestEvent` or calling the `on_event` callback the `PullAdapter` protocol hands it. This was a *documented, deliberate* choice at the time (this file's own "GitHub Poll Adapter" section previously read: *"the adapter does not convert issues to `IngestEvent` and push them through `on_event` ... This reuses 100% of `GithubIngester`'s existing, tested ... logic ... with zero refactor"*) — reversed by this increment, per HLD Key Design Decision #12.
2. **`GithubIngester.ingest_issue`/`ingest_pr` duplicate `run_ingest_event`'s anchor-linking.** `GithubIngester._link_anchors` and `server.py`'s `_link_anchors_resilient` are two independently-maintained copies of the same mechanical-linkers-then-LLM-fallback sequence.
3. **The webhook path's PR handling is *less* complete than the poll/batch path's.** `GitHubAdapter.normalize_event`'s `pull_request` branch builds a bare `FixData(fix_id, summary)` — no merge commit, no closing-issue references, no dependabot handling. `GithubIngester.ingest_pr` (used by poll and by `modok ingest-github`) additionally writes `IMPLEMENTED_IN` (merge commit) and `RESOLVED_BY` (closing references) edges, and treats an *open* Dependabot PR as a pending `CustomerIssue`. `run_ingest_event`'s existing `fix` branch matches the webhook shape (thin), not the poll/batch shape (rich) — so unifying onto the *existing* `fix` branch as-is would regress poll's richer behavior, not just relocate it.

Finding 3 means "preserve existing behavior" cannot mean "keep both paths exactly as they were" — the two paths currently disagree. Per HLD constraint #4 (preserve `GithubIngester`'s mutations where practical), poll's richer behavior is the target both paths converge to. Concretely: webhook-delivered merged-PR events will start getting `IMPLEMENTED_IN`/`RESOLVED_BY` edges they did not get before. This is treated as fixing a latent gap as a direct, expected consequence of unification — not as an unrelated schema redesign (HLD constraint #7 still holds: no *new* edge types or node fields beyond what `ingest_pr` already writes today).

### What changes

- **`FixData` gains fields** (`src/modok/webhook/models.py`): `pr_url: str | None`, `merge_commit_sha: str | None`, `closing_issue_numbers: list[str]`, `is_open_dependabot: bool`. Populated by whichever caller builds the event (webhook payload or polled PR dict).
- **`run_ingest_event`'s `fix` branch is extended** to perform the edge-writing `ingest_pr` currently does inline: if `is_open_dependabot`, upsert a `CustomerIssue` instead of a `Fix` (matching `ingest_pr`'s existing special case) and return; otherwise upsert the `Fix` node, then write `IMPLEMENTED_IN` to the commit (only if `node_exists_by_parts(("commit", ...))` — never invent a `Commit` node either) and `RESOLVED_BY` to each closing-referenced `CustomerIssue` that already exists.
- **`GithubIngester.ingest_issue`/`ingest_pr` become normalize-and-dispatch wrappers**: build the same `CustomerIssueData`/`FixData` shape from the raw polled dict, then call `run_ingest_event`. Their public signatures (`issue: dict -> bool`, `pr: dict -> bool`) do not change — `GithubIngester.run()`, the poll adapter, and the `modok ingest-github` CLI command (all three current callers) require no changes themselves.
- **Dispatch goes through `asyncio.to_thread`, not a direct call.** `run_ingest_event` is synchronous and internally calls `asyncio.run(...)` (`src/modok/webhook/server.py`) — every existing caller (the webhook route, the standing-query route) reaches it via `await asyncio.to_thread(run_ingest_event, event, quine_client)` specifically to avoid this. `ingest_issue`/`ingest_pr` are themselves `async def`, already running inside an event loop (via `GithubIngester.run()`), so a direct call would raise `RuntimeError: asyncio.run() cannot be called from a running event loop`. Found while drafting Phase 5 tests, before it became a real Phase 6 bug — `ingest_issue`/`ingest_pr` must call `await asyncio.to_thread(run_ingest_event, event, self._quine)`, matching every other caller's pattern, not `run_ingest_event(event, self._quine)` directly.
- **One shared field-mapping helper per resource type** (`_customer_issue_data_from_github_issue`, `_fix_data_from_merged_pr` — exact module TBD in Phase 6) is called by both `GitHubAdapter.normalize_event` (unwrapping the webhook envelope first) and `GithubIngester.ingest_issue`/`ingest_pr` (using the raw polled dict directly), so the title→summary/body→raw_text/labels→ticket_kind/merge_commit_sha→IMPLEMENTED_IN mapping exists exactly once regardless of which envelope shape it arrived in.
- **`GithubIngester._link_anchors` is deleted.** Once `ingest_issue` routes through `run_ingest_event`, which already calls `_link_anchors_resilient`, the duplicate has no callers. This consolidation happens *after* parity tests cover both paths (see below), not opportunistically alongside the routing change.
- **Cursor behavior for issues/PRs is unchanged** — still the single `last_github_sync` field, still advanced only after `GithubIngester.run()` returns successfully. The new per-resource-type cursor (below) is additive, for workflow runs only.

### Parity discipline

Before any routing change: write characterization tests capturing current behavior for both the poll path (`GithubIngester.ingest_issue`/`ingest_pr` against representative fixture payloads — a plain issue, an edited issue, a merged PR with closing refs, an open Dependabot PR) and the webhook path (`GitHubAdapter.normalize_event` → `run_ingest_event` against the equivalent webhook payload shapes), asserting the resulting graph writes (node fields, edges, anchor-linking calls) as of *today's* code. These tests must pass against the current, pre-unification implementation first — proving they characterize reality, not a hoped-for behavior.

After the routing change, the same fixtures must produce identical graph state through both paths (aside from finding 3's now-closed gap, which is an intentional, called-out improvement to the webhook path, not a divergence). Only once these parity tests are green does the anchor-linking consolidation (deleting `GithubIngester._link_anchors`) happen.

---

## New Node Types

All new types follow the existing `idFrom` key convention: `(lowercase-hyphenated-type, project_slug, natural-key...)`.

| Type | idFrom key | Key fields |
|---|---|---|
| `WorkflowRun` | `("workflow-run", project_slug, run_id)` | `run_id`, `workflow_name`, `head_sha`, `head_branch`, `event` (trigger type), `status`, `conclusion`, `run_number`, `latest_run_attempt`, `created_at`, `updated_at`, `url`, plus expansion-state fields (below) |
| `WorkflowJob` | `("workflow-job", project_slug, run_id, run_attempt, github_job_id)` | `github_job_id`, `run_id` (parent), `run_attempt`, `name`, `status`, `conclusion`, `started_at`, `completed_at`, `url` |
| `WorkflowJobStep` | `("workflow-job-step", project_slug, run_id, run_attempt, github_job_id, step_number)` | `step_number`, `name`, `status`, `conclusion`, `started_at`, `completed_at` |
| `TestExecution` | `("test-execution", project_slug, run_id, run_attempt, classname, test_name)` | `suite_name`, `test_name`, `classname`, `run_attempt`, `status` (pass/fail/skip/error), `duration_seconds` |
| `TestFailure` | `("test-failure", project_slug, run_id, run_attempt, classname, test_name)` (same natural key as its `TestExecution` — at most one failure record per execution) | `failure_type`, `message`, `assertion_text`, `stack_trace_excerpt` (bounded — see Non-Goals), `observed_at`, `run_attempt`, matcher provenance fields (see ErrorSignatureMatcher); reserved-not-implemented: `latest_outcome`, `superseded_by`, `resolved_at`, `is_current` (see § Historical vs. Current Failure State) |
| `InvestigationMilestone` | `("investigation-milestone", project_slug, investigation_id, milestone_kind, *evidence_key)` | `milestone_kind` (e.g. `"ci-corroborated"`), `standing_query_name`, `created_at`, plus non-authoritative presentation fields (see § Investigation and Milestone Model) |

**`run_attempt` is part of `TestExecution`/`TestFailure`'s key, not just `WorkflowJob`'s.** A re-run (fresh `run_attempt` under the same `run_id`) produces new `TestExecution`/`TestFailure` nodes rather than overwriting the prior attempt's — this is what lets a flaky test's original failure remain in the graph as historical evidence even after a later attempt passes (§ Historical vs. Current Failure State), instead of the retry's result silently erasing it.

**No `WorkflowAttempt` node.** GitHub's re-run mechanism produces multiple attempts of the same `run_id`; rather than a separate node type, `run_attempt` is carried as a property on every node scoped to a specific attempt (`WorkflowRun.latest_run_attempt` for "which attempt is current"; `WorkflowJob`/`WorkflowJobStep`/`TestExecution`/`TestFailure`'s own `run_attempt` for "which attempt produced this specific record"). See Decisions & Alternatives for why a node wasn't used, and § Workflow Job Identity for why `run_attempt` is part of the *key*, not just an informational property, for `WorkflowJob`.

New edges:

```
WorkflowRun -[:HAS_JOB]-> WorkflowJob
WorkflowJob -[:HAS_STEP]-> WorkflowJobStep
TestExecution -[:RAN_IN]-> WorkflowRun
TestFailure -[:OCCURRED_IN]-> TestExecution
TestFailure -[:HAS_ERROR]-> ErrorSignature        (reuses the existing node type; only written on a matcher hit)
WorkflowRun -[:TARGETED_COMMIT]-> Commit          (neutral source association — see below)
WorkflowRun -[:TESTED_COMMIT]-> Commit            (asserts meaningful execution — see below)
Investigation -[:HAS_MILESTONE]-> InvestigationMilestone
InvestigationMilestone -[:EVIDENCED_BY]-> TestFailure
InvestigationMilestone -[:EVIDENCED_BY]-> WorkflowRun
```

### Workflow Job Identity

`WorkflowJob`'s idFrom key includes `run_attempt` explicitly (`("workflow-job", project_slug, run_id, run_attempt, github_job_id)`), not just `github_job_id`. This does not depend on knowing, in advance, whether GitHub's own job IDs are globally unique or merely unique-per-attempt — the composite key is correct either way: if GitHub's `github_job_id` values turn out to already be globally unique across attempts, the extra key segments are redundant but harmless; if they are not (e.g. reused or renumbered per attempt), a rerun's jobs still cannot collide with or silently overwrite the prior attempt's job/step history. Graph correctness does not wait on verifying GitHub's actual behavior (see Open Questions) — it is safe under either possibility from the start.

### Targeted vs. Tested Commit

Two distinct edges, not one, because "GitHub associated this run with this commit" and "this commit was meaningfully exercised by CI" are different claims:

- **`WorkflowRun -[:TARGETED_COMMIT]-> Commit`** — written whenever GitHub identifies `head_sha` for the run and the `Commit` node already exists, *regardless* of the run's `status`/`conclusion`. This is neutral source provenance: "this run was created for this commit," nothing more. No equivalent edge already exists elsewhere in the graph to reuse — `Fix -[:IMPLEMENTED_IN]-> Commit` asserts a fix's origin, a different relationship in both direction and meaning.
- **`WorkflowRun -[:TESTED_COMMIT]-> Commit`** — written only if, additionally, the run's `conclusion` is not `"cancelled"`, `"startup_failure"`, or `"action_required"` (exact qualifying set may grow in Phase 6 as real conclusion values are observed; the principle is "the run's jobs meaningfully started," not merely "the run object exists"). A run cancelled or failed at the infrastructure level before any job executed gets `TARGETED_COMMIT` but not `TESTED_COMMIT` — it must not assert "this commit was CI-tested" when nothing tested it.

Both are gated on `node_exists_by_parts(("commit", project_slug, head_sha))` — never invent a `Commit` node early.

**Future standing queries should pick the edge that matches what they actually need**: a query needing only "which commits does CI activity touch at all" traverses `TARGETED_COMMIT`; a query depending on "this commit was actually exercised by tests" (e.g. a future fix-verification or regression-candidate pattern) traverses `TESTED_COMMIT`. `ci-corroboration-pattern` itself traverses neither — it reaches `WorkflowRun` via `TestExecution -[:RAN_IN]->`, not via a commit edge — this distinction matters for future patterns, not this one.

**Reconciliation is periodic and bidirectional, not just additive.** An earlier version of this design relied solely on the overlap window (below) reprocessing the same `WorkflowRun` after `ingest-git` catches up — found during the edge-case probe to be an unreliable guarantee: a *completed* run's `updated_at` never changes again, so once the cursor advances past the overlap window, that run is never revisited by the normal "changed since cursor" fetch, and if `ingest-git`'s own cadence lags behind that window, the edge is permanently missed, not just delayed. Fixed by adding an explicit, independent reconciliation step, run once per poll cycle regardless of cursors, per project:

1. **Add `TARGETED_COMMIT`/`TESTED_COMMIT`** for any `WorkflowRun` whose `head_sha` now matches an existing `Commit` node but is missing the edge(s) its current `conclusion` qualifies for.
2. **Remove `TESTED_COMMIT`** for any `WorkflowRun` whose edge already exists but whose `conclusion` — re-read from GitHub — no longer qualifies (a run's conclusion can change after a manual re-run resets it). Implemented via `replace_edges_by_parts`, the same reconciliation primitive `HAS_ERROR` already uses, rather than a one-way `write_edge_by_parts` — so the edge's presence always reflects current source state, not just whatever was true the first time it was checked.

This is cheap (bounded by however many such gaps currently exist, typically zero) and idempotent, and it decouples both edges' eventual consistency from the relative timing of independently-scheduled ingestion paths.

---

## ErrorSignatureMatcher

A single deterministic, registry-backed matcher, used by both the ticket-side anchor-linking path (`link_customer_issue_error_anchors`, replacing its current inline word-boundary substring check with a call into this matcher) and the new JUnit test-failure ingestion path.

```python
@dataclass
class ErrorSignatureMatch:
    error_slug: str              # canonical registered error signature ID
    normalized_error: str
    matcher_rule: str            # e.g. "word_boundary_substring"
    source_field: str            # e.g. "body", "stack_trace"
    matched_fragment: str        # the actual matched text, for provenance/debugging

class ErrorSignatureMatcher:
    def match(
        self, candidate_fields: dict[str, str | None], registry: Registry
    ) -> list[ErrorSignatureMatch]:
        """Check each candidate field, in a fixed priority order, against every
        registered error signature's normalized_error string using a word-boundary
        match (same algorithm as today's link_customer_issue_error_anchors — not
        a rewrite, an extraction). Returns one ErrorSignatureMatch per distinct
        error_slug matched, first field/fragment that matched it. Never invents
        an error signature: only normalized_error values already present in the
        registry are checked against; the caller is still responsible for
        confirming the corresponding ErrorSignature node exists in the graph
        before writing any edge (never-invent-a-node discipline)."""
```

Candidate fields, by caller:

- **Customer issue**: `title`, `body`, `explicit_error_text` (from `mentioned_files`/explicit LLM-fallback extraction, when the mechanical linker found nothing and the LLM fallback ran).
- **JUnit failure**: `failure_type`, `message`, `assertion_text`, `stack_trace` (full), `stderr` (bounded — only included when the artifact provides it and it is under a size threshold; consistent with the HLD's "no raw logs" non-goal).

The check order and word-boundary algorithm are unchanged from today's `link_customer_issue_error_anchors` (`docs/llds/standing-queries.md § Error linking algorithm`) — this is an extraction into a shared, multi-field-aware form, not a new matching strategy. **No match is a valid, common result** — a `TestFailure` with no `HAS_ERROR` edge is simply a test failure MODOK has no error-signature evidence for yet; it still exists as a node (for the corroboration pattern to eventually connect, if a later poll or registry update makes a match possible) but does not participate in any standing query until it does.

### Parity discipline

**Same risk class as the poll/webhook routing unification (§ Prerequisite), same gate.** `link_customer_issue_error_anchors` is live, shipped, tested behavior feeding `error-flagged-pattern`/`actionable-issue-pattern` today — "extraction, not rewrite" is a design intent, not a guarantee, and re-pointing an already-working matching path through a new shared interface deserves the same before/after proof the GitHub-routing change got, not just a good-faith description.

Before repointing `link_customer_issue_error_anchors` at `ErrorSignatureMatcher`:

1. Write characterization tests capturing `link_customer_issue_error_anchors`'s current behavior (single-field, `raw_text` only) across: exact registered matches, normalization behavior, no match, multiple possible matches, title-only matches, body-only matches, duplicate text appearing in both title and body, and bounded/malformed input.
2. Run both the current implementation and the extracted `ErrorSignatureMatcher` against the same inputs.
3. Assert identical canonical `ErrorSignature` IDs and resulting `HAS_ERROR` relationships between the two — not identical internal call shapes or log output, which are free to differ.
4. Only then switch `link_customer_issue_error_anchors`'s production call site to go through `ErrorSignatureMatcher`.

Once parity is established, both the customer-issue path and the JUnit-failure path use the one shared matcher, so `ci-corroboration-pattern` joins through genuinely canonical `ErrorSignature` identity on both sides — not two separately-evolving copies of "word-boundary match against a registry string" that happen to agree today.

---

## Poll Cycle Extension

**Discovery and per-run expansion are two independent concerns, not one cursor.** An earlier version of this design made `last_workflow_sync` responsible for both "have we seen this run" and "did we finish processing it," advancing only past runs that fully succeeded. Found during the Phase 2 review to create exactly the failure mode the isolation requirement exists to prevent: a single permanently-malformed or unreachable run (a corrupt artifact, a repo permission edge case) would hold `last_workflow_sync` back indefinitely, and since discovery of *every newer* run is gated on that same cursor, one bad run would silently stop MODOK from ever seeing anything newer. Fixed by separating them:

- **Discovery high-water mark** (`last_workflow_sync`) — advances based purely on which runs have been *discovered* (their existence and top-level metadata fetched and upserted), never gated on whether their jobs/steps/tests finished expanding. This is what lets the poller keep moving forward through new activity regardless of any one run's expansion trouble.
- **Per-run expansion state** — a durable property on the `WorkflowRun` node itself, tracking that specific run's own progress independent of the discovery cursor:

  ```
  discovered → expansion_pending → complete
                                 ↘ partially_ingested ↘
                                 ↘ retryable_failure  → (retry) expansion_pending
                                 ↘ terminal_failure
  ```

  `WorkflowRun.expansion_state` (one of the six values above), plus `expansion_attempts: int`, `expansion_last_error: str | None`, `expansion_last_attempted_at: str | None` for diagnosability. No separate queue or job-tracking infrastructure — the state lives on the node that's already being upserted every cycle; the "retry backlog" is simply "query for `WorkflowRun` nodes not yet `complete` or `terminal_failure`," not a new durable structure. This satisfies the task's own framing: a small state/checkpoint model, not a job queue.

Existing per-project loop, extended per project per 30-second cycle:

1. Fetch issues and PRs changed since `last_github_sync` (unchanged, existing cursor).
2. Fetch workflow runs changed since `last_workflow_sync`. For each: upsert the `WorkflowRun` node with its top-level metadata (setting `expansion_state = "discovered"` if new, leaving an existing run's state alone if this is a metadata-only update). **Advance `last_workflow_sync` immediately after this step**, to the most recent `updated_at` fetched — discovery does not wait on anything below.
3. Separately, query for every `WorkflowRun` (across however many discovery cycles back) whose `expansion_state` is not `complete` or `terminal_failure` — this cycle's expansion backlog, independent of what step 2 just discovered.
4. For each run in that backlog: set `expansion_state = "expansion_pending"`, increment `expansion_attempts`, set `expansion_last_attempted_at`. Expand into jobs and steps (separate GitHub Actions API calls, keyed by `run_id` + current `run_attempt`). **Write each successfully-fetched entity as it's obtained** — a `WorkflowJob`/`WorkflowJobStep` that's fetched successfully is upserted immediately, not buffered and discarded if a later step in this same run's expansion fails. This is what makes "successful portions of a partially expanded run remain idempotently stored" true: a run whose job-list succeeds but whose artifact download fails still has its jobs/steps in the graph, not none of them.
5. Resolve the run's `head_sha`; write/reconcile `TARGETED_COMMIT`/`TESTED_COMMIT` per their own criteria (§ Targeted vs. Tested Commit) — this is itself one of the incrementally-written sub-steps, not gated on the rest of expansion succeeding.
6. For completed runs (`status == "completed"`) with a configured test-result artifact pattern (opt-in per-project setting — see `docs/customize-for-your-project.md`; a project with nothing configured treats this run as expansion-`complete` once jobs/steps and the commit edges are done, skipping test ingestion entirely, not as a failure): fetch the matching artifact.
7. Download and unzip the artifact; parse JUnit XML into `TestExecution` records (keyed by the run's current `run_attempt`, § Workflow Job Identity's sibling reasoning for `TestExecution`/`TestFailure`).
8. For each failed/errored `TestExecution`, run it through `ErrorSignatureMatcher` and create/upsert a `TestFailure` node, writing `HAS_ERROR` only on a match.
9. Set the run's final `expansion_state` for this attempt: `complete` if every applicable sub-step (2–8, as configured for this project) succeeded; `partially_ingested` if some sub-steps succeeded and at least one failed (jobs written, but artifact parsing failed, for example); `retryable_failure` if nothing new was written this attempt and the failure looks transient (API error, rate limit); `terminal_failure` if the failure looks permanent (confirmed-corrupt artifact after a bounded number of attempts — exact threshold a Phase 6 detail). A run left in `partially_ingested`/`retryable_failure` is picked up again by step 3 on a later cycle — retries simply re-run the same fetch-and-upsert sequence, which is naturally safe since every write is idempotent on its deterministic key.
10. Independently of all of the above (not gated on any cursor or expansion state): run the `TARGETED_COMMIT`/`TESTED_COMMIT` reconciliation sweep (§ Targeted vs. Tested Commit) once per cycle, per project.

**Failure isolation, with distinguishable causes.** A failure expanding one workflow run does not prevent other workflow runs (via the independent discovery cursor above), or unrelated issues/PRs, from being processed in the same cycle. Four distinct failure causes are logged differently, not lumped into one generic line — found during the edge-case probe that an operator otherwise can't tell a permanent misconfiguration from a transient blip:

- **Actions API error** (job/step/artifact-list fetch failed, e.g. 5xx or timeout) — logged as transient; contributes to `retryable_failure`.
- **No artifact matches the configured pattern for this run** — logged distinctly from a fetch error; this is not necessarily a failure (a run whose test job didn't execute has no results to find) and still reaches `complete` for the sub-steps it does apply to, but is visible rather than silent, so a genuinely wrong pattern configuration is discoverable.
- **Artifact fetched but corrupt or truncated** (fails to unzip, or unzips to something that isn't valid XML at all) — logged as a data-integrity issue, distinct from a dialect the parser doesn't recognize (Open Questions, below); repeated corruption over several attempts escalates to `terminal_failure` rather than retrying forever.
- **Rate limit (429)** — logged with the same `Retry-After`-aware handling `GithubIngester`'s existing rate-limit behavior already has (`docs/llds/github-ingestion.md § Rate Limiting`), extended to the new Actions API calls; contributes to `retryable_failure`.

**Overlap window** for the discovery cursor itself: `since` is compared against GitHub's own `updated_at` timestamps the same way issues already are — a small overlap (re-checking items at or slightly before the cursor, deduplicated by deterministic ID on write) avoids a boundary item being skipped due to clock/pagination edge cases, mirroring the existing PR incremental-fetch's `<=`/`>` boundary handling. This is a defense against clock/pagination skew for *discovery*; it has nothing to do with expansion retries (handled entirely by `expansion_state`, above) or `TESTED_COMMIT` eventual consistency (handled entirely by the reconciliation sweep).

---

## Idempotency and Dedup

An unchanged poll cycle (nothing new since last cursor, nothing new in the expansion backlog) must not duplicate:

- **Nodes** — every new type uses `upsert_node` against its deterministic `idFrom` key; re-polling the same `WorkflowRun`/`WorkflowJob`/`WorkflowJobStep`/`TestExecution`/`TestFailure` overwrites in place, never creates a second node. A new `run_attempt` produces new `TestExecution`/`TestFailure`/`WorkflowJob` nodes (by design — see § Historical vs. Current Failure State), not a duplicate of the prior attempt's.
- **Edges** — written via `write_edge_by_parts`/`replace_edges_by_parts` as appropriate; re-writing the same edge is a no-op at the Quine level.
- **Test failures** — `TestFailure`'s natural key is the same as its `TestExecution`'s (run_id + run_attempt + classname + test name), so re-parsing the same run's artifact twice produces the same node, not a duplicate failure record.
- **Investigation milestones and GitHub comments** — see § Investigation and Milestone Model, below; governed by `InvestigationMilestone`'s own deterministic key, not `Investigation`'s (which is intentionally stable and shared across many milestones — see below).

---

## Investigation and Milestone Model

> **Compatibility invariant.** A `CustomerIssue` may currently be connected to multiple `Investigation` nodes created by different standing-query generations and identity schemes — the three pre-existing patterns' one-`Investigation`-per-firing model (`docs/llds/standing-queries.md § Investigation Node`) and this section's stable-per-ticket-plus-milestones model coexist, unmigrated, in this slice. Consumers must not assume `INVESTIGATES` (or any future equivalent) is singular per `CustomerIssue`. A new consumer needing "the" investigation for a ticket must query by investigation type, identity version, standing-query definition, or milestone semantics — not by "the one `Investigation` this ticket has," which is not a guarantee this design provides. The `Investigation`+`InvestigationMilestone` model in this section is the preferred direction for future work; migrating the three pre-existing patterns' identities onto it is deferred, not attempted in this slice (Open Questions, below).

**One stable `Investigation` per `(project, ticket)`; many `InvestigationMilestone` records underneath it.** This is the concrete first application of the general pattern in § Context and Design Philosophy, and the resolution to a real design mistake caught during Phase 2 review: an earlier version of this LLD tried to give each corroborating `TestFailure` its own `Investigation` (by folding `workflow_run_id`/`test_failure_id` into `investigation_id`). That would have worked for *this* slice's demo, but does not generalize — MODOK's longer-term investigation model needs one accumulating record per ticket that many independent pieces of evidence (CI failures today; related issues, deployments, fix verifications, later) attach to over time, not a proliferating family of single-purpose investigation nodes.

```
CustomerIssue
    → Investigation                         idFrom('investigation', project_slug, source_system, ticket_id)
        → InvestigationMilestone A          idFrom('investigation-milestone', project_slug, investigation_id,
             -[:EVIDENCED_BY]-> TestFailure A                "ci-corroborated", error_signature_slug, *test_failure_A_key)
             -[:EVIDENCED_BY]-> WorkflowRun A
        → InvestigationMilestone B          idFrom('investigation-milestone', project_slug, investigation_id,
             -[:EVIDENCED_BY]-> TestFailure B                "ci-corroborated", error_signature_slug, *test_failure_B_key)
             -[:EVIDENCED_BY]-> WorkflowRun B
        → (future: milestones for related issues, deployment correlation, fix verification, ...)
```

**`Investigation`'s identity is stable and evidence-free** — `investigation_id = f"{source_system}-{ticket_id}"`, no pattern name or evidence baked in. Writing it is a genuine get-or-create: `upsert_node` on this key is safe to call every time a new milestone attaches, whether or not the `Investigation` already exists. This is a different `Investigation` identity scheme from the one the three pre-existing patterns use (§ below) — deliberately: those three are unchanged in this slice, not migrated.

**Each `InvestigationMilestone`'s identity is specific to its evidence** — for CI corroboration: `("investigation-milestone", project_slug, investigation_id, "ci-corroborated", error_signature_slug, *test_failure_natural_key)`. Because this embeds the specific `TestFailure`'s own natural key, two different corroborating test failures for the same ticket always produce two different, independently-existing milestones — never an overwrite, never a collision.

### Handling a `ci-corroboration-pattern` match

New `IngestEvent.kind == "milestone"` and `MilestoneData` (`src/modok/webhook/models.py`):

```python
@dataclass(frozen=True, eq=True)
class MilestoneData:
    source_system: str
    ticket_id: str
    milestone_kind: str          # "ci-corroborated" for this pattern
    standing_query_name: str
    workflow_run_id: str         # presentation/notification convenience — see below
    test_failure_id: str         # presentation/notification convenience — see below
    error_signature: str
```

In `run_ingest_event`'s new `milestone` branch:

1. Compute `investigation_id = f"{source_system}-{ticket_id}"`; upsert the `Investigation` node (get-or-create — safe every time) and its `-[:INVESTIGATES]->` edge to the `CustomerIssue`, same as the existing `investigation` branch does today.
2. Compute this milestone's own deterministic key (above) and check `node_exists_by_parts` on it *first*. If it already exists, stop — this exact test failure has already produced a milestone (and, if it was the first, a comment); nothing more to do. This is the dedup point, not `Investigation`'s existence (which is expected to already exist most of the time, by design).
3. **Before writing the new milestone**, check whether the `Investigation` already has *any* `HAS_MILESTONE` edge to an `InvestigationMilestone` with `milestone_kind == "ci-corroborated"`. Record this as `is_first_ci_transition` — true only if none exist yet.
4. Upsert the `InvestigationMilestone` node, write `Investigation -[:HAS_MILESTONE]->` to it, and write `-[:EVIDENCED_BY]->` edges to the specific `TestFailure` and `WorkflowRun` this milestone is about. This step always happens for a new milestone, regardless of `is_first_ci_transition`.
5. **Post a GitHub comment only if `is_first_ci_transition` is true.** A second, third, etc. corroborating `TestFailure` for a ticket that has already had its first CI-corroboration comment is fully recorded in the graph (step 4 happens every time) but produces no additional comment — see § Notification Wording for why, and for the exact one-time comment's content.

`workflow_run_id`/`test_failure_id` on `MilestoneData` exist *only* to make the (possibly-posted) comment specific and readable — they are not part of any node's identity and are not authoritative for anything; the graph's authoritative record of which test failure a milestone is about is always the `EVIDENCED_BY` edge from step 4, whether or not that particular milestone produced a comment.

A second, later-arriving corroborating `TestFailure` for the *same* `CustomerIssue` repeats this whole sequence: step 1 is a no-op (Investigation already exists), step 2 finds no existing milestone for *this* test failure's key (even though the ticket already has one from the first), step 3 now finds `is_first_ci_transition = False` (a prior `ci-corroborated` milestone already exists), so step 4 writes the new milestone and its evidence edges but step 5 posts no comment — the first milestone, and its comment, are completely untouched either way.

### Notification Wording

**The CI-corroboration comment is deliberately standalone in v1** — no comment discovery, no scraping existing GitHub comments, no attempt to locate or link to an earlier debug-packet comment on the same issue. Doing any of that would couple this standing query to incidental presentation history (whether an earlier comment is still there, still findable, hasn't been edited/deleted), introduce a new GitHub read path this component doesn't otherwise need, and make correctness depend on comment-scraping succeeding. None of that is worth it for what the comment needs to say.

Instead, the comment's *wording* — not a link or a lookup — carries the connection: it must read as **additional evidence for the same issue**, never as the start of a new investigation, and never as superseding an earlier debug packet. Example:

```markdown
MODOK found additional CI evidence related to this issue.

A structured test failure matched the same registered error signature:

Error: {error_signature}
Test: {test_failure_id}
Workflow: {workflow_name}
Commit: {head_sha}
Run: {workflow_run_id}

This evidence has been added to MODOK's investigation history for this issue.
```

Requirements on this wording (exact copy is a Phase 6 detail, but the constraints below are not):

- Must not say MODOK "opened" or "started" a new investigation.
- Must not imply this supersedes an earlier debug packet.
- Must not require an earlier MODOK comment to exist — the wording is correct whether or not one does.
- Must not post a link to an earlier comment unless a stable URL/ID for it is already persisted and available through an existing abstraction (none is, today — so v1 never does this).
- Must not add a new GitHub search or comment-scanning mechanism to try to find one.

**Posted once, on the issue's first CI-corroboration transition** (§ above) — not once per corroborating `TestFailure`. Later CI evidence keeps accumulating in the graph (new milestones, new `EVIDENCED_BY` edges) without producing repeated transition comments, so a ticket that racks up many corroborating test failures over time doesn't turn into a wall of near-identical GitHub comments.

**Explicit v1 design statement**: GitHub comments produced by different MODOK investigation workflows may remain separate — the `new-bug-report-pattern`/`actionable-issue-pattern`/`error-flagged-pattern` debug-packet comments and the `ci-corroboration-pattern` evidence comment are not consolidated or cross-linked in this slice. The `CustomerIssue` (the GitHub issue itself) is the shared user-visible context a reader already has open; MODOK doesn't need to re-derive that connection through comment cross-referencing. Consolidating MODOK's output into one persistent, updating status comment per issue is a plausible future direction, explicitly deferred, not attempted here.

### Worked example: two test failures corroborating one investigation

```
CustomerIssue(github, "42")
  -[:HAS_ERROR]-> ErrorSignature("db-timeout")

TestFailure(run_id=100, run_attempt=1, classname="TestDb", test_name="test_connect")
  -[:HAS_ERROR]-> ErrorSignature("db-timeout")
  -[:OCCURRED_IN]-> TestExecution(run_id=100, ...) -[:RAN_IN]-> WorkflowRun(run_id=100)

# ci-corroboration-pattern fires for id(TestFailure@run_id=100). is_first_ci_transition=True
# (no prior ci-corroborated milestone exists on this Investigation yet). Result:
Investigation("github-42")
  -[:INVESTIGATES]-> CustomerIssue(github, "42")
  -[:HAS_MILESTONE]-> InvestigationMilestone("github-42", "ci-corroborated", "db-timeout", run_id=100, ...)
                         -[:EVIDENCED_BY]-> TestFailure(run_id=100, ...)
                         -[:EVIDENCED_BY]-> WorkflowRun(run_id=100)
# One GitHub comment posted (the "additional CI evidence" wording, § Notification Wording),
# referencing run 100 — this is the issue's first CI-corroboration transition.

# Days later, a second, unrelated workflow run also fails with the same error:
TestFailure(run_id=105, run_attempt=1, classname="TestDb", test_name="test_connect")
  -[:HAS_ERROR]-> ErrorSignature("db-timeout")
  -[:OCCURRED_IN]-> TestExecution(run_id=105, ...) -[:RAN_IN]-> WorkflowRun(run_id=105)

# ci-corroboration-pattern fires again — id(TestFailure@run_id=105) is a genuinely new
# distinct id, so DistinctId mode fires for it independently of run_id=100's firing.
Investigation("github-42")                      # same node, upserted (get-or-create, no-op on existing fields)
  -[:HAS_MILESTONE]-> InvestigationMilestone(...) # the run_id=100 milestone, untouched
  -[:HAS_MILESTONE]-> InvestigationMilestone("github-42", "ci-corroborated", "db-timeout", run_id=105, ...)
                         -[:EVIDENCED_BY]-> TestFailure(run_id=105, ...)
                         -[:EVIDENCED_BY]-> WorkflowRun(run_id=105)
# is_first_ci_transition=False this time (the run_id=100 milestone already exists) — the
# new milestone and its evidence edges are written exactly as before, but NO second GitHub
# comment is posted. The investigation now has two pieces of CI corroboration on record,
# queryable independently or together, but the issue's comment thread shows only one
# CI-corroboration notification, not one per corroborating failure.
```

### Historical vs. Current Failure State

`TestFailure` means **"this failure was observed"** — it does not mean "this test is currently failing," and the CI-corroboration milestone it produces is a historical record, not a live status. If the test in question is flaky and a later attempt (a new `run_attempt`, a new `TestExecution`/`TestFailure` key) passes, the *original* `TestFailure` node — and any milestone/comment it already produced — is not retracted or updated. This is intentional and consistent with how `Investigation` behaves everywhere else in this project: it records that evidence became true at some point, not a continuously-reconciled live state.

The schema leaves room for this to be tightened later without a rename: `observed_at` and `run_attempt` are real fields on `TestFailure` as of this slice; `latest_outcome`, `superseded_by`, `resolved_at`, and `is_current` are reserved names, not implemented — a future slice could compute or store "is this the current outcome for this test" (by comparing `run_attempt` against the parent `WorkflowRun.latest_run_attempt`, or by explicit reconciliation) without needing new field names or a schema migration. Retry-pass reconciliation is deferred, explicitly, as future work — not ruled out as impossible.

A future standing query that needs to distinguish "there is an active incident right now" from "this failure was once observed" will need `is_current` (or equivalent) implemented; `ci-corroboration-pattern` itself does not need this distinction — corroborating evidence that a described error signature has occurred in CI is useful regardless of whether that specific test run is still failing today.

---

## Decisions & Alternatives

| Decision | Chosen | Alternatives Considered | Rationale |
|---|---|---|---|
| Unify GitHub event routing before extending it | Migrate poll's issue/PR ingestion onto `IngestEvent`/`run_ingest_event`, *then* add CI event types | CI-only event boundary, added alongside the existing two-pattern inconsistency | See HLD §12 — a third pattern doesn't remove a two-pattern problem |
| `WorkflowAttempt` as a property, not a node | `run_attempt` on every attempt-scoped node (`WorkflowRun.latest_run_attempt`, `WorkflowJob`/`WorkflowJobStep`/`TestExecution`/`TestFailure`'s own `run_attempt`) | A separate `WorkflowAttempt` node between `WorkflowRun` and `WorkflowJob` | Nothing in the CI-corroboration pattern, or any other planned query, needs to traverse "the attempt" as its own hop — it's an attribute of which run/job/test generation produced the data, not a relationship worth its own node and edges |
| Poll's richer PR behavior as the unification target | `run_ingest_event`'s `fix` branch is extended to match `ingest_pr`'s current behavior (IMPLEMENTED_IN, RESOLVED_BY, dependabot) | Unify onto the existing thin `fix` branch, accepting a poll-path regression | HLD constraint: preserve `GithubIngester`'s existing mutations where practical; the webhook path gaining this behavior is an intentional, called-out side effect, not scope creep |
| One shared `ErrorSignatureMatcher` | Extracted from today's ticket-side word-boundary algorithm, made multi-field-aware | Two independent normalizations (ticket text, JUnit text) hoping to coincide | See HLD §13 |
| `TestExecution`/`TestFailure`'s natural key | `(run_id, run_attempt, classname, test_name)` | `(run_id, classname, test_name)` without `run_attempt`; `(run_id, suite_name, test_name)`; a separate synthetic failure ID | `classname` distinguishes same-named tests under different test classes, which `suite_name` alone does not. `run_attempt` additionally ensures a retry produces new nodes rather than overwriting the original attempt's — required for § Historical vs. Current Failure State to hold at all |
| `WorkflowJob`'s natural key includes `run_attempt` | `(run_id, run_attempt, github_job_id)` | `(run_id, github_job_id)` alone, deferring correctness to a Phase 6 verification of GitHub's actual job-ID behavior | Graph correctness should not depend on an unverified assumption about an external system's ID scheme — the composite key is safe whether or not GitHub's job IDs turn out to already be attempt-unique (§ Workflow Job Identity) |
| `TARGETED_COMMIT` as a separate edge from `TESTED_COMMIT` | Two edges, two distinct claims | One edge (`TESTED_COMMIT` alone), written liberally regardless of conclusion | A cancelled/never-started run should retain neutral source association with its commit without falsely asserting the commit was tested — collapsing the two would force a choice between losing provenance and overclaiming test coverage |
| `Investigation` stable per `(project, ticket)`, `InvestigationMilestone` per evidence item | Get-or-create `Investigation` on a fixed key; a new, independently-keyed `InvestigationMilestone` per corroborating `TestFailure` | Fold `workflow_run_id`/`test_failure_id` into `investigation_id` itself, one `Investigation` per corroboration (the design's own first draft) | Found during Phase 2 review not to generalize — MODOK's long-term investigation model needs one accumulating record per ticket, not a proliferating family of single-purpose investigation nodes. See § Investigation and Milestone Model |
| `ci-corroboration-pattern`'s `DistinctId` keyed on `id(tf)` | Key on the `TestFailure`'s own id | Key on `id(ci)` (the `CustomerIssue`), matching the other three patterns' convention | Quine's `DistinctId` mode fires at most once per distinct id, ever — keying on `id(ci)` would mean only the *first* corroborating `TestFailure` for a ticket could ever fire this pattern. Keying on `id(tf)` makes each new corroborating failure its own distinct id, so accumulation is a property of the standing query itself, not something the write-back logic has to work around |

## Open Questions & Future Decisions

### Deferred

1. **Exact artifact-selection config shape** — "configured test-result artifacts" per project (a filename glob? an artifact-name exact match? multiple patterns?). Deferred to Phase 3/6; a project with nothing configured simply skips steps 6–7 of the poll cycle.
2. **JUnit XML dialect variance** — different test runners (pytest, JUnit proper, Jest-with-JUnit-reporter) produce slightly different XML shapes. First-slice scope targets the common subset (testsuite/testcase/failure/error/skipped elements); a project whose runner emits something the parser doesn't recognize gets a logged warning and no `TestExecution` nodes for that run, not an ingestion failure.
3. **Rate limiting for the expanded poll cycle** — fetching workflow runs + jobs + steps + artifacts is meaningfully more API calls per cycle than issues/PRs alone. Existing `GithubIngester` rate-limit handling (`docs/llds/github-ingestion.md § Rate Limiting`) is assumed sufficient for now; revisit if a live project's cycle time approaches the 30-second interval.
4. **Exact `terminal_failure` threshold** — how many `retryable_failure` attempts before a run's expansion is given up on as permanent. A concrete number (and whether it varies by failure cause) is a Phase 6 detail; the state machine (§ Poll Cycle Extension) does not depend on the exact threshold chosen.
5. **GitHub job-ID stability across re-run attempts is still worth confirming, but no longer a correctness risk.** `WorkflowJob`'s key already includes `run_attempt` (§ Workflow Job Identity), so this no longer gates whether the schema is safe. Still worth confirming against GitHub's actual Actions API behavior (`POST /actions/runs/{run_id}/rerun` and `/rerun-failed-jobs`) during Phase 6, purely to know whether the extra key segment is ever load-bearing in practice or always redundant — not to decide whether the design is sound.
6. **Deleted or GitHub-garbage-collected workflow runs are not detected.** If a run is ingested (even partially) and later deleted upstream, MODOK has no mechanism to notice — it simply stops appearing in "changed since cursor" fetches, and its nodes/edges (including any fired `HAS_ERROR`/corroboration evidence) persist indefinitely as if still current. Explicitly out of scope for v1, not an oversight.
7. **Sequencing enforcement between the routing unification and CI ingestion is a phase-gate, not a merge-gate.** This is being built as one coordinated pass through this LID workflow rather than independent, possibly-parallel PRs — the ordering in § Context ("unify first, then extend") is enforced by Phase 5's tests-first discipline (CI-ingestion tests cite the parity-test IDs from the prerequisite as a dependency), not by a separate CI check. If this work is ever split across independent contributors or branches, a real merge gate (parity-test suite green on the target branch before CI-ingestion PRs merge) should be added at that point — not needed for how this is actually being built now.
8. **`latest_outcome`/`superseded_by`/`resolved_at`/`is_current` on `TestFailure` are reserved, not implemented.** § Historical vs. Current Failure State names these so a future slice can add active-incident detection without a schema rename, but none are populated in this slice. A future standing query wanting "is this failure still current" cannot be built until at least one of them is.
9. **Migrating the three pre-existing standing queries onto the `Investigation`+`InvestigationMilestone` model is a deliberate, deferred follow-up, not done here.** `actionable-issue-pattern`, `new-bug-report-pattern`, and `error-flagged-pattern` keep their current one-`Investigation`-per-firing behavior and identity scheme unchanged in this slice. A ticket that has fired one of those *and* later accumulates CI-corroboration milestones will, for now, have both an old-style standalone `Investigation` (or several) and a new-style stable `Investigation` with milestones — two coexisting shapes, not yet unified. This is an accepted, visible transitional state, not an oversight; unifying all four onto one model is future work this design does not preclude.

## References

- `docs/high-level-design.md § Continuous CI Ingestion, § ErrorSignatureMatcher (shared), § Key Design Decisions #12–13`
- `docs/llds/standing-queries.md § Mechanical Anchor Linking, § run_ingest_event — investigation branch, § GitHub Poll Adapter, § Investigation Node` — the routing/anchor-linking/investigation mechanisms this component extends or, for `Investigation`'s identity, deliberately diverges from
- `docs/llds/github-ingestion.md` — `GithubIngester`'s existing issue/PR mutation logic, preserved underneath the new routing
- `docs/llds/webhook-receiver.md § Pull adapter, § IngestEvent` — the protocol and event-model boundary this component now fully participates in
- `docs/llds/quine-client.md` — `write_edge_by_parts`/`node_exists_by_parts`/`replace_edges_by_parts`/`upsert_node` primitives reused for all new node/edge types
