# File Escalation Pattern

## Context and Design Philosophy

See `docs/high-level-design.md § File Escalation Pattern` and Key Design Decision #15. The goal: when three or more customer issues independently identify the same file as a high-confidence debug-packet candidate, and have done so since the file's most recent commit, MODOK escalates mechanically by opening a new GitHub issue linking the contributing tickets — a repeat-offender signal a human would otherwise only notice by re-reading tickets one at a time.

This is the first standing query in this project whose trigger condition is a threshold over accumulating evidence (count ≥ 3) combined with a recency comparison, rather than a simple existential join (`docs/llds/standing-queries.md`'s four existing patterns all fire on "does this path exist in the graph," never on "how many, and since when"). Key Design Decision #15 documents the live-verified reason the design below splits responsibility the way it does: Quine's standing-query *pattern* grammar hard-rejects `WITH` clauses and aggregate functions (confirmed via two live compile errors against `~/.modok/quine.jar` 1.10.0); the identical `WITH`/`count()`/`ORDER BY ... LIMIT 1` combination is fully supported in a pattern's `enrichment_query` (`CypherQuery andThen` stage) — confirmed via a full live end-to-end run. Everything below follows from that split.

**A second live-verification pass (during the Phase 2 edge-case probe) found a structural limitation this design cannot route around: Quine's standing-query pattern grammar does not support binding a relationship (edge) to a variable at all** — `CompileError ... Assigning edges to variables is not yet supported in standing query patterns`, confirmed live. `DistinctId` mode can only key on the `id()`/`strId()` of a *node* bound in the pattern, and `RETURN DISTINCT id(ci) AS id` fires at most once per `ci`, ever — confirmed live: a ticket's *second* `FLAGS` edge (to a different file, written after that ticket's first `FLAGS` edge already fired the pattern once) produced **no delivery at all**. Since no node-based key can distinguish "this ticket's first flag" from "this same ticket's second flag to a different file," a purely node-keyed pattern cannot guarantee every file that crosses the threshold is ever evaluated — a file could accumulate well past 3 qualifying flags and never fire, if the flag that would have crossed its threshold happens to come from a ticket whose *own* `id(ci)` already fired elsewhere. See § Reconciliation Sweep below for how this is closed without abandoning the fast, Quine-native path for the common case.

## Graph Model

Two new node/edge additions, one new node type:

```
CustomerIssue -[:FLAGS]-> File
FileEscalation -[:ESCALATES]-> File
FileEscalation -[:INCLUDES]-> CustomerIssue
```

```python
class FileEscalation(QuineNode):
    node_type: Literal["FileEscalation"]
    project_slug: str
    file_path: str
    since_commit: str          # Commit.sha this escalation window opened after
    github_issue_number: str
    status: str                # "open" — no resolution lifecycle in v1, mirrors Investigation
    created_at: str            # ISO 8601, set at first-qualifying-delivery time
    standing_query_name: str   # "file-escalation-pattern"
```

`idFrom('file-escalation', project_slug, file_path, since_commit)` — the composite key is the idempotency mechanism: a later commit touching the file changes `since_commit`, which addresses a *different* `FileEscalation` node, naturally opening a fresh escalation window rather than requiring any explicit "reset" bookkeeping.

**New field on `CustomerIssue`: `created_at: str`.** `CustomerIssue` currently has no timestamp field at all (`src/modok/quine/models.py:78-87`) — a real, load-bearing gap for this component, since the recency comparison needs *some* per-issue timestamp to compare against `Commit.timestamp`. Set to MODOK's own ingestion wall-clock time (`datetime.now(timezone.utc).strftime(...)`), mirroring the exact precedent `WorkflowRun.created_at`/`InvestigationMilestone.created_at`/`DiagnosticNote.created_at` already establish — MODOK's own write-time stamp, not an attempt to recover the source system's original timestamp (GitHub's issue `created_at` is not currently plumbed through `GithubIngester` at all; adding that plumbing is a separate, larger change out of scope here — see Open Questions). Set once, at each of the three existing `CustomerIssue(...)` construction call sites (`webhook/pipeline.py:44`, `webhook/pipeline.py:69`, `cli/commands/ingest.py:75`) — never updated afterward, since `upsert_node` only ever runs once per `CustomerIssue` today (subsequent writes for the same ticket go through `run_ingest_event`'s `investigation`/`milestone`/new `file_escalation` branches, none of which re-upsert the `CustomerIssue` node itself).

**`FLAGS` scope: `kind == "source"` candidates only.** `ScoredCandidate.kind` is `"source"` or `"test"` (`src/modok/retrieval/models.py:97`). Restricting `FLAGS` to source candidates sidesteps an unresolved addressing question — whether a `"test"`-kind candidate's `path` should resolve to a `File` node (code-map universal) or a `TestFile` node (frontmatter-registered, a distinct node type per `docs/llds/test-coverage-ci-linking.md`) is not settled by anything read during this design pass — and keeps this component consistent with this project's own immediately-preceding work demoting bare test-file signal (`docs/llds/diagnostic-retrieval-engine.md § Test Coverage (Informational)`, `rocketmark/stagehand#31`). A `"test"`-kind candidate reaching `HIGH` confidence already carries real non-coverage evidence (e.g. `recent_test_failure`), so excluding it here is a scope decision, not a claim that such candidates are uninteresting — see Open Questions.

## `FLAGS` Write-Back

Written in `_maybe_notify_github` (`src/modok/webhook/server.py:296`), immediately after `packet = await retrieve(...)` returns (line 407-419) and before `format_debug_packet_markdown` is called — the same point that already has the full `DebugPacket`, the resolved `CustomerIssue` node id, and (per the existing `if source_system != "github": return` guard at the top of the function) already only runs when a GitHub issue exists to eventually escalate against.

**Only the `investigation` branch refreshes `FLAGS`; the `milestone` branch does not — confirmed by reading both directly, not assumed.** `_process_investigation` (fires for `new-bug-report-pattern`/`error-flagged-pattern`/`actionable-issue-pattern`) calls `_maybe_notify_github`, which calls `retrieve()`. `_process_milestone` (fires for `ci-corroboration-pattern`) calls a *different* function, `_maybe_post_ci_corroboration_comment`, which formats a small mechanical template from the milestone's own fields (`error_signature`, `test_failure_id`, workflow info) and never calls `retrieve()` at all — a deliberate design point of `docs/llds/continuous-ci-ingestion.md`'s milestone model (fast, no DRE traversal), not an oversight to fix here. Consequence: `FLAGS` reflects a ticket's high-confidence file set as of its most recent *investigation*-triggering `retrieve()` call — a later CI-corroboration milestone that would have pushed some candidate to `HIGH` confidence does not refresh `FLAGS` for that ticket. Accepted as v1 scope, not fixed: extending the milestone path to also call `retrieve()` would be an invasive change to a different, already-shipped component's deliberately-fast design, made solely to serve this new feature. See Open Questions.

```python
high_confidence_files = [c.path for c in packet.scored_candidates if c.kind == "source" and c.confidence == "high"]
await client.replace_edges_by_parts(
    ("customer-issue", project_slug, source_system, ticket_id),
    "FLAGS",
    [("file", project_slug, path) for path in high_confidence_files],
)
```

**`replace_edges_by_parts`, not additive `write_edge_by_parts` — called unconditionally, even when `high_confidence_files` is empty.** A ticket's high-confidence candidate set can change across re-investigations of the same `CustomerIssue` — reconciling the full current set on each write, rather than accumulating every candidate ever seen across every re-investigation, mirrors the exact reconciliation rationale `link_customer_issue_error_anchors`/`link_customer_issue_feature_anchors` already establish for `HAS_ERROR`/`AFFECTS` (`docs/llds/standing-queries.md § Mechanical Anchor Linking`, step 6). Calling `replace_edges_by_parts` only when the set is non-empty (an earlier draft of this pseudocode did this) would silently defeat that same rationale for the one case it matters most — a re-investigation that *drops* a ticket's last remaining high-confidence file to zero would leave the stale `FLAGS` edge in place forever, since nothing would ever call `replace_edges_by_parts` to clear it. `replace_edges_by_parts` with an empty target list is exactly how `link_customer_issue_error_anchors`/`link_customer_issue_feature_anchors` already handle the equivalent "matched nothing this time" case for `HAS_ERROR`/`AFFECTS`. This is a deliberate scope difference from `FileEscalation -[:INCLUDES]-> CustomerIssue` below, which *is* additive — the two edges answer different questions ("what does this ticket currently point at" vs. "which tickets have ever contributed to this escalation").

**Only `File` nodes that already exist can receive a `FLAGS` edge.** `replace_edges_by_parts`/`write_edge_by_parts` `MATCH` both endpoints before writing (`docs/llds/quine-client.md`) — a `scored_candidates` path with no corresponding `File` node (should not happen in practice, since candidates are themselves derived from graph traversal, but not structurally impossible) silently writes no edge for that path, never inventing one. Consistent with the never-invent-a-node discipline governing every other mechanical write in this project.

## Standing Query

New `src/modok/quine/standing_queries/file_escalation_pattern.yaml`:

```yaml
name: file-escalation-pattern
mode: DistinctId
pattern: |
  MATCH (f)<-[:FLAGS]-(ci) WHERE f.node_type = 'File' AND ci.node_type = 'CustomerIssue'
  RETURN DISTINCT id(ci) AS id
enrichment_query: |
  MATCH (ci) WHERE id(ci) = $that.data.id
  MATCH (ci)-[:FLAGS]->(f) WHERE f.node_type = 'File'
  WITH f
  MATCH (f)<-[:TOUCHES]-(c) WHERE c.node_type = 'Commit'
  WITH f, c ORDER BY c.timestamp DESC LIMIT 1
  MATCH (f)<-[:FLAGS]-(ci2) WHERE ci2.node_type = 'CustomerIssue' AND ci2.created_at > c.timestamp
  WITH f, c, count(distinct ci2) AS n
  WHERE n >= 3
  RETURN f.project_slug AS project_slug, f.repo_path AS file_path, c.sha AS since_commit,
         'file-escalation-pattern' AS standing_query_name
output_name: file-escalation-trigger
```

**Keyed on `id(ci)` — the flagging `CustomerIssue` — not `id(f)`.** The same per-new-evidence keying lesson `ci-corroboration-pattern` already establishes (`docs/llds/standing-queries.md § ci_corroboration_pattern.yaml`): `DistinctId` fires at most once per distinct id, ever. Keying on the stable `File` would mean only the *first* ticket to ever flag a given file could fire this pattern — the 2nd and 3rd tickets, which is exactly what needs to be observed for the threshold to matter, would never re-trigger evaluation for that file. Keying on each new `ci` means the pattern fires once per newly-written `FLAGS` edge, and the enrichment query (below) decides, freshly, each time, whether that firing is actionable.

**Live-verified behavior of this exact shape (Key Design Decision #15):** no delivery after the 1st or 2nd flagging issue on a file (count below threshold); delivery with the correct fields after the 3rd; a further delivery after a 4th, 5th, etc. (confirmed — `DistinctId`'s one-per-id constraint applies to the newly-arriving `ci`, not to how many times the *file* has crossed the threshold, so redelivery past the threshold is expected and handled by the `run_ingest_event` branch below, not suppressed at the Quine level); and, after a new `Commit` lands and three more qualifying issues arrive, a fresh delivery keyed to the new commit with the count correctly restarted from zero — the "since last edit" reset falls out of `ORDER BY c.timestamp DESC LIMIT 1` with no separate reset bookkeeping required.

**No `project_slug` filter**, matching every existing pattern's rationale: `idFrom()` topology already makes a `File`/`CustomerIssue` pair from different projects structurally unable to share a `FLAGS` edge, so a `WHERE` filter would be redundant.

**Enrichment returns only identifying fields**, matching `actionable-issue-pattern`'s established minimum — not the list of contributing issues. Gathering that full list is deferred to an ordinary `client.query()` call in the `run_ingest_event` branch below, the same "enrichment identifies the match; MODOK-side code assembles the full picture" split every existing pattern already uses for its DRE-assembled packet.

## `run_ingest_event` — `file_escalation` branch

New `IngestEvent.kind = "file_escalation"` and `FileEscalationData`:

```python
@dataclass(frozen=True, eq=True)
class FileEscalationData:
    project_slug: str
    file_path: str
    since_commit: str
    standing_query_name: str
```

**Route dispatch** (`POST /standing-query/result`, `docs/llds/standing-queries.md § Standing Query Result Route`): extends the existing payload-shape dispatch. A row containing `milestone_kind` → `MilestoneData` (unchanged); a row containing `since_commit` and no `milestone_kind` → `FileEscalationData` (new); otherwise → `InvestigationData` (unchanged, default). `since_commit` is a field no other row shape produces, so this is unambiguous.

Branch behavior calls a single shared function, `_process_file_escalation(client, project_slug, file_path, since_commit)` — used identically by this branch and by the reconciliation sweep below, so there is exactly one place this logic lives (mirroring `docs/llds/test-coverage-ci-linking.md`'s "both call sites use the same resolution function" precedent):

1. Query the current full set of qualifying issues directly — re-deriving membership rather than trusting only the single `ci` that happened to trigger this specific delivery, since by the time this Python code runs, more qualifying issues may already exist than the one that caused the firing:
   ```cypher
   MATCH (f) WHERE f.node_type = 'File' AND f.project_slug = $p AND f.repo_path = $path
   MATCH (f)<-[:TOUCHES]-(c) WHERE c.node_type = 'Commit' AND c.sha = $since_commit
   MATCH (f)<-[:FLAGS]-(ci) WHERE ci.node_type = 'CustomerIssue' AND ci.created_at > c.timestamp
   RETURN ci.source_system AS source_system, ci.ticket_id AS ticket_id, ci.summary AS summary
   ```
   If fewer than 3 rows come back (possible if a `FLAGS` edge was reconciled away by a later `replace_edges_by_parts` call between the standing query firing and this code running — see `FLAGS` Write-Back above), stop: no escalation, nothing written. The enrichment's own `n >= 3` check already gated the *delivery*; this is a second, authoritative check against current state before any write happens, the same double-check discipline `_process_investigation`'s dedup check applies before every write. A failure in this query itself is caught by the same broad `try/except` covering the rest of this function (point 4 below) — not a distinct failure mode.
2. `node_exists_by_parts(("file-escalation", project_slug, file_path, since_commit))`:
   - **Does not exist**: immediately upsert a placeholder `FileEscalation` node — `status="open"`, `created_at=now`, `github_issue_number=""` — and write `ESCALATES` plus one `INCLUDES` edge per issue found in step 1, **before** calling GitHub. This ordering is deliberate: a second, concurrent call to this same function for the same `(file_path, since_commit)` (two near-simultaneous deliveries, or a standing-query delivery racing the reconciliation sweep) will now see the placeholder node already exists and fall into the "already exists" branch below instead of also attempting to create a GitHub issue — closing the double-issue race the Phase 2 edge-case probe found, in the common case where the two calls aren't *exactly* simultaneous. (A true simultaneous double-check-before-either-writes remains theoretically possible — this is the same residual, accepted risk `Investigation`'s own check-then-write dedup already carries; MODOK is a single-user/trusted-team v1 tool per the HLD, and no lock/mutex primitive exists anywhere in this codebase to close it fully.) Only after the placeholder is written: format and create the GitHub issue (`format_file_escalation_markdown`, `create_issue` — § GitHub Issue Creation below) and, on success, update the node's `github_issue_number`. On failure, `github_issue_number` stays `""` — the node already exists, so a later call (the 4th ticket, a redelivered match, or the next reconciliation sweep) takes the "already exists" branch, sees the empty `github_issue_number`, and retries creation rather than posting an update comment. This is a genuine improvement over `Investigation`'s equivalent gap (`docs/llds/standing-queries.md § run_ingest_event — investigation branch`, point 5, where a first-attempt GitHub failure is never retried) — redelivery past the threshold is expected and common here, so retries fall out naturally rather than needing new machinery.
   - **Already exists, `github_issue_number` is `""`**: issue creation is still pending or previously failed — retry it (same create-and-store step as above), not an update comment.
   - **Already exists, `github_issue_number` is set**: diff the issues found in step 1 against the `FileEscalation`'s existing `INCLUDES` targets (`MATCH (fe)-[:INCLUDES]->(ci) WHERE id(fe) = idFrom('file-escalation', $p, $path, $since_commit) RETURN ci.ticket_id`). For each issue not already linked: write its `INCLUDES` edge and post an update comment (`post_issue_comment`, reused unchanged) naming the newly-added ticket. If the diff is empty (pure redelivery of an already-processed match), no-op — no duplicate comment. **Residual risk, accepted, not fixed**: two concurrent calls can still both compute the same diff before either's `INCLUDES` write is visible to the other, producing two update comments for the same newly-added ticket in the rare case of near-simultaneous deliveries — same category of accepted risk as the create-race above, and bounded in impact (a duplicate comment, never a duplicate issue).
3. `ticket_kind`/`status` of a contributing `CustomerIssue` is **not** checked — a closed or resolved ticket's `FLAGS` edge still counts toward the threshold and stays in `INCLUDES` indefinitely. Deliberate v1 scope, not an oversight: see Open Questions.
4. Any failure anywhere in this function (the re-derivation query, GitHub issue creation, or the update comment) is caught, logged to stderr, and does not raise — same best-effort discipline as `_maybe_notify_github` (`SQ-GH-004`).

### GitHub Issue Creation

New `create_issue(github_repo: str, token: str, title: str, body: str) -> str | None` (`src/modok/ingestion/github.py`, alongside `post_issue_comment`) — `POST /repos/{github_repo}/issues`, same header shape. Unlike `post_issue_comment`, this **returns** the created issue's number (as a string, matching `ticket_id`'s existing type elsewhere) on success, or `None` on any non-2xx response or exception (logged, never raised) — the caller needs the number to persist onto `FileEscalation.github_issue_number`, so this one function in the GitHub write-back family cannot be purely fire-and-forget.

`github_repo`/`GITHUB_TOKEN` resolution mirrors `_maybe_notify_github`'s existing guard exactly (`ModokConfig.load()` → matching `ProjectConfig.github_repo`; `os.environ.get("GITHUB_TOKEN")`) — if either is missing, `_process_file_escalation` logs and returns without attempting creation, same as the sibling `investigation` branch's behavior for a GitHub-sourced ticket with no configured repo/token. Because every contributing `CustomerIssue` here is already known to be `source_system == "github"` (only GitHub-sourced tickets can carry a `FLAGS` edge — see `FLAGS` Write-Back's guard), this is a config gap, not a source-type mismatch, when it happens.

**Title**: `"MODOK: {file_path} flagged by {n} tickets since {since_commit_short}"` (`since_commit_short` = `since_commit[:7]`, matching the SHA-prefix convention `format_debug_packet_markdown` already uses). **Labels**: `["modok-escalation"]` — a single fixed label, new to this component, giving operators a mechanical way to filter these issues without inventing a broader labeling taxonomy. **Repo**: the same `github_repo` configured for the project (`ProjectConfig.github_repo`) — there is only one repo in scope, since every contributing `CustomerIssue` and the escalated `File` both belong to the same `project_slug`.

## Reconciliation Sweep (Correctness Backstop)

**Why this exists, precisely**: the live-verified pattern limitation above (§ Context and Design Philosophy) means the standing query is a *best-effort fast path*, not a completeness guarantee — a file can cross the threshold and never receive a delivery, if the qualifying flag that crossed it came from a ticket whose `id(ci)` already fired on a different file. `reconcile_file_escalations(client, project_slug)` (`src/modok/ingestion/ci_ingestion.py`, alongside the three existing sweeps) closes this every poll cycle, mirroring `reconcile_commit_edges`/`reconcile_dependency_change_edges`/`reconcile_test_execution_links` exactly:

```cypher
MATCH (f) WHERE f.node_type = 'File' AND f.project_slug = $p
MATCH (f)<-[:FLAGS]-(any_ci) WHERE any_ci.node_type = 'CustomerIssue'
WITH DISTINCT f
MATCH (f)<-[:TOUCHES]-(c) WHERE c.node_type = 'Commit'
WITH f, c ORDER BY c.timestamp DESC LIMIT 1
MATCH (f)<-[:FLAGS]-(ci) WHERE ci.node_type = 'CustomerIssue' AND ci.created_at > c.timestamp
WITH f, c, count(distinct ci) AS n
WHERE n >= 3
RETURN f.repo_path AS file_path, c.sha AS since_commit
```

This is an ordinary `client.query()` call (not a standing-query enrichment), so the `WITH`/aggregation restriction that applies to standing-query patterns and enrichment stages does not apply here — the same unrestricted Cypher already confirmed working for every other MODOK read path. For each `(file_path, since_commit)` row returned, call the same `_process_file_escalation` used by the `run_ingest_event` branch above — the sweep's own idempotency comes entirely from that shared function's `node_exists_by_parts` check, not from anything sweep-specific.

**`_process_file_escalation` lives in `src/modok/webhook/server.py`, alongside `_process_investigation`/`_process_milestone`** — the established location for this project's `run_ingest_event`-dispatched processing functions. `reconcile_file_escalations` (in `src/modok/ingestion/ci_ingestion.py`) imports it via a **lazy, function-body import**, not a module-level one. This is not a style preference: the real import graph is `server.py → router.py → github_poll.py → ci_ingestion.py` (`server.py` imports `PULL_ADAPTERS`/`PUSH_ADAPTERS` from `router.py`, which registers `GitHubPollAdapter` from `github_poll.py`, which imports CI-ingestion functions from `ci_ingestion.py`) — a module-level `ci_ingestion.py → server.py` import would close that into a genuine cycle. `src/modok/webhook/pipeline.py` already solves the identical problem for `_process_investigation`/`_process_milestone` (see its own module docstring) using this exact technique; `reconcile_file_escalations` follows the same precedent rather than inventing a new one.

**Runs every poll cycle, isolated in its own `try`/`except`**, wired into `_run_ci_ingestion_cycle` (`src/modok/webhook/adapters/github_poll.py`) alongside `reconcile_test_execution_links` — a failure here must not block issue/PR sync, CI ingestion, or dependency ingestion in the same cycle, same discipline as every sibling sweep.

**Cost is bounded, unlike the sweeps this pattern is modeled on.** `reconcile_test_execution_links`'s sweep re-attempts *every* unresolved `TestExecution` every cycle, forever, because some are structurally unresolvable (non-pytest classnames). This sweep has no equivalent unbounded-cost case: the `MATCH ... WHERE n >= 3` filter means the query only does real work proportional to files that currently have *any* `FLAGS` edges (expected to be small — high-confidence candidates are the exception, not the norm, across all tickets), and a file that already has an open `FileEscalation` for its current `since_commit` is filtered out by `_process_file_escalation`'s own idempotency check on the very next line after being found, not by the sweep query itself — so a long-lived, already-escalated file still costs one cheap re-derivation query per cycle, not nothing, but not unbounded growth either.

## Failure Handling

| Condition | Behavior |
|---|---|
| Fewer than 3 currently-qualifying issues found in `_process_file_escalation`'s own re-check | No write at all — treated as a stale/superseded delivery, not an error |
| `github_repo` unconfigured or `GITHUB_TOKEN` unset when a `FileEscalation` placeholder is ready to create/retry its GitHub issue | Logged, non-fatal; `github_issue_number` stays `""`, retried on the next qualifying delivery or sweep cycle — same shape as any other creation failure |
| GitHub issue creation fails (config present, API/network error) | `github_issue_number` stays `""` on the already-written placeholder node; retried automatically on the next qualifying delivery or sweep cycle |
| GitHub update comment fails on a later qualifying delivery | Logged, non-fatal; the `INCLUDES` edge for that issue is still written — the graph record does not depend on the comment succeeding, matching `Investigation`'s existing authority-model precedent |
| A `File` referenced by `scored_candidates` doesn't (yet) exist as a graph node | No `FLAGS` edge written for that path; not an error, not logged distinctly (mirrors `replace_edges_by_parts`'s existing silent-skip behavior for nonexistent targets elsewhere in this project) |
| Standing query enrichment itself fails or Quine is unreachable | No delivery; the reconciliation sweep (§ above) still catches the file on its next cycle — no permanent gap |
| A `File` has no `Commit -[:TOUCHES]->` edge at all (never committed, or commit history not ingested) | Both the enrichment query and the reconciliation sweep's `MATCH (f)<-[:TOUCHES]-(c)` return zero rows for that file — it can never cross the threshold, indefinitely. Not an error; see Testable Non-Goals |
| A qualifying `CustomerIssue.created_at` is on the same second as, or skewed relative to, the file's `Commit.timestamp` | Strict `>` comparison, no tie-break; a same-second or skew-affected issue silently does not count toward the threshold. No error surfaced — see Open Questions |

## Testable Non-Goals

- No escalation for `kind == "test"` candidates in v1 — deferred, see Open Questions.
- No resolution lifecycle for `FileEscalation.status` — v1 only ever writes `"open"`, mirroring `Investigation.status`'s identical v1 scope.
- No retroactive re-evaluation if a `FLAGS` edge is reconciled away after an escalation already fired — the escalation issue, once created, is not closed or edited to remove a ticket.
- No cross-file or cross-escalation aggregation (idea #2, the common-root parent investigation, is separate work, explicitly sequenced after this one).
- GitHub `CustomerIssue.created_at` (the source system's original report time) is not plumbed through — `created_at` is MODOK's own ingestion timestamp, not GitHub's.
- A `File` with no `Commit -[:TOUCHES]->` edge at all never crosses the threshold — no fallback "since project start" behavior for files with no ingested commit history.
- A ticket's `status`/`ticket_kind` is never checked — a closed, resolved, or wontfix ticket's `FLAGS` edge counts toward the threshold and stays in `INCLUDES` exactly like an open one.
- No cap on how many files a single ticket's `FLAGS` write-back can target — every `kind == "source"`/`confidence == "high"` candidate gets a `FLAGS` edge, however many that is for a given packet. Not considered a gaming risk in v1: the threshold counts *distinct tickets* per file, so one ticket flagging many files never inflates any single file's count by more than 1.
- If a `File`'s `repo_path` is later changed (rename, via unrelated code-map re-extraction), an already-open `FileEscalation`'s `_process_file_escalation` re-derivation query (keyed on the old `file_path` string) stops matching that file going forward — the escalation can no longer accumulate further `INCLUDES` edges or update comments, silently. Same class of gap `docs/llds/test-coverage-ci-linking.md § Open Questions` already accepts for `EXECUTES` edges after a `TestFile` rename.

## Decisions & Alternatives

| Decision | Chosen | Alternatives Considered | Rationale |
|---|---|---|---|
| Aggregation location for the fast path | Standing-query `enrichment_query` (`CypherQuery andThen`) | Pure pattern aggregation | Live-verified: pure-pattern aggregation is not possible (`WITH`/`count()` hard-rejected by the pattern compiler); the enrichment stage is confirmed to support the needed Cypher, so the fast path's threshold decision stays inside Quine rather than moving to Python. (A reconciliation sweep is still added, but as a correctness backstop for a separate, narrower gap — see the next row — not as the primary aggregation mechanism.) |
| Pattern keying | `id(ci)` — the newly-arriving flagging `CustomerIssue`, **accepted as best-effort only**, backed by the reconciliation sweep for correctness | `id(f)` — the target `File`; `id(r)` on the `FLAGS` relationship itself | `id(f)` was rejected first: keying on the stable file means only the first-ever flagging ticket could fire the pattern (the exact bug `ci-corroboration-pattern` already found and fixed for a structurally identical reason). `id(r)` was tried next, specifically to fix the *remaining* gap (a ticket's 2nd+ flag never re-firing) — live-rejected outright: Quine's standing-query pattern grammar does not support binding an edge to a variable at all. With no node-based key able to guarantee per-edge novelty, `id(ci)` is kept as the fast-path key and the reconciliation sweep is added as the correctness guarantee the pattern itself cannot provide |
| Coverage guarantee for the pattern's known gap | A poll-cycle reconciliation sweep (`reconcile_file_escalations`), reusing the same shared processing function as the standing-query branch | Accept the gap as a documented limitation; move the entire count/threshold decision to a poll sweep and drop the standing query | Accepting the gap outright would silently violate the HLD's own falsifiable success metric ("exactly one new escalation issue" — a missed file never gets one at all). Dropping the standing query entirely would work but abandons the near-instant detection this project's standing-query architecture is built around for the common case, and contradicts HLD Key Design Decision #10 more directly than a sweep used only as a backstop for a live-verified, narrow gap |
| `FileEscalation` write ordering on first creation | Write the placeholder node (empty `github_issue_number`) *before* calling GitHub, update after | Call GitHub first, write the node only after a successful response | The Phase 2 edge-case probe found the reverse ordering (GitHub-first) can orphan a real GitHub issue if the process crashes between the GitHub call succeeding and the node write — the next qualifying delivery would find no node and create a duplicate issue. Placeholder-first means the idempotency check is live from the earliest possible moment, at the cost of a `FileEscalation` node that can transiently exist with no real issue behind it (handled explicitly — see `run_ingest_event` branch, point 2) |
| `CustomerIssue.created_at` semantics | MODOK's own ingestion wall-clock time | GitHub's original issue `created_at`; no timestamp, use `Investigation.triggered_at` instead | Matches existing precedent (`WorkflowRun`/`InvestigationMilestone`/`DiagnosticNote` all use MODOK write-time, not source-system time) and avoids new GitHub-payload plumbing; the `Investigation.triggered_at` alternative was rejected because a `CustomerIssue` can have multiple `Investigation`s (`docs/llds/standing-queries.md`), making "which one's timestamp" ambiguous, and adds a traversal hop for no benefit over a direct field |
| `FLAGS` edge write discipline | `replace_edges_by_parts` (reconcile full current set) | Additive `write_edge_by_parts` (accumulate every candidate ever seen) | Mirrors the existing `HAS_ERROR`/`AFFECTS` reconciliation rationale — a ticket's high-confidence set can change across re-investigations, and an edit/re-evaluation removing a stale flag should actually remove it, not accumulate forever |
| `FileEscalation -[:INCLUDES]-> CustomerIssue` edge write discipline | Additive `write_edge_by_parts`, one per newly-discovered contributing issue | `replace_edges_by_parts`, full set each time | This edge answers "which tickets have ever contributed to this escalation," a historical record that should only grow — unlike `FLAGS`, there is no legitimate case where a ticket that already contributed should later be un-contributed |
| Escalation scope | `kind == "source"` candidates only | Include `kind == "test"` candidates too | Sidesteps an unresolved `File`-vs-`TestFile` addressing question for test-kind candidates, and stays consistent with this project's own recent work demoting bare test-file signal (`rocketmark/stagehand#31`) — deferred, not ruled out permanently |
| Idempotency key | `(file_path, since_commit)` composite `idFrom` | A single `FileEscalation` per file, updated in place across commits | A composite key keyed to the *current* triggering commit means a later commit opens a genuinely new window with its own node and its own GitHub issue, rather than silently reopening or overwriting a prior (possibly already-resolved-by-the-team) escalation — matches the "since last edit" framing literally, not just approximately |

## Open Questions & Future Decisions

### Deferred

1. **`kind == "test"` candidates** — excluded from `FLAGS` in v1 pending a decision on whether test-file paths in `scored_candidates` should resolve against `File` or `TestFile` nodes; not investigated in this pass.
2. **GitHub's original issue `created_at`** — not plumbed through `GithubIngester`; `CustomerIssue.created_at` is MODOK's ingestion time, which can diverge meaningfully from report time for backfilled/batch-imported tickets (an old ticket imported today would look "recent" relative to a file's last commit, which could inflate an escalation count incorrectly for a batch-imported project). Not expected to matter for the live-polling common case (issues are typically ingested within one 30-second poll cycle of being reported); worth revisiting if MODOK is ever used against a large historical backfill.
3. **`FileEscalation.status` lifecycle** — no transition to `"resolved"`/`"stale"` in v1, same deferred scope as `Investigation.status`.
4. **Retroactive re-evaluation on `FLAGS` reconciliation** — if a ticket's high-confidence file set changes after an escalation already fired for one of its (now-dropped) files, the existing `FileEscalation`/GitHub issue is not revisited, edited, or closed.
5. **Idea #2 (common-root parent investigation)** — explicitly the next, separate LID arrow per the user's stated sequencing; not designed here.
6. **Ticket `status`/`ticket_kind` never filtered** — a closed/resolved ticket's `FLAGS` edge counts toward the threshold and stays in `INCLUDES` forever. Accepted for v1: filtering would need a decision about whether to check status at flag-write time, at count time, or both, and whether a ticket closing *after* contributing should retroactively remove its `INCLUDES` edge — none of which has a clear answer yet, and an escalation driven partly by since-closed tickets is still a real repeat-offender signal a human should probably see once, not obviously wrong.
7. **`ci.created_at > c.timestamp`'s strict comparison has no tie-break or skew handling** — a same-second issue/commit pair, or any clock skew between MODOK's ingestion wall-clock and git's own commit timestamps, silently excludes a qualifying issue with no error surfaced. Not expected to matter in practice (commit timestamps and ticket-report times are rarely seconds apart), but undemonstrated.
8. **Multi-row enrichment delivery fan-out is inherited, not independently confirmed for this pattern.** If a single `ci` firing crosses the threshold for two different files in the same enrichment evaluation (possible: the enrichment's `MATCH (ci)-[:FLAGS]->(f)` traverses every file that `ci` flags, not just the one specific to this firing), `docs/llds/standing-queries.md § Standing Query Result Route`'s existing array-body handling should fan this out into two independent `_process_file_escalation` calls via the generic per-row `run_ingest_event` loop — this relies on the route's existing generic behavior, not anything new in this component, and was not separately exercised live for this specific pattern.
9. **`FLAGS` is not refreshed by a CI-corroboration milestone.** Only `_process_investigation` (the `new-bug-report-pattern`/`error-flagged-pattern`/`actionable-issue-pattern` path) calls `retrieve()` and thus refreshes a ticket's `FLAGS` edges; `_process_milestone` (the `ci-corroboration-pattern` path) posts its own lightweight comment and never calls `retrieve()`. A ticket whose confidence on some file only reaches `HIGH` *after* a later CI-corroboration milestone will not get a `FLAGS` edge for that file unless some other event re-triggers an investigation for it. Found during the Phase 4 cross-segment audit — confirmed by reading `_process_milestone`/`_maybe_post_ci_corroboration_comment` directly, not assumed. Not fixed here: doing so would mean extending the milestone path to call `retrieve()`, an invasive change to a different, already-shipped component's deliberately fast, DRE-traversal-free design.

## References

- `docs/high-level-design.md § File Escalation Pattern, Key Design Decision #15` — why this exists, and the live-verified Quine aggregation findings this design depends on
- `docs/llds/standing-queries.md` — `DistinctId` semantics, the `ci-corroboration-pattern` per-new-evidence keying precedent, `Investigation` node-exists-first idempotency, `run_ingest_event`/`POST /standing-query/result` dispatch conventions this component extends
- `docs/llds/diagnostic-retrieval-engine.md § Test Coverage (Informational)` — the `rocketmark/stagehand#31` context motivating the `kind == "source"`-only scope decision
- `docs/llds/test-coverage-ci-linking.md` — `File` vs `TestFile` node-type distinction this component's `kind == "test"` deferral is about
- `docs/llds/quine-client.md` — `replace_edges_by_parts`, `write_edge_by_parts`, `node_exists_by_parts` primitives reused here unchanged
