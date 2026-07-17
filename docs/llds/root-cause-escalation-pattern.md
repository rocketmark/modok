# Root-Cause Escalation Pattern

## Context and Design Philosophy

See `docs/high-level-design.md § Root-Cause Escalation Pattern` and Key Design Decision #16. The goal: when three or more currently-open customer issues independently affect the same feature, MODOK groups them under a single parent GitHub issue so research accumulates in one place, mirroring `docs/llds/file-escalation-pattern.md`'s shape (pattern-detects-existence, enrichment/Python-decides, reconciliation-sweep backstop) but grouping by `Feature` instead of `File`, and — the one genuinely new piece — resetting the accumulation window when a human closes the parent issue rather than when a new commit lands.

**`CustomerIssue.status` freshness, confirmed during the Phase 4 cross-segment check, not assumed.** `GithubIngester.run()` fetches issues with `state=all` and an incremental `since=` window (`src/modok/ingestion/github.py`) — a ticket closed on GitHub is re-fetched (closing is an "update") on the next poll cycle and re-ingested through the same `customer_issue` branch, which fully re-`upsert_node`s the `CustomerIssue` (including a corrected `status`) and re-runs anchor linking. This component depends on `status` staying live without adding any new polling of its own — confirmed by reading the fetch parameters directly, the same way `docs/llds/file-escalation-pattern.md`'s Phase 4 audit confirmed (and corrected) an assumption about the milestone path.

**No new write-back edge.** `CustomerIssue -[:AFFECTS]-> Feature` is already written by mechanical/LLM anchor linking on every ticket (`docs/llds/standing-queries.md § Mechanical Anchor Linking`) — this component reads that edge, it never writes it.

**Threshold and exclusion logic live entirely in Python (`_process_root_cause_escalation`), not in the standing query's enrichment.** File Escalation's enrichment did its own `count(distinct ...)`/`ORDER BY ... LIMIT 1` aggregation, proven safe by live verification. This component's per-feature logic is materially more complex — it must also exclude tickets already linked to *any* prior `RootCauseEscalation` for the feature (open or closed) and check a *different* live system (GitHub's issue state) before deciding whether to append or open a new escalation. Given this project's repeated experience that every new Cypher shape needs live verification before it can be trusted (Key Design Decision #15's two rejections), keeping the enrichment to the same trivial 2-hop shape `error-flagged-pattern` already proves safe, and pushing all decision logic into ordinary, unrestricted Python-side Cypher (`client.query()`, the same unconstrained path the reconciliation sweep already uses), is the lower-risk choice — consistent with, not a departure from, the precedent Key Design Decision #15 established.

---

## Graph Model

New node type, two new edges:

```
RootCauseEscalation -[:ESCALATES]-> Feature
RootCauseEscalation -[:INCLUDES]-> CustomerIssue
```

```python
class RootCauseEscalation(QuineNode):
    node_type: Literal["RootCauseEscalation"]
    project_slug: str
    feature_slug: str
    sequence: int              # 1st, 2nd, 3rd... escalation ever opened for this feature
    github_issue_number: str
    status: str                # "open" — decorative only, see below; never read for the
                                # open/closed decision
    created_at: str
    standing_query_name: str
```

`idFrom('root-cause-escalation', project_slug, feature_slug, sequence)` — `sequence` is the reset mechanism: closing escalation *N*'s GitHub issue lets escalation *N+1* open for the same feature, addressed at a distinct address rather than reusing or mutating the closed one.

**`status` is not authoritative for open/closed — GitHub is.** Unlike `FileEscalation` (where "does this escalation already exist" is the only state that matters), this component must know whether the escalation's issue is *currently open on GitHub*, which can change without MODOK ever writing to Quine (a human closing the issue). `status` stays `"open"` for the lifetime of the node — checking it would silently answer the wrong question. Every decision that depends on open/closed state calls the GitHub API directly (§ below); the property exists only for schema parity with `FileEscalation` and possible future diagnostic display, never as a decision input.

---

## `_process_root_cause_escalation`

`_process_root_cause_escalation(client: Any, project_slug: str, feature_slug: str) -> int` — the single function used by both the `run_ingest_event` branch and the reconciliation sweep, mirroring `_process_file_escalation`'s role exactly.

1. **Qualifying tickets**, two ordinary queries (Python-side set difference, not a Cypher `NOT EXISTS` subquery — that shape is unverified against this project's Quine version and unnecessary when a second query and a set difference accomplish the same thing with zero new risk):
   ```cypher
   -- currently-open tickets affecting this feature
   MATCH (feat) WHERE feat.node_type = 'Feature' AND feat.project_slug = $p AND feat.feature_slug = $f
   MATCH (feat)<-[:AFFECTS]-(ci) WHERE ci.node_type = 'CustomerIssue' AND ci.status = 'open'
   RETURN ci.source_system AS source_system, ci.ticket_id AS ticket_id, ci.summary AS summary
   ```
   ```cypher
   -- tickets already linked to a SUCCESSFULLY-CREATED escalation (open or closed) for this feature
   MATCH (rce) WHERE rce.node_type = 'RootCauseEscalation' AND rce.project_slug = $p
     AND rce.feature_slug = $f AND rce.github_issue_number <> ''
   MATCH (rce)-[:INCLUDES]->(ci) WHERE ci.node_type = 'CustomerIssue'
   RETURN ci.ticket_id AS ticket_id
   ```
   `qualifying` = rows from the first query whose `ticket_id` is not in the second query's result set. WHEN `len(qualifying) < 3`: return `0`, nothing written.

   **`github_issue_number <> ''` in the exclusion query is load-bearing, found during the Phase 2 edge-case probe.** An earlier version excluded any `INCLUDES`-linked ticket regardless of whether the escalation's issue was ever actually created. That silently broke the retry path it was meant to support: a ticket written to `INCLUDES` during a *failed* creation attempt would be excluded from every future `qualifying` computation, so `len(qualifying) < 3` would keep returning early using only brand-new tickets — the pending escalation's own stuck tickets could never resurface to trigger a retry, and the "the qualifying set may have grown" rationale for `_create_or_retry_root_cause_escalation` being unconditional (below) would never actually get exercised. Scoping exclusion to escalations that successfully got a real GitHub issue number means a failed attempt's tickets naturally reappear in `qualifying` next time, correctly reunited with any newly-opened tickets, with no separate "resume a pending attempt" logic needed.
2. **Find the latest escalation for this feature** (ordinary `ORDER BY`/`LIMIT`, already proven safe as a one-shot query — Key Design Decision #15's Test A):
   ```cypher
   MATCH (rce) WHERE rce.node_type = 'RootCauseEscalation' AND rce.project_slug = $p AND rce.feature_slug = $f
   RETURN rce.sequence AS sequence, rce.github_issue_number AS github_issue_number
   ORDER BY rce.sequence DESC LIMIT 1
   ```
   - **No rows**: this feature has never had an escalation. Call `_create_or_retry_root_cause_escalation(..., sequence=1, qualifying)` (§ below). Return `1`.
   - **`github_issue_number == ""`**: the latest escalation's issue creation is still pending or previously failed (§ `_create_or_retry_root_cause_escalation`'s idempotency). Retry at the *same* `sequence` — do **not** increment, since no issue was ever successfully opened for it. Return `1`.
   - **Otherwise**: resolve `github_repo`/`GITHUB_TOKEN` (same helper `_resolve_github_repo_and_token` `file-escalation-pattern.md` already introduces). If unavailable, log and return `0`. Call `get_issue_state(github_repo, token, github_issue_number)` (new function, § GitHub Issue State below).
     - **`None`** (API failure): log and return `0` — do not guess; retried on the next delivery or sweep cycle, same as any other transient failure in this project.
     - **`"open"`**: append. Every entry in `qualifying` is, by construction of step 1's exclusion query, *not yet* linked to a successfully-created escalation — write an `INCLUDES` edge and post an update comment (`post_issue_comment`, reused unchanged) for each. Return `1`. **No separate diff step is needed here**, unlike `FileEscalation` — the exclusion already happened in step 1. **Two accepted residual risks, found during the Phase 2 edge-case probe, neither fixed for the same reasons `FileEscalation` accepts its analogous ones**: (a) no re-check after this read — if a human closes the issue in the window between this `get_issue_state` call and the writes below, MODOK can append a ticket and comment on an issue that's already closed by the time the write lands; (b) two near-simultaneous callers reaching this branch for the same feature can both compute the same `qualifying` set and both post the same ticket's update comment before either's `INCLUDES` write is visible to the other, producing a duplicate comment (never a duplicate issue — the loop only ever appends to an already-real `github_issue_number`).
     - **`"closed"`**: this window is done. Call `_create_or_retry_root_cause_escalation(..., sequence=latest_sequence + 1, qualifying)`. Return `1`.
3. Any exception anywhere in this function is caught, logged to stderr, and does not propagate — same best-effort discipline as `_process_file_escalation`. **A failure partway through the append branch's per-ticket loop aborts the remaining tickets in that batch for this cycle** (no per-ticket exception isolation) — inherited from, not a new gap relative to, `_process_file_escalation`'s structurally identical append loop; a later delivery or sweep cycle picks up whatever remains unprocessed, since nothing already written is lost or needs undoing.

### `_create_or_retry_root_cause_escalation`

```python
async def _create_or_retry_root_cause_escalation(
    client: Any, project_slug: str, feature_slug: str, sequence: int,
    qualifying: list[tuple[str, str, str]],
) -> None: ...
```

**Node/edge writes are unconditional; the external `create_issue` call is separately, narrowly guarded — found during the Phase 2 edge-case probe to need splitting, not one shared gate.** An earlier version gated the *entire* sequence (including `create_issue`) on `not exists`, mirroring `_process_file_escalation`. That reintroduced the double-issue race `FileEscalation`'s gate exists to narrow: with the exclusion-query fix above, `qualifying` on a retry can legitimately be a superset of what was written the first time, so *something* has to run unconditionally to pick up the newly-qualifying tickets — but making the whole function (including the GitHub call) unconditional means two near-simultaneous callers can both pass the `github_issue_number == ""` check upstream and both call `create_issue`, each creating a real issue with only one number surviving the final `SET` (last write wins), permanently orphaning the other. The fix: separate the *idempotent* graph writes (safe to always repeat) from the *external, non-idempotent* GitHub call (re-checked immediately before it fires, narrowing the race window as close to the actual action as `FileEscalation`'s placeholder-first ordering does for its own creation race — not eliminating it; the same theoretical simultaneous-read residual risk applies, and is accepted for the same reason: no lock/mutex primitive exists anywhere in this codebase).

1. `node_exists_by_parts(rce_parts)`. WHEN `False`: upsert the `RootCauseEscalation` node (`status="open"`, `github_issue_number=""`) and write `ESCALATES` to the `Feature`. WHEN `True`: skip both — the node and its `ESCALATES` edge never need rewriting once created.
2. Write `INCLUDES` to every `CustomerIssue` in `qualifying`, **unconditionally, regardless of step 1's outcome** (idempotent — already-present edges are harmless re-writes; this is what correctly picks up tickets that became qualifying after the node's first creation).
3. **Immediately before any GitHub call**, re-fetch the node's *current* `github_issue_number` via a direct property query (not `node_exists_by_parts`, which only confirms existence). WHEN non-empty: a concurrent caller already created (or is in the process of creating) the issue — return without calling `create_issue` again. This is the actual race guard; steps 1–2 above are idempotent and need none.
4. Resolve `github_repo`/`GITHUB_TOKEN`; if unavailable, return (leaves `github_issue_number` empty, retried later).
5. Format title/body (§ GitHub Issue Creation) and call `create_issue(..., labels=["modok-root-cause"])`. On success, `SET` the node's `github_issue_number` via the same targeted-property-update technique `_create_file_escalation_issue` already uses (`client.query()` with `idFrom()` embedded, not a full `upsert_node` re-write).

---

## GitHub Issue State

New `get_issue_state(github_repo: str, token: str, issue_number: str) -> str | None` (`src/modok/ingestion/github.py`, alongside `create_issue`/`post_issue_comment`) — `GET /repos/{github_repo}/issues/{issue_number}`, same header shape as the other two. Returns the issue's `state` field (`"open"` or `"closed"`, GitHub's own values, passed through unchanged — no MODOK-side remapping) on a 2xx response. Never raises.

**A `404` is distinguished from every other failure, found during the Phase 2 edge-case probe.** Treating every non-2xx/exception identically as `None` (transient, "retry later") is correct for a network error or a `5xx`, but wrong for a `404` — an issue that was deleted or transferred out of the repo will never return `2xx` again, so treating it as transient would retry forever and permanently block a new escalation from ever opening for that feature (§ Failure Handling's `None`-handling explicitly never guesses "closed," which is exactly wrong here). A `404` specifically returns `"closed"`: functionally, an escalation whose issue no longer exists can no longer be appended to, which is the same actionable consequence as a human closing it — treating the two identically is the correct, not merely convenient, interpretation. Every other non-2xx response or exception (network error, `5xx`, malformed response) still returns `None`.

---

## Standing Query

New `src/modok/quine/standing_queries/root_cause_escalation_pattern.yaml`:

```yaml
name: root-cause-escalation-pattern
mode: DistinctId
pattern: |
  MATCH (feat)<-[:AFFECTS]-(ci) WHERE feat.node_type = 'Feature' AND ci.node_type = 'CustomerIssue'
  RETURN DISTINCT id(ci) AS id
enrichment_query: |
  MATCH (ci) WHERE id(ci) = $that.data.id
  MATCH (ci)-[:AFFECTS]->(feat) WHERE feat.node_type = 'Feature'
  RETURN feat.project_slug AS project_slug, feat.feature_slug AS feature_slug,
         'root-cause-escalation-pattern' AS standing_query_name
output_name: root-cause-escalation-trigger
```

Deliberately minimal — no aggregation, no threshold check, matching `error-flagged-pattern`'s already-proven 2-hop shape rather than reaching for the more complex enrichment `file-escalation-pattern.md` uses. **Keyed on `id(ci)`, not `id(feat)`**, for the identical reason `file-escalation-pattern` is: `DistinctId` fires at most once per id, ever, and keying on the stable `Feature` would block a 2nd/3rd ticket from ever re-triggering evaluation. Inherits the same known gap (Key Design Decision #15): a ticket's *second* `AFFECTS` edge, to a different feature, can fail to re-fire this pattern — closed by the reconciliation sweep below, not by the pattern itself.

Every delivery — regardless of whether the feature actually has 3+ qualifying tickets yet — reaches `_process_root_cause_escalation`, which returns `0` immediately if the threshold isn't met (step 1 above). This is safe and cheap: two lightweight queries per delivery, no writes, for the common case of a ticket whose feature isn't (yet) escalation-worthy.

---

## `run_ingest_event` — `root_cause_escalation` branch

New `IngestEvent.kind = "root_cause_escalation"` and `RootCauseEscalationData`:

```python
@dataclass(frozen=True, eq=True)
class RootCauseEscalationData:
    project_slug: str
    feature_slug: str
    standing_query_name: str
```

**Route dispatch** extends `_standing_query_row_to_event_data` (`src/modok/webhook/server.py`) again: a row containing `feature_slug` and no `since_commit`/`milestone_kind` → `RootCauseEscalationData`. Distinguishing field: `feature_slug` is unique to this shape (neither `file-escalation-pattern` nor the other three existing patterns ever return it).

Branch: `assert isinstance(event.data, RootCauseEscalationData)`; lazy-import `_process_root_cause_escalation` from `modok.webhook.server` (same reasoning as `_process_file_escalation`'s import in `pipeline.py`); call with `(quine_client, event.data.project_slug, event.data.feature_slug)`.

---

## Reconciliation Sweep

`reconcile_root_cause_escalations(client: Any, project_slug: str) -> None` (`src/modok/ingestion/ci_ingestion.py`, alongside `reconcile_file_escalations`), wired into `_run_ci_ingestion_cycle` (`src/modok/webhook/adapters/github_poll.py`) in its own `try`/`except`, same as every sibling sweep.

```cypher
MATCH (feat) WHERE feat.node_type = 'Feature' AND feat.project_slug = $p
MATCH (feat)<-[:AFFECTS]-(ci) WHERE ci.node_type = 'CustomerIssue' AND ci.status = 'open'
WITH DISTINCT feat
RETURN feat.feature_slug AS feature_slug
```

A loose prefilter — any feature with *at least one* currently-open, affecting ticket, not necessarily 3 unlinked ones. `_process_root_cause_escalation`'s own step 1 is the authoritative check; features that don't actually qualify simply return `0` when visited. Lazy-imports `_process_root_cause_escalation` from `modok.webhook.server`, for the identical import-cycle reason `reconcile_file_escalations` does (`docs/llds/file-escalation-pattern.md § Standing Query`).

**Cost, found during the Phase 2 edge-case probe to need stating explicitly, unlike `FileEscalation`'s tighter prefilter.** `FileEscalation`'s sweep only re-derives per file with *any* `FLAGS` edge — expected to be a small set, since high-confidence candidates are the exception across all tickets. This sweep's prefilter is looser by design (any open, affecting ticket, not a threshold check), so a project with many features each carrying 1–2 open tickets that never reach 3 pays two lightweight read-only queries per such feature, every cycle, for no eventual payoff. Accepted for v1: still bounded by "number of features with any current open-ticket activity," not unbounded, and both queries are read-only with no writes — not measured against a specific feature count where this becomes non-negligible; revisit if observed.

---

## GitHub Issue Creation

`format_root_cause_escalation_title(feature_slug: str, n: int, sequence: int) -> str`: `"MODOK: {feature_slug} has {n} open tickets in progress"` (no sequence number in the title text itself — `sequence` disambiguates the graph node, not the human-facing title; a feature's *current* escalation is always "the open one," and a closed prior one is self-evidently closed by its own GitHub state, so repeating "attempt #2" in the title adds noise without adding information).

`format_root_cause_escalation_markdown(feature_slug: str, issues: list[tuple[str, str, str]]) -> str`: same shape as `format_file_escalation_markdown` — header, one-line summary, a `**Related tickets:**` list of `{source_system}#{ticket_id}: {summary}`.

`format_root_cause_escalation_update_markdown(source_system: str, ticket_id: str, summary: str) -> str`: `"Additional ticket affecting this feature: {source_system}#{ticket_id} — {summary}"`.

**Labels**: `["modok-root-cause"]` — distinct from `FileEscalation`'s `"modok-escalation"`, so the two escalation families stay independently filterable on GitHub.

---

## Failure Handling

| Condition | Behavior |
|---|---|
| Fewer than 3 currently-open, not-yet-linked qualifying tickets | No write at all |
| `github_repo`/`GITHUB_TOKEN` unavailable | Logged, non-fatal; retried on the next delivery or sweep cycle |
| `get_issue_state` returns `None` (API failure) | Logged, non-fatal; no write this cycle — retried later. Does **not** default to `"closed"` or `"open"` — guessing either way risks a wrong action (a wrong "closed" guess opens a spurious duplicate escalation; a wrong "open" guess silently blocks a new one from ever opening) |
| GitHub issue creation fails (new or retry) | `github_issue_number` stays `""`; retried on the next qualifying delivery or sweep cycle, same shape as `FileEscalation` |
| GitHub update comment fails | Logged, non-fatal; the `INCLUDES` edge is still written — matches `FileEscalation`'s authority-model precedent |
| Reconciliation sweep itself fails | Isolated in its own `try`/`except`, does not block other poll-cycle steps |

---

## Testable Non-Goals

- No `ErrorSignature`-level grouping in v1 — deferred (Key Design Decision #16).
- No automatic closing, editing, or re-opening of a `RootCauseEscalation`'s GitHub issue by MODOK — closing is exclusively a human action MODOK observes, never performs.
- **`INCLUDES` is a permanent, historical record regardless of the escalation's open/closed state or later changes to the ticket** — found during the Phase 2 edge-case probe to need stating for the open-escalation case explicitly, not just the closed one. A ticket already linked to *any* escalation (open or closed) is never re-linked, moved, or removed — including if its `AFFECTS` edge to the feature is later reconciled away by an edited ticket (`docs/llds/standing-queries.md § Mechanical Anchor Linking` step 6), and including if it's included in a still-open escalation. Same non-retroactive discipline `FileEscalation` establishes, applied uniformly rather than only to the closed case.
- No cap on how many sequential `RootCauseEscalation`s a single feature can accumulate over its lifetime.
- `RootCauseEscalation.status` is written but never read for any decision — GitHub's live issue state is authoritative, not the graph.
- No polling or caching of GitHub issue state outside the direct `get_issue_state` call made at decision time — every check is live.
- `_process_root_cause_escalation` called with a `feature_slug` that matches no existing `Feature` node returns `0` silently — step 1's first `MATCH` simply binds zero rows. Not an error, not logged distinctly (mirrors `FileEscalation`'s equivalent "no `Commit` edge" non-goal).
- **A ticket affecting multiple features can be legitimately included in multiple, independent, simultaneously-open `RootCauseEscalation`s — one per feature.** Exclusion (step 1, query 2) is scoped per-feature by design: Key Design Decision #16's "never double-counted" language means never recounting the *same ticket toward the same feature's threshold twice* across a closed-then-reopened sequence, not a global one-escalation-per-ticket constraint. A ticket genuinely affecting both `wifi-provisioning` and `bluetooth-pairing` is legitimately relevant research context for both.

## Decisions & Alternatives

| Decision | Chosen | Alternatives Considered | Rationale |
|---|---|---|---|
| Grouping key | Shared `Feature` (`AFFECTS`) | Shared `ErrorSignature` (`HAS_ERROR`); both, as two patterns | User-selected: `AFFECTS` coverage is far broader — most organically-written tickets never produce a `HAS_ERROR` match |
| Reset mechanism | Closing the escalation's GitHub issue (live API check) | No reset, accumulate forever; literal calendar window (e.g. 30 days) | User-selected: puts the reset decision in the human's hands rather than an arbitrary constant; avoids the recency-alone triggering this project has rejected elsewhere |
| Threshold/exclusion logic location | Python (`_process_root_cause_escalation`), ordinary unrestricted Cypher | Standing-query enrichment (aggregation), matching `FileEscalation` | This component's exclusion logic spans two node types (`CustomerIssue` open-status and `RootCauseEscalation`'s own prior `INCLUDES`), materially more complex than `FileEscalation`'s single-hop count; keeping the enrichment trivial avoids introducing another unverified Cypher shape |
| Qualifying-ticket exclusion technique | Two queries + Python set difference | A single `WHERE NOT EXISTS { ... }` subquery | The subquery shape is unverified against this project's Quine version; two proven-safe queries plus a Python set difference carries zero new Cypher risk |
| Qualifying-exclusion scope | Excludes tickets linked only to escalations with a real `github_issue_number` (`<> ''`) | Exclude any `INCLUDES`-linked ticket regardless of issue-creation success | Found during the Phase 2 edge-case probe: excluding unconditionally silently made the retry path unreachable under normal operation — a failed escalation's stuck tickets would never resurface in `qualifying`, since step 1's `< 3` guard runs before any retry logic is reached |
| `_create_or_retry_root_cause_escalation` gating | Split: node/edge writes unconditional (idempotent); the external `create_issue` call separately re-checked immediately before firing | Gate the entire sequence on `not exists`, mirroring `_process_file_escalation`'s original shape; or make the entire sequence unconditional | A single `not exists` gate around everything (including `create_issue`) reintroduces the double-issue race that gate exists to narrow, once the exclusion-scope fix above makes retries reachable with a potentially-grown `qualifying` set. Splitting lets the safe, idempotent writes always run while keeping the one non-idempotent, externally-visible action narrowly guarded |
| `RootCauseEscalation.status` | Always `"open"`, decorative only | Sync it from GitHub's real issue state on every check | GitHub is already the authoritative source consulted at decision time; caching its state onto the node risks the value going stale between checks, exactly the failure mode this component exists to avoid for the *decision* itself — no benefit to also caching it redundantly |
| GitHub issue title | No sequence number in the human-facing text | Include "attempt #N" or similar | The issue's own open/closed state on GitHub already communicates which escalation is current; repeating the sequence number in prose adds noise, not information |

## Open Questions & Future Decisions

1. **`ErrorSignature`-level grouping** — deferred, not designed here (Key Design Decision #16).
2. **No cap on sequence growth** — a feature that never gets fixed could accumulate many sequential escalations over a project's lifetime. Not expected to matter at any realistic scale; revisit if observed.
3. **`get_issue_state` cost at scale** — one live GitHub API call per currently-open `RootCauseEscalation`, per poll cycle (via the sweep) plus per relevant standing-query delivery. Bounded by open-escalation count, not ticket volume, but not measured against GitHub's rate limits at any particular scale — same caveat this project's poll-interval tuning already carries elsewhere.
4. **Idea #1/#2 interaction** — a ticket can independently trigger both `FileEscalation` and `RootCauseEscalation`; nothing unifies or cross-references the two. Not investigated — no evidence either currently needs the other.
5. **Multi-`AFFECTS` fan-out is asserted, not confirmed — a weaker claim than `FileEscalation`'s own equivalent Open Question.** If a single `CustomerIssue` affects two or more features, the enrichment's `MATCH (ci)-[:AFFECTS]->(feat)` (no `LIMIT`) can return multiple rows for one firing, relying on `docs/llds/standing-queries.md § Standing Query Result Route`'s existing generic array-body handling to fan this out into independent `_process_root_cause_escalation` calls per feature. `file-escalation-pattern.md`'s Open Question #8 flags the identical reliance for its own pattern and explicitly marks it "not separately exercised live" — this document should carry the same caveat rather than asserting the fan-out works without qualification, found missing during the Phase 2 edge-case probe.

## References

- `docs/high-level-design.md § Root-Cause Escalation Pattern, Key Design Decision #16`
- `docs/llds/file-escalation-pattern.md` — the sibling component this one's architecture, naming, and idempotency conventions mirror throughout
- `docs/llds/standing-queries.md § Mechanical Anchor Linking` — the pre-existing `AFFECTS` edge this component reads, never writes
- `docs/llds/quine-client.md` — `write_edge_by_parts`, `node_exists_by_parts` primitives reused here unchanged
