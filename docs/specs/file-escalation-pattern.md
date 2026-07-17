# File Escalation Pattern Specs

See `docs/llds/file-escalation-pattern.md`. Test Level Convention matches `docs/specs/quine-client.md § Test Level Convention`: `[U]` unit-testable against a mocked client; `[C]` requires confirmation against a live Quine instance; `[P]` a property that must hold regardless of ordering/repetition/concurrency.

---

## Graph Model

- [ ] **FESC-NODE-001** [U]: THE SYSTEM SHALL introduce a `FileEscalation` node type with fields `project_slug`, `file_path`, `since_commit`, `github_issue_number`, `status`, `created_at`, `standing_query_name`, addressed via `idFrom('file-escalation', project_slug, file_path, since_commit)`.
- [ ] **FESC-NODE-002** [U]: `CustomerIssue` SHALL gain a `created_at` field, set to MODOK's own ingestion wall-clock time (ISO 8601 UTC) at each of the three existing `CustomerIssue(...)` construction call sites (`webhook/pipeline.py` × 2, `cli/commands/ingest.py` × 1). No other code path SHALL modify `created_at` after initial construction.
- [ ] **FESC-EDGE-001** [U]: THE SYSTEM SHALL write `FileEscalation -[:ESCALATES]-> File` exactly once, at first creation of the `FileEscalation` node — never re-written on subsequent processing of the same `(file_path, since_commit)`.
- [ ] **FESC-EDGE-002** [U]: THE SYSTEM SHALL write `FileEscalation -[:INCLUDES]-> CustomerIssue` via additive `write_edge_by_parts`, one edge per newly-discovered contributing `CustomerIssue` — never via `replace_edges_by_parts`. An issue already linked SHALL NOT be re-written or removed by a later processing pass.

---

## `FLAGS` Write-Back

- [ ] **FESC-FLAGS-001** [U]: WHEN `_maybe_notify_github`'s `retrieve()` call returns a packet for a `source_system == "github"` `CustomerIssue`, THE SYSTEM SHALL compute the set of `path`s from `packet.scored_candidates` WHERE `kind == "source"` AND `confidence == "high"`.
- [ ] **FESC-FLAGS-002** [U, P]: THE SYSTEM SHALL call `replace_edges_by_parts` to reconcile the `CustomerIssue`'s `FLAGS` edges to exactly the set computed in FESC-FLAGS-001, unconditionally — including WHEN that set is empty, so a re-investigation that drops a ticket's high-confidence file count to zero clears any stale `FLAGS` edges from a prior investigation rather than leaving them in place.
- [ ] **FESC-FLAGS-003** [U]: A `scored_candidates` path with no corresponding `File` node SHALL receive no `FLAGS` edge (the endpoint does not exist for `replace_edges_by_parts`/`write_edge_by_parts` to `MATCH`) and SHALL NOT raise an error or cause a `File` node to be invented.
- [ ] **FESC-FLAGS-004** [U]: WHEN `source_system != "github"`, THE SYSTEM SHALL NOT write any `FLAGS` edge (inherits `_maybe_notify_github`'s existing early return).
- [ ] **FESC-FLAGS-005** [U]: `FLAGS` edges SHALL be written only from `_process_investigation`'s `retrieve()` call (via `_maybe_notify_github`). `_process_milestone` (the `ci-corroboration-pattern`/CI-corroboration path) SHALL NOT write, update, or refresh `FLAGS` edges for any `CustomerIssue` — this is a deliberate v1 scope boundary, not an oversight (`docs/llds/file-escalation-pattern.md § Open Questions, item 9`).

---

## Standing Query

- [ ] **FESC-SQ-001** [C]: `file-escalation-pattern`'s pattern (`MATCH (f)<-[:FLAGS]-(ci) WHERE f.node_type = 'File' AND ci.node_type = 'CustomerIssue' RETURN DISTINCT id(ci) AS id`, mode `DistinctId`) SHALL register successfully against a live Quine instance and SHALL fire once per newly-written `FLAGS` edge whose source `ci` has not previously appeared as a match for this pattern.
- [ ] **FESC-SQ-002** [C]: The pattern's `enrichment_query` SHALL, for the firing `ci`, traverse to the flagged `File`, find that `File`'s most recent `Commit` via `ORDER BY c.timestamp DESC LIMIT 1`, count distinct qualifying `CustomerIssue`s (`ci2.created_at > c.timestamp`), and RETURN a result row (thereby triggering `PostToEndpoint` delivery) if and only if that count is `>= 3`.
- [ ] **FESC-SQ-003** [C, P]: WHEN a file has fewer than 3 qualifying `FLAGS` edges since its most recent `Commit`, no delivery SHALL occur. WHEN a 3rd qualifying edge lands, exactly one delivery SHALL occur with the correct `n`. A 4th, 5th, etc. qualifying edge MAY independently produce further deliveries for the same `(file_path, since_commit)` — this is expected, not a bug, and correctness under redelivery is the responsibility of `_process_file_escalation` (§ below), not the standing query.
- [ ] **FESC-SQ-004** [C, P]: WHEN a new `Commit` lands touching a `File` that already has an open escalation window, and 3 further qualifying `CustomerIssue`s flag that file after the new commit's timestamp, a delivery SHALL occur with `since_commit` equal to the new commit's `sha` and `n` counted only from issues created after the new commit — issues that qualified under the *prior* `since_commit` SHALL NOT count toward this new window.
- [ ] **FESC-SQ-005** [C]: THE SYSTEM SHALL NOT attempt to key the pattern's `DistinctId` return on the `FLAGS` relationship itself — Quine's standing-query pattern grammar does not support binding a relationship to a variable (`CompileError: Assigning edges to variables is not yet supported in standing query patterns`, live-confirmed). Any future revision of this pattern MUST NOT reintroduce this shape without re-verifying against a live Quine instance first.
- [ ] **FESC-SQ-006** [U]: `POST /standing-query/result`'s existing payload-shape dispatch SHALL route a row containing `since_commit` and no `milestone_kind` to `FileEscalationData`; a row containing `milestone_kind` SHALL continue routing to `MilestoneData`; any other row SHALL continue routing to `InvestigationData` (the pre-existing default, unchanged).

---

## `_process_file_escalation` (shared branch/sweep logic)

- [ ] **FESC-PROC-001** [U]: `_process_file_escalation(client, project_slug, file_path, since_commit)` SHALL be the single function used by both the `run_ingest_event` `file_escalation` branch and the reconciliation sweep (FESC-POLL-* below) — no duplicated processing logic between the two call sites. It SHALL be defined in `src/modok/webhook/server.py`, alongside `_process_investigation`/`_process_milestone`.
- [ ] **FESC-PROC-001a** [U]: `reconcile_file_escalations` (`src/modok/ingestion/ci_ingestion.py`) SHALL import `_process_file_escalation` via a function-body (lazy) import, never a module-level import — a module-level import would close the real `server.py → router.py → github_poll.py → ci_ingestion.py` import chain into a cycle. This mirrors `src/modok/webhook/pipeline.py`'s existing technique for `_process_investigation`/`_process_milestone`.
- [ ] **FESC-PROC-002** [U]: On invocation, THE SYSTEM SHALL re-derive the current full set of qualifying `CustomerIssue`s for `(file_path, since_commit)` directly from the graph (not trust only whichever `ci` triggered the call). WHEN fewer than 3 rows are returned, THE SYSTEM SHALL write nothing and return without error.
- [ ] **FESC-PROC-003** [U, P]: WHEN no `FileEscalation` node exists for `(project_slug, file_path, since_commit)`, THE SYSTEM SHALL, in this order: (a) upsert a `FileEscalation` node with `status="open"`, `created_at=now`, `github_issue_number=""`; (b) write `ESCALATES` to the `File`; (c) write `INCLUDES` to every currently-qualifying `CustomerIssue`; (d) only then attempt to create a GitHub issue (FESC-GH-* below). Node/edge writes SHALL happen before the GitHub API call, never after.
- [ ] **FESC-PROC-004** [U]: WHEN a `FileEscalation` node exists with `github_issue_number == ""`, THE SYSTEM SHALL treat GitHub issue creation as pending/previously-failed and retry it (same create-and-store step as FESC-PROC-003(d)) — SHALL NOT attempt to post an update comment in this state.
- [ ] **FESC-PROC-005** [U]: WHEN a `FileEscalation` node exists with a non-empty `github_issue_number`, THE SYSTEM SHALL diff the currently-qualifying `CustomerIssue`s against existing `INCLUDES` targets. For each issue not already linked: THE SYSTEM SHALL write its `INCLUDES` edge regardless of whether the accompanying update comment succeeds — a failed `post_issue_comment` call (FESC-ERR-003) SHALL NOT prevent or roll back the `INCLUDES` edge write. WHEN the diff is empty, THE SYSTEM SHALL take no action (no edge write, no comment).
- [ ] **FESC-PROC-006** [U]: `ticket_kind`/`status` of a contributing `CustomerIssue` SHALL NOT be checked anywhere in this function — a closed or resolved ticket's `FLAGS` edge counts toward the threshold and stays in `INCLUDES` identically to an open one.
- [ ] **FESC-PROC-007** [U]: Any exception raised anywhere within `_process_file_escalation` SHALL be caught, logged to stderr, and SHALL NOT propagate to the caller.

---

## GitHub Issue Creation

- [ ] **FESC-GH-001** [U]: `create_issue(github_repo, token, title, body) -> str | None` SHALL return the created issue's number as a string on a 2xx response, and `None` on any non-2xx response or exception — never raising.
- [ ] **FESC-GH-002** [U]: WHEN `github_repo` is unconfigured for the project or `GITHUB_TOKEN` is unset, `_process_file_escalation` SHALL log and return without attempting `create_issue`, leaving any already-written `FileEscalation` node's `github_issue_number` at `""`.
- [ ] **FESC-GH-003** [U]: The escalation issue's title SHALL be `"MODOK: {file_path} flagged by {n} tickets since {since_commit[:7]}"` and it SHALL carry the label `"modok-escalation"`.
- [ ] **FESC-GH-004** [U]: `FileEscalation.github_issue_number` SHALL be updated only WHEN `create_issue` returns a non-`None` value. WHEN `create_issue` returns `None`, `github_issue_number` SHALL remain `""` on the node, leaving it eligible for a retry on the next call to `_process_file_escalation` for the same `(file_path, since_commit)`.

---

## Reconciliation Sweep

- [ ] **FESC-POLL-001** [U]: `reconcile_file_escalations(client, project_slug)` SHALL query for `(file_path, since_commit)` pairs meeting the same threshold-and-recency condition as the standing query's enrichment (FESC-SQ-002), independent of whether any standing-query delivery ever occurred for them, and SHALL call `_process_file_escalation` for each.
- [ ] **FESC-POLL-002** [U, P]: A `(file_path, since_commit)` pair that already has a `FileEscalation` node with a non-empty `github_issue_number` and no new qualifying issues SHALL be a no-op when reprocessed by the sweep — verifying `_process_file_escalation`'s own idempotency (FESC-PROC-005) is sufficient without any sweep-specific dedup logic.
- [ ] **FESC-POLL-003** [U]: THE SYSTEM SHALL run `reconcile_file_escalations` once per poll cycle, per project, isolated in its own `try`/`except` within `_run_ci_ingestion_cycle` — a failure SHALL NOT block issue/PR sync, CI ingestion's other steps, or dependency ingestion in the same cycle.
- [ ] **FESC-POLL-004** [U, P]: A file that a standing-query delivery already fully processed (an open `FileEscalation` with all currently-qualifying issues already in `INCLUDES` and a non-empty `github_issue_number`) SHALL NOT produce a duplicate GitHub issue or a duplicate update comment when subsequently visited by the sweep.

---

## Failure Handling

- [ ] **FESC-ERR-001** [U]: A `File` with no `Commit -[:TOUCHES]->` edge at all SHALL never appear in either the standing query's enrichment results or the reconciliation sweep's results, regardless of how many `FLAGS` edges it accumulates.
- [ ] **FESC-ERR-002** [U]: `ci.created_at > c.timestamp` SHALL be a strict inequality — an issue with `created_at` equal to or earlier than the file's most recent commit timestamp SHALL NOT count toward the threshold, in both the standing-query enrichment and the reconciliation sweep.
- [ ] **FESC-ERR-003** [U]: A failure creating the GitHub issue, posting an update comment, or running `_process_file_escalation`'s own re-derivation query SHALL all be caught by the same broad exception handling (FESC-PROC-007) — no failure mode within this function is distinct from another in how it is handled.

---

## Scope Boundary

- [ ] **FESC-SCOPE-001** [U]: THE SYSTEM SHALL write `FLAGS` edges only for `scored_candidates` entries WHERE `kind == "source"`. A `kind == "test"` candidate, however high its confidence, SHALL NOT receive a `FLAGS` edge in this version.
- [ ] **FESC-SCOPE-002** [U]: `FileEscalation.status` SHALL only ever be written as `"open"` — no code path SHALL write `"resolved"`, `"stale"`, or any other value.
- [ ] **FESC-SCOPE-003** [P]: WHEN a `FLAGS` edge that previously contributed to an already-created `FileEscalation` is later removed by a subsequent `replace_edges_by_parts` reconciliation (FESC-FLAGS-002), THE SYSTEM SHALL NOT retroactively edit, close, or comment on the already-created GitHub issue to reflect the removal.
- [ ] **FESC-SCOPE-004** [P]: WHEN a `File`'s `repo_path` changes after a `FileEscalation` already exists referencing the old path, THE SYSTEM SHALL NOT re-resolve, migrate, or merge that `FileEscalation` to the new path — the existing node's re-derivation query (keyed on the old path string) simply stops matching, and the node's ability to accumulate further `INCLUDES` edges or update comments ends silently. Falsified if a `repo_path` change causes a new `FileEscalation` to be spuriously created for the same logical file, or causes the old node's data to be mutated.

---

## Open Questions & Future Decisions

**Not independently unit-tested, by design**: `FESC-FLAGS-003` (never-invent-a-node on a missing `File` target) is inherited behavior of `replace_edges_by_parts` itself, already covered by that primitive's own tests — not re-verified here. `FESC-SQ-003`/`FESC-SQ-004` are `[C]`-level: the live firing/reset/redelivery behavior they describe was confirmed via direct live-Quine spikes during Phase 1/2 design (`docs/high-level-design.md § Key Design Decision #15`), not re-derivable from a mocked `DummyQuine` unit test — consistent with this project's existing Test Level Convention (`docs/specs/quine-client.md`).

Traced to `docs/llds/file-escalation-pattern.md § Open Questions & Future Decisions` — not independently spec'd, since none are behavioral requirements yet: `kind == "test"` candidate inclusion, plumbing GitHub's original issue `created_at`, `FileEscalation.status` lifecycle beyond `"open"`, retroactive re-evaluation on `FLAGS` reconciliation (already covered as a negative spec, `FESC-SCOPE-003`), idea #2 (common-root parent investigation, separate LID arrow), ticket `status`/`ticket_kind` filtering (already covered as a negative spec, `FESC-PROC-006`), `created_at`/`timestamp` tie-break handling (already covered as a spec, `FESC-ERR-002`), and multi-row enrichment delivery fan-out (relies on `docs/llds/standing-queries.md`'s existing generic array-body route handling — not independently spec'd here since no new behavior is introduced by this component for that case).
