# Root-Cause Escalation Pattern Specs

See `docs/llds/root-cause-escalation-pattern.md`. Test Level Convention matches `docs/specs/quine-client.md § Test Level Convention`: `[U]` unit-testable against a mocked client; `[C]` requires confirmation against a live Quine instance; `[P]` a property that must hold regardless of ordering/repetition/concurrency.

---

## Graph Model

- [ ] **RCESC-NODE-001** [U]: THE SYSTEM SHALL introduce a `RootCauseEscalation` node type with fields `project_slug`, `feature_slug`, `sequence` (int), `github_issue_number`, `status`, `created_at`, `standing_query_name`, addressed via `idFrom('root-cause-escalation', project_slug, feature_slug, sequence)`.
- [ ] **RCESC-NODE-002** [U]: `RootCauseEscalation.status` SHALL always be written as `"open"` and SHALL NOT be read by any decision logic in this component — GitHub's live issue state, not this property, is authoritative for whether an escalation currently accepts new contributions.
- [ ] **RCESC-EDGE-001** [U]: THE SYSTEM SHALL write `RootCauseEscalation -[:ESCALATES]-> Feature` only when the `RootCauseEscalation` node does not already exist — never rewritten on a subsequent call for the same `(feature_slug, sequence)`.
- [ ] **RCESC-EDGE-002** [U, P]: THE SYSTEM SHALL write `RootCauseEscalation -[:INCLUDES]-> CustomerIssue` via additive `write_edge_by_parts`, unconditionally on every call regardless of whether the `RootCauseEscalation` node already existed — an already-present edge is a harmless idempotent re-write, and this is what allows a ticket that becomes qualifying after the escalation's first creation to still be linked.

---

## `_process_root_cause_escalation`

- [ ] **RCESC-PROC-001** [U]: THE SYSTEM SHALL query currently-open (`CustomerIssue.status == "open"`) tickets affecting the given feature via `AFFECTS`, and separately query tickets already linked via `INCLUDES` to a `RootCauseEscalation` for that feature that has a non-empty `github_issue_number`. `qualifying` SHALL be the first set minus the second (a Python-side set difference, not a Cypher subquery).
- [ ] **RCESC-PROC-002** [U]: WHEN `len(qualifying) < 3`, THE SYSTEM SHALL write nothing and return `0`.
- [ ] **RCESC-PROC-003** [U]: A ticket linked via `INCLUDES` to a `RootCauseEscalation` whose `github_issue_number` is still `""` (creation pending or failed) SHALL NOT be excluded from `qualifying` on a subsequent call — only tickets linked to an escalation with a real, non-empty `github_issue_number` are excluded.
- [ ] **RCESC-PROC-004** [U]: THE SYSTEM SHALL find the `RootCauseEscalation` with the highest `sequence` for the given `(project_slug, feature_slug)`, if any, via `ORDER BY sequence DESC LIMIT 1`.
- [ ] **RCESC-PROC-005** [U]: WHEN no `RootCauseEscalation` exists for the feature, THE SYSTEM SHALL create one at `sequence = 1`.
- [ ] **RCESC-PROC-006** [U]: WHEN the latest `RootCauseEscalation`'s `github_issue_number` is `""`, THE SYSTEM SHALL retry creation at the *same* `sequence` — SHALL NOT increment it.
- [ ] **RCESC-PROC-007** [U]: WHEN the latest `RootCauseEscalation` has a non-empty `github_issue_number`, THE SYSTEM SHALL call `get_issue_state` for that issue number before taking any further action.
- [ ] **RCESC-PROC-008** [U]: WHEN `get_issue_state` returns `None`, THE SYSTEM SHALL write nothing and return `0` — it SHALL NOT assume `"open"` or `"closed"`.
- [ ] **RCESC-PROC-009** [U]: WHEN `get_issue_state` returns `"open"`, THE SYSTEM SHALL write an `INCLUDES` edge and post an update comment for every entry in `qualifying`, without any additional diff step (the exclusion in RCESC-PROC-001 already guarantees every entry is new).
- [ ] **RCESC-PROC-010** [U]: WHEN `get_issue_state` returns `"closed"`, THE SYSTEM SHALL create a new `RootCauseEscalation` at `sequence = latest_sequence + 1`.
- [ ] **RCESC-PROC-011** [U]: `github_repo`/`GITHUB_TOKEN` unavailability at any point where a GitHub call would otherwise be made SHALL be logged and SHALL result in no write and a `0` return, not an exception.
- [ ] **RCESC-PROC-012** [U]: Any exception raised anywhere within `_process_root_cause_escalation` (including inside `_create_or_retry_root_cause_escalation`) SHALL be caught, logged to stderr, and SHALL NOT propagate to the caller.
- [ ] **RCESC-PROC-013** [U]: `_process_root_cause_escalation` called with a `feature_slug` matching no existing `Feature` node SHALL return `0` without error (RCESC-PROC-001's first query binds zero rows).

---

## `_create_or_retry_root_cause_escalation`

- [ ] **RCESC-CREATE-001** [U, P]: `RootCauseEscalation` node upsert and its `ESCALATES` edge SHALL be written only WHEN `node_exists_by_parts` for `(feature_slug, sequence)` returns `False`.
- [ ] **RCESC-CREATE-002** [U, P]: Immediately before calling `create_issue`, THE SYSTEM SHALL re-fetch the node's current `github_issue_number` via a direct property query. WHEN it is already non-empty, THE SYSTEM SHALL NOT call `create_issue` again and SHALL return without further action.
- [ ] **RCESC-CREATE-003** [U]: `github_issue_number` SHALL be updated via a targeted property `SET`, not a full `upsert_node` call, and only WHEN `create_issue` returns a non-`None` value.

---

## GitHub Issue State

- [ ] **RCESC-GH-001** [U]: `get_issue_state(github_repo, token, issue_number) -> str | None` SHALL return the value of the fetched issue's `state` field (`"open"`/`"closed"`, passed through unchanged) on a 2xx response.
- [ ] **RCESC-GH-002** [U]: A `404` response SHALL return `"closed"`, not `None` — a deleted or transferred issue is treated as equivalent to a human closing it, since neither can be appended to.
- [ ] **RCESC-GH-003** [U]: Any other non-2xx response or exception SHALL return `None`. `get_issue_state` SHALL NOT raise under any condition.
- [ ] **RCESC-GH-004** [U]: `create_issue` SHALL be called with `labels=["modok-root-cause"]` — a distinct label from `FileEscalation`'s `"modok-escalation"`.
- [ ] **RCESC-GH-005** [U]: The escalation issue's title SHALL be `"MODOK: {feature_slug} has {n} open tickets in progress"`, with no sequence number in the title text.

---

## Standing Query

- [ ] **RCESC-SQ-001** [C]: `root-cause-escalation-pattern`'s pattern (`MATCH (feat)<-[:AFFECTS]-(ci) WHERE ... RETURN DISTINCT id(ci) AS id`, mode `DistinctId`) SHALL contain no `WITH` clause and no aggregate function — matching the proven-safe `error-flagged-pattern` shape, not attempting the more complex aggregation `file-escalation-pattern` uses.
- [ ] **RCESC-SQ-002** [U]: The pattern SHALL key on `id(ci)`, not `id(feat)` — verified via static inspection of the loaded pattern string.
- [ ] **RCESC-SQ-003** [U]: The `enrichment_query` SHALL return only `project_slug`, `feature_slug`, and `standing_query_name` — no aggregation, no threshold check; all threshold/exclusion logic lives in `_process_root_cause_escalation`.
- [ ] **RCESC-SQ-004** [U]: `POST /standing-query/result`'s dispatch SHALL route a row containing `feature_slug` and neither `since_commit` nor `milestone_kind` to `RootCauseEscalationData`.

---

## Reconciliation Sweep

- [ ] **RCESC-POLL-001** [U]: `reconcile_root_cause_escalations(client, project_slug)` SHALL query for feature slugs with at least one currently-open, affecting `CustomerIssue`, and SHALL call `_process_root_cause_escalation` for each — regardless of whether that feature actually has 3+ unlinked qualifying tickets (the authoritative check is inside the shared function, not the sweep's prefilter).
- [ ] **RCESC-POLL-002** [U]: `reconcile_root_cause_escalations` SHALL import `_process_root_cause_escalation` via a function-body (lazy) import, never module-level, for the same import-cycle reason `reconcile_file_escalations` does (`FESC-PROC-001a`).
- [ ] **RCESC-POLL-003** [U]: THE SYSTEM SHALL run `reconcile_root_cause_escalations` once per poll cycle, per project, isolated in its own `try`/`except` within `_run_ci_ingestion_cycle` — a failure SHALL NOT block any other per-cycle step.

---

## Status Sync

- [ ] **RCESC-STATUS-001** [U]: `reconcile_root_cause_escalation_status(client, project_slug, github_repo, token)` SHALL query every `RootCauseEscalation` for the project WHERE `status = 'open'` AND `github_issue_number <> ''`.
- [ ] **RCESC-STATUS-002** [U]: For each queried escalation, THE SYSTEM SHALL call `get_issue_state(github_repo, token, github_issue_number)`.
- [ ] **RCESC-STATUS-003** [U, P]: WHEN `get_issue_state` returns `"closed"`, THE SYSTEM SHALL `SET status = 'closed'` on that `RootCauseEscalation` via a targeted property update.
- [ ] **RCESC-STATUS-004** [U]: WHEN `get_issue_state` returns `"open"` or `None`, THE SYSTEM SHALL NOT write anything for that escalation.
- [ ] **RCESC-STATUS-005** [U]: A `RootCauseEscalation` already `status = 'closed'` SHALL be excluded from every subsequent run's query (RCESC-STATUS-001) — never re-checked or reverted.
- [ ] **RCESC-STATUS-006** [U]: `status` written by this sweep SHALL NOT be read anywhere in `_process_root_cause_escalation`'s append-vs-new decision logic — that logic SHALL continue to call `get_issue_state` directly, independent of this sweep's writes.
- [ ] **RCESC-STATUS-007** [U]: THE SYSTEM SHALL run `reconcile_root_cause_escalation_status` once per poll cycle, per project, isolated in its own `try`/`except` within `_run_ci_ingestion_cycle` — a failure SHALL NOT block any other per-cycle step, including `reconcile_root_cause_escalations`.

---

## Scope Boundary

- [ ] **RCESC-SCOPE-001** [U]: THE SYSTEM SHALL group only by `Feature` (`AFFECTS`) in this version — no `ErrorSignature`-based (`HAS_ERROR`) grouping.
- [ ] **RCESC-SCOPE-002** [P]: A `CustomerIssue` already linked via `INCLUDES` to any `RootCauseEscalation` (open or closed) for a feature SHALL NOT be re-linked, moved, or removed from it — including if its `AFFECTS` edge to that feature is later reconciled away.
- [ ] **RCESC-SCOPE-003** [U]: THE SYSTEM SHALL NOT close, edit, or re-open a `RootCauseEscalation`'s GitHub issue under any condition — only a human action, observed via `get_issue_state`, changes what MODOK does next.
- [ ] **RCESC-SCOPE-004** [P]: A `CustomerIssue` affecting two or more `Feature`s MAY be linked via `INCLUDES` to a separate, independently-open `RootCauseEscalation` for each — this is not double-counting within a single feature's threshold and SHALL NOT be prevented.

---

## Open Questions & Future Decisions

Traced to `docs/llds/root-cause-escalation-pattern.md § Open Questions & Future Decisions` — not independently spec'd: `ErrorSignature`-level grouping, unbounded sequence growth, `get_issue_state` cost at scale, `FileEscalation`/`RootCauseEscalation` non-interaction, and multi-`AFFECTS` fan-out reliance on `docs/llds/standing-queries.md`'s existing generic route handling (inherited, not independently confirmed — same caveat `file-escalation-pattern.md`'s own Open Question #8 carries).

**Accepted residual risks, not fixed, matching `FileEscalation`'s precedent for structurally identical risks**: (a) two near-simultaneous callers can both pass `RCESC-CREATE-002`'s check before either writes the real issue number, in a true-simultaneous read (narrowed, not eliminated); (b) a human closing the issue in the window between `get_issue_state` and the subsequent append write is not re-checked; (c) two near-simultaneous append-branch calls can both post the same update comment before either's `INCLUDES` write is visible to the other.
