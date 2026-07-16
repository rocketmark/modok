# Test-Coverage CI Linking Specs

See `docs/llds/test-coverage-ci-linking.md`. Test Level Convention matches `docs/specs/quine-client.md § Test Level Convention`: `[U]` unit-testable against a mocked client; `[C]` requires confirmation against a live Quine instance; `[P]` a property that must hold regardless of ordering/repetition.

---

## classname → TestFile Resolution

- [ ] **TCLINK-RESOLVE-001** [U]: Given a `classname` string, THE SYSTEM SHALL generate candidate paths by splitting on `.` into `parts` and, for `i` from `len(parts)` down to `1`, forming candidate `i` as `"/".join(parts[:i]) + ".py"` — trying the full dotted path first, then progressively shorter prefixes (treating trailing segments as class names, not path segments).
- [ ] **TCLINK-RESOLVE-002** [U]: For each candidate, in most-specific-first order, THE SYSTEM SHALL check registered `TestFile` nodes for an exact `repo_path` match. WHERE the candidate has two or more path segments, THE SYSTEM SHALL ALSO check for a `repo_path` ending with `"/" + candidate`. A single-segment candidate SHALL NOT be checked by suffix match, only exact match.
- [ ] **TCLINK-RESOLVE-003** [U]: THE SYSTEM SHALL stop at the first candidate (most specific first) that matches at least one `TestFile`, ignoring less-specific candidates once a match is found.
- [ ] **TCLINK-RESOLVE-004** [U]: WHEN a candidate matches exactly one `TestFile`, THE SYSTEM SHALL treat it as resolved and use it as the link target.
- [ ] **TCLINK-RESOLVE-005** [U]: WHEN a candidate matches more than one `TestFile`, THE SYSTEM SHALL NOT write an `EXECUTES` edge and SHALL NOT fall through to try a less-specific candidate — the resolution for that `TestExecution` is `"ambiguous"`, not retried against a different candidate within the same resolution attempt.
- [ ] **TCLINK-RESOLVE-006** [U]: WHEN no candidate matches any `TestFile`, THE SYSTEM SHALL NOT write an `EXECUTES` edge and SHALL NOT invent a `TestFile` node.

---

## Graph Model

- [ ] **TCLINK-EDGE-001** [U]: THE SYSTEM SHALL write `TestExecution -[:EXECUTES]-> TestFile` via `write_edge_by_parts`, addressing the `TestExecution` by its existing key (`"test-execution", project_slug, run_id, run_attempt, classname, test_name`) and the `TestFile` by its existing key (`"test-file", project_slug, repo_path`). No new node type SHALL be introduced.
- [ ] **TCLINK-EDGE-002** [U]: THE SYSTEM SHALL NOT write a direct edge from `TestFailure` to `TestFile`. A `TestFailure`'s file SHALL be reachable only via `TestFailure -[:OCCURRED_IN]-> TestExecution -[:EXECUTES]-> TestFile`.
- [ ] **TCLINK-EDGE-003** [U]: `TestExecution` SHALL carry a `link_state` property, one of `"resolved"`, `"ambiguous"`, or unset — a property on the existing node, not a new node type or `idFrom` key change. A no-match ("unresolved") resolution result SHALL NOT be persisted to this property; it SHALL leave `link_state` unset.

---

## Where Resolution Runs

- [ ] **TCLINK-POLL-001** [U]: WHEN `write_test_execution` upserts a new `TestExecution` node and writes its `RAN_IN` edge, THE SYSTEM SHALL immediately attempt classname resolution (TCLINK-RESOLVE-001 through 006) for that execution.
- [ ] **TCLINK-POLL-002** [U, P]: Once per poll cycle, per project, independent of any cursor, THE SYSTEM SHALL sweep `TestExecution` nodes where `link_state IS NULL OR link_state = "ambiguous"` and retry resolution for each. This sweep SHALL NOT exclude a `TestExecution` on the basis of how many prior reconciliation attempts it has had — a no-match result SHALL remain eligible for every future cycle, indefinitely (deliberate: see `docs/llds/test-coverage-ci-linking.md § Where Resolution Runs, Cost caveat` for why a bounded-attempts exclusion was drafted and rejected).
- [ ] **TCLINK-POLL-003** [U]: WHEN a reconciliation attempt (TCLINK-POLL-002) finds zero matching candidates, THE SYSTEM SHALL NOT modify `link_state` — it remains unset (or `"ambiguous"` if it already was) and the `TestExecution` remains eligible for the next cycle's sweep. WHEN it finds more than one matching candidate, THE SYSTEM SHALL set `link_state = "ambiguous"`.
- [ ] **TCLINK-POLL-004** [U]: The inline resolution attempt (TCLINK-POLL-001) and the reconciliation sweep (TCLINK-POLL-002) SHALL call the same resolution function — no duplicated resolution logic between the two call sites.
- [ ] **TCLINK-POLL-005** [U]: A failure in the reconciliation sweep SHALL be isolated in its own `try`/`except` in the poll cycle and SHALL NOT block issue/PR sync, CI ingestion's other steps, or dependency ingestion in the same cycle.

---

## Diagnostic Retrieval Engine Integration

- [ ] **TCLINK-DRE-001** [U]: The system shall add a `recent_test_failure` `EvidenceItem` type with a fixed score of `9.0`. It SHALL NOT be added to `_NON_CORROBORATING_TYPES`.
- [ ] **TCLINK-DRE-002** [U]: For every path currently in `test_file_evidence` during `retrieve()`, THE SYSTEM SHALL traverse `TestFile <-[:EXECUTES]- TestExecution <-[:OCCURRED_IN]- TestFailure` and add one `recent_test_failure` evidence item per matching `TestFailure`, before the `covered_tests` filtering step (`docs/specs/diagnostic-retrieval-engine.md § DRE-TESTCOV-002`) runs — so a test file earning this evidence remains in `scored_candidates`/`relevant_tests` rather than moving to `covered_tests`.
- [ ] **TCLINK-DRE-003** [U]: `DebugPacket.recent_test_failures` (a new field, list of `RecentTestFailure`) SHALL be populated on both existing `DebugPacket` construction sites in `retrieve()` (the `"partial"` `on_progress` packet and the final returned packet).
- [ ] **TCLINK-DRE-004** [U]: `RecentTestFailure.explanation` SHALL be composed via a mechanical string template — no LLM call is made to produce it.
- [ ] **TCLINK-DRE-005** [U]: `recent_test_failure` evidence SHALL be scoped to the `TestFile` candidate only. It SHALL NOT add evidence to any source file.

---

## Failure Handling

- [ ] **TCLINK-ERR-001** [U]: A `TestFile` that does not yet exist at `TestExecution` write time SHALL result in no `EXECUTES` edge from the inline attempt (TCLINK-POLL-001), without raising, and SHALL remain eligible for the reconciliation sweep indefinitely (TCLINK-POLL-002) — not just for a bounded number of retries.
- [ ] **TCLINK-ERR-002** [U]: An `"unresolved"` result and an `"ambiguous"` result SHALL be logged distinctly from each other — never merged into one generic "no link" message.

---

## Scope Boundary

- [ ] **TCLINK-SCOPE-001** [U]: This component SHALL NOT create any `Investigation`, `InvestigationMilestone`, GitHub comment, or standing-query registration as a side effect of any node/edge write.
- [ ] **TCLINK-SCOPE-002** [U]: THE SYSTEM SHALL NOT set or read `TestFailure.is_current`/`latest_outcome` — `recent_test_failure` evidence fires on any linked `TestFailure` regardless of whether a later `TestExecution` attempt superseded it. "Currently failing" is not a claim this component makes.
- [ ] **TCLINK-SCOPE-003** [P]: WHEN a `TestFile` referenced by an already-written `EXECUTES` edge is later renamed or deleted (its old `repo_path` no longer has a `TestFile` node, or a new `TestFile` node exists at a different path for the same logical test), THE SYSTEM SHALL NOT modify, remove, or re-resolve the existing `EXECUTES` edge. This is a deliberate, documented limitation — `TestExecution`/`TestFailure` are historical records, and reconciling them against later `TestFile` changes is out of scope for this component (`docs/llds/test-coverage-ci-linking.md § Open Questions, item 5`). Falsified if a `TestFile` rename or deletion causes an existing `EXECUTES` edge to be silently altered.

---

## Open Questions & Future Decisions

Traced to `docs/llds/test-coverage-ci-linking.md § Open Questions & Future Decisions` — not independently spec'd, since none are behavioral requirements yet (`.modok/test-classname-map.yml` override config, verifying stagehand's actual CI `classname` shape against a real artifact, `is_current`/"currently failing" evidence, evidence propagation to source files). Two items are the exception, promoted to specs above precisely because they are testable, easy-to-accidentally-regress boundaries, not merely deferred features: the `TestFile` rename/deletion non-reconciliation item (`TCLINK-SCOPE-003`), and the deliberate choice to retry unresolved `TestExecution`s indefinitely rather than a bounded-attempts exclusion that was drafted and rejected as itself incorrect (`TCLINK-POLL-002`).
