# Test-Coverage CI Linking

## Context and Design Philosophy

See `docs/high-level-design.md § Test-Coverage CI Linking` and Key Design Decision #14. The goal: complete the distinction `docs/llds/diagnostic-retrieval-engine.md § Test Coverage (Informational)` already draws between "a test structurally covers this ticket's area" (informational, `covered_tests`) and "this test is evidence for this ticket" (scored) — by giving the DRE a mechanical way to detect the strongest possible version of the second claim: **a covering test that recently failed in CI**.

This closes an existing, explicitly-named gap. Both halves of the link already exist and are already ingested — `TestFile` nodes (from registered `test_files:` frontmatter, `docs/llds/ingestion-pipeline.md`) and `TestExecution`/`TestFailure` nodes (from Continuous CI Ingestion, `docs/llds/continuous-ci-ingestion.md`) — but nothing connects them. CI ingestion keys a test by `classname`/`test_name` (whatever the test runner's JUnit output says); the DRE keys a test by `repo_path`. This component is the missing translation between the two, and nothing else.

**Mechanical only, fails safe.** Per HLD Key Design Decision #14: `classname` → path derivation is a deterministic string transform, verified only against `TestFile` nodes that already exist. A `classname` that doesn't resolve produces no link — never a wrong one. No LLM, no per-project override config (deferred — see Open Questions), no code-map cross-referencing.

---

## classname → TestFile Resolution

### Grounding

Generated real JUnit XML from this project's own `pytest --junitxml` output (not assumed) to confirm the actual convention being derived against:

```xml
<testcase classname="tests.test_dependency_models" name="test_dependency_package_model_has_required_fields" />
<testcase classname="test_classy" name="test_module_level" />
<testcase classname="test_classy.TestSomething" name="test_a" />
```

Two shapes observed: a module-level test function's `classname` is the dotted module path with no extra segment (`tests.test_dependency_models` → `tests/test_dependency_models.py`); a class-grouped test's `classname` has one extra trailing segment that is a **class name, not a path segment** (`test_classy.TestSomething` — the real file is `test_classy.py`, not `test_classy/TestSomething.py`). A naive dot-to-slash transform is wrong for the second shape.

There is also a real, unverified-but-likely ambiguity for this project's actual target repo: stagehand has independent `client/` and `agent/` Python subprojects, each plausibly running `pytest` from its own subdirectory in CI — meaning a `classname` like `tests.test_output_consistency` may be missing the `client.`/`agent.` prefix a full repo-relative path would need. Not confirmed against a real stagehand CI artifact in this session (Phase 6 should verify against one); the algorithm below is designed to tolerate this ambiguity without needing that confirmation first.

### Algorithm

Given a `classname` string:

1. Split on `.` into `parts`.
2. Generate candidate paths from **most specific to least**: for `i` from `len(parts)` down to `1`, candidate `i` is `"/".join(parts[:i]) + ".py"`. This tries the full dotted path as a file path first, then progressively treats trailing segments as class names instead of path segments.
3. For each candidate, in that order, query registered `TestFile` nodes where `repo_path` **equals** the candidate, or — **only when the candidate has two or more path segments** — `repo_path` **ends with** `"/" + candidate` (tolerates a missing leading directory prefix — the stagehand `client/`/`agent/` case above). A single-segment candidate (e.g. `conftest.py`) is checked by exact match only, never suffix match: found during the edge-case probe that a short, generic candidate suffix-matching against an unrelated, deeply-nested file of the same name (`conftest.py` matching `agent/build/vendor/thirdparty/conftest.py`) is a real false-positive risk that gets *worse*, not better, the shorter the candidate is — and a single-segment collision produces exactly one match, so the existing ambiguity check (step 4) would not catch it.
4. Stop at the first candidate that matches at least one `TestFile`.
   - **Exactly one match**: resolved — this is the link target.
   - **More than one match** (e.g. two subprojects each have a same-named, same-relative-path test file): ambiguous — do not link, log distinctly (§ Failure Handling). Guessing between them would be exactly the kind of invented relationship this project's mechanical-linking discipline exists to prevent.
5. **No candidate ever matches**: no link. Safe, silent, expected for non-pytest `classname` shapes (e.g. this project's own C/C++ `agent/tests/` suite, whatever JUnit-conversion tool produces its `classname` values).

Steps 3–5 never invent a `TestFile` node — resolution only ever points at nodes that already exist, same discipline as `USES_DEPENDENCY`'s `DependencyPackage` gating (`docs/llds/dependency-graph-ingestion.md § File Usage Mapping`).

---

## Graph Model

New edge, no new node type:

```
TestExecution -[:EXECUTES]-> TestFile
```

`TestFailure` does not get its own direct edge to `TestFile` — a failure's file is reached by traversing its existing `TestFailure -[:OCCURRED_IN]-> TestExecution -[:EXECUTES]-> TestFile` path. Two edges saying the same thing would only add a place for them to drift; the existing `OCCURRED_IN` hop already exists and this is one more hop through it, not a parallel fact.

`idFrom` addressing is unchanged — `EXECUTES` is written via `write_edge_by_parts` between the `TestExecution`'s existing key (`"test-execution", project_slug, run_id, run_attempt, classname, test_name`) and the resolved `TestFile`'s existing key (`"test-file", project_slug, repo_path`).

One new property on the existing `TestExecution` node (no new node type, no `idFrom` key change): `link_state: str | None`, one of `"resolved"`, `"ambiguous"`, or unset. A no-match ("unresolved") result is never persisted to this property — it leaves `link_state` unset so the reconciliation sweep keeps retrying it (§ Where Resolution Runs). `resolve_test_execution_link`'s own return value distinguishes `"unresolved"` from `"ambiguous"` as a third, transient outcome (used for distinct logging, § Failure Handling) — the distinction exists in what gets *logged*, not in what gets *persisted*.

---

## Where Resolution Runs

**Inline, best-effort, at `TestExecution` write time.** `write_test_execution` (`src/modok/ingestion/ci_ingestion.py`) already writes the `RAN_IN` edge immediately after upserting the node; resolution is attempted right after, in the common case where the `TestFile` already exists (the usual case — code and its registered docs typically predate a given CI run).

**Reconciliation sweep, once per poll cycle per project**, mirroring `reconcile_commit_edges`/`reconcile_dependency_change_edges` (`docs/llds/continuous-ci-ingestion.md § Targeted vs. Tested Commit`, `docs/llds/dependency-graph-ingestion.md § Reconciliation`) — same precedent, same reason: a `TestFile` node can be registered *after* the `TestExecution` that should link to it (a doc's `test_files:` frontmatter gets added in a later commit than the CI run that already exercised that test). The sweep queries `TestExecution` nodes with `link_state IS NULL OR link_state = "ambiguous"` (i.e. not yet `"resolved"`) and retries resolution for each. Both a no-match and an ambiguous result are retried **every cycle, indefinitely** — same unbounded-retry shape as the two sibling sweeps this pattern mirrors, deliberately: see the cost caveat below for why a cheaper "give up after N attempts" alternative was considered and rejected.

Both call sites use the same resolution function — no duplicated logic between the inline and swept paths.

**Cost caveat, found during the edge-case probe — and a rejected fix, found during the same probe.** Unlike the `TARGETED_COMMIT`/`TESTED_COMMIT` and `DependencyChange`-edge reconciliation sweeps this pattern is modeled on — where an unresolved gap is expected to be transient and small — a `TestExecution` whose `classname` simply doesn't fit the pytest dotted-module-path convention (this project's own C/C++ `agent/tests/` suite, for one) will *never* resolve, and the sweep re-attempts it every cycle, forever, for as long as CI keeps producing more such executions.

An earlier version of this LLD "fixed" this by marking a `TestExecution` `"unresolved"` (permanently excluded from the sweep) after just its first reconciliation attempt. Found, on reflection during the same edge-case probe, to trade an unbounded-but-correct cost for a bounded-but-wrong result: a `TestFile` frequently *does* get registered well after the `TestExecution` that should link to it — the doc declaring it can be committed hours or days later — and one extra sweep attempt (seconds after the inline attempt already failed) is nowhere near long enough to distinguish "genuinely unresolvable" from "not registered yet." Marking it `"unresolved"` that fast would silently and permanently abandon resolution for the common, legitimate case this whole reconciliation mechanism exists to handle. A bounded-attempts-then-give-up model (mirroring `WorkflowRun.expansion_attempts`/`terminal_failure`, `docs/llds/continuous-ci-ingestion.md`) would fix the false-abandonment risk but needs its own attempt counter and a threshold long enough to span realistic registration lag — meaningfully more state for a problem neither sibling sweep in this codebase actually bothers solving. **Deferred, not fixed in v1** — see Open Questions.

---

## Diagnostic Retrieval Engine Integration

New evidence type: `recent_test_failure`, weight **9.0** — the same tier as `commit_message_match`, deliberately not higher or lower: like a commit message naming the exact ticket topic, an observed CI failure on a test that already covers the ticket's area is direct, concrete, non-inferential evidence, not a correlation. Corroborating (not added to `_NON_CORROBORATING_TYPES`).

New traversal, alongside the existing `_traverse_files_to_recent_dependency_changes`/`_traverse_files_to_recent_commits` calls in `retrieve()`: for every path currently in `test_file_evidence` (every discovered test file, whether reached via coverage or already carrying other evidence — not just `covered_tests_map` entries), traverse:

```cypher
MATCH (tf) WHERE id(tf) = idFrom('test-file', $project_slug, $path)
MATCH (tf)<-[:EXECUTES]-(te)<-[:OCCURRED_IN]-(failure)
RETURN failure, te
```

Each result adds one `recent_test_failure` `EvidenceItem` to that test file's evidence — **before** the `covered_tests` filtering step (`docs/specs/diagnostic-retrieval-engine.md § DRE-TESTCOV-002`) runs, so a test file that both covers the ticket's area and has this evidence stays in `scored_candidates`/`relevant_tests` on its own merit, the same mechanism that already promotes a covered test with `commit_message_match`/`ticket_mention` evidence — no new promotion logic, this is a new evidence *source* into an existing pipeline stage.

New `DebugPacket` field, mirroring `recent_dependency_changes`'s shape:

```python
@dataclass
class RecentTestFailure:
    test_path: str
    classname: str
    test_name: str
    run_id: str
    failure_type: str
    message: str
    observed_at: str
    explanation: str   # mechanical template, no LLM — same discipline as
                        # _format_dependency_change_explanation
```

Populated on both existing `DebugPacket` construction sites (`"partial"` and final), same pattern as every other DRE field added this arrow.

**Historical, not "currently failing."** `TestFailure` means "this failure was observed," not "this test is failing right now" (`docs/llds/continuous-ci-ingestion.md § Historical vs. Current Failure State` — `is_current`/`latest_outcome` are reserved, not implemented). This component inherits that limitation as-is: `recent_test_failure` evidence fires on any linked `TestFailure`, regardless of whether a later, passing `TestExecution` superseded it. Closing that gap is `continuous-ci-ingestion.md`'s own deferred work, not something this component's linking mechanism can or should paper over.

**Not propagated to source files.** A test's failure is evidence about *that test*, scoped to the `TestFile` candidate only — it does not additionally score whatever source file(s) the test presumably exercises. Guessing which source file a failing test implicates would be exactly the kind of inference this project's mechanical-linking discipline avoids; the existing `feature_primary_file`/`element_anchor_match`/`function_anchor_match` evidence on source files is unaffected and unrelated to this change.

---

## Failure Handling

| Condition | Behavior |
|---|---|
| `classname` resolves to zero `TestFile` candidates | No `EXECUTES` edge written; `link_state` left unset (retried every future reconciliation cycle indefinitely — § Where Resolution Runs, § Cost caveat). Not logged as an error — the common, expected case for non-pytest test runners. |
| `classname` resolves to more than one `TestFile` at the same candidate specificity | No `EXECUTES` edge written; `link_state = "ambiguous"` (also retried every reconciliation cycle). Logged distinctly from the zero-match case — an operator investigating "why isn't this test linking" needs to tell "nothing matched" from "two things matched and we refused to guess" apart. |
| `TestFile` doesn't exist yet at `TestExecution` write time | Silently skipped inline; picked up by the reconciliation sweep once the `TestFile` is registered. |
| Reconciliation sweep itself fails | Isolated in its own `try`/`except` in the poll cycle, same as every other per-cycle step (`docs/llds/continuous-ci-ingestion.md`, `docs/llds/dependency-graph-ingestion.md § Polling and Checkpoint Behavior`) — does not block issue/PR sync, CI ingestion's other steps, or dependency ingestion in the same cycle. |

---

## Testable Non-Goals

- No `.modok/test-classname-map.yml` override config in v1 — deferred per HLD Key Design Decision #14; nothing yet demonstrates the mechanical case is insufficient.
- No code-map symbol cross-referencing for disambiguation — the candidate-generation + existence-check algorithm above is the whole mechanism.
- No support for non-pytest `classname` conventions (Jest-with-JUnit-reporter, C/C++ test-to-JUnit converters, ...) — `docs/llds/continuous-ci-ingestion.md`'s own "JUnit XML dialect variance" open question already covers ingesting *these* runners' results at all; this component only adds `classname`-based linking for the pytest dotted-module-path shape.
- No "is this test currently failing" claim — inherits `TestFailure`'s existing historical-only semantics unchanged.
- No evidence propagation from a failing test to the source file(s) it tests.
- No new standing query, incident, or GitHub comment — this is graph-fact + retrieval-evidence work only, same boundary every other arrow in this project draws.

---

## Decisions & Alternatives

| Decision | Chosen | Alternatives Considered | Rationale |
|---|---|---|---|
| Resolution algorithm | Progressive trailing-segment trimming + existence-check against known `TestFile`s, both exact and suffix match | Naive dot-to-slash only; config-mapped overrides; code-map cross-referencing | Naive dot-to-slash breaks on class-grouped tests (real, observed shape); the other two are real options but premature — see HLD Key Design Decision #14 |
| Ambiguous match handling | No link, logged distinctly from zero-match | Pick the first/shortest candidate; pick by additional heuristic (e.g. most-recently-modified file) | Guessing between two equally-valid candidates is exactly the invented-relationship risk mechanical linking exists to avoid; a silent wrong link is worse than a visible non-link |
| `EXECUTES` on `TestExecution`, not `TestFailure` | One edge, traversed through the existing `OCCURRED_IN` hop | A second, direct `TestFailure -> TestFile` edge | Avoids two edges that could independently drift; `OCCURRED_IN` already exists for exactly this purpose |
| `recent_test_failure` weight | 9.0, same tier as `commit_message_match` | Higher than any existing type (treat as decisive); lower, treat as merely corroborating like `recent_commit` | A concrete observed failure on a covering test is direct evidence, not a correlation — deserves the same tier as the project's other "this specifically names the ticket's topic" signal, not a new, untested tier above everything else |
| Resolution timing | Inline best-effort + periodic reconciliation sweep | Inline only | Same ordering problem CI ingestion's own `TARGETED_COMMIT`/`TESTED_COMMIT` and dependency ingestion's `INTRODUCED_BY`/`MERGED_VIA` already hit and fixed — a `TestFile` can be registered after the `TestExecution` that should link to it |
| Reconciliation sweep retries unresolved executions forever, unbounded | Matches the unmodified `TARGETED_COMMIT`/`DependencyChange` sweep pattern exactly — no new give-up state | A `link_state = "unresolved"` terminal marker, excluding an execution from the sweep after its first (or Nth) failed attempt | Drafted, then reverted, during the edge-case probe: excluding after just one attempt risks permanently abandoning resolution for a `TestFile` that legitimately gets registered hours or days later — the common case this sweep exists to handle. A correct bounded-attempts version (mirroring `WorkflowRun.expansion_attempts`/`terminal_failure`) needs its own counter and a threshold long enough to span realistic registration lag — real added state for a cost problem neither sibling sweep in this codebase currently solves either. Deferred, not fixed, in v1 |
| Suffix (missing-prefix) matching gated on candidate specificity | Only attempted for candidates with 2+ path segments; single-segment candidates require an exact match | Suffix-match every candidate, including bare filenames | Found during the edge-case probe: a short, generic single-segment candidate (`conftest.py`) risks an accidental exact-count-one suffix match against an unrelated, deeply-nested file of the same name — a false positive the ambiguity check (step 4) cannot catch, since it produces exactly one match |

## Open Questions & Future Decisions

### Deferred

1. **`.modok/test-classname-map.yml` override config** — for ecosystems whose `classname` doesn't fit the pytest dotted-module-path convention. Same shape as `dependency-map.yml`'s `import_overrides`. Not built until the mechanical-only case is demonstrated insufficient on a real project.
2. **Verify stagehand's actual CI `classname` shape** — this LLD's suffix-matching tolerance for a missing leading directory prefix (`client.`/`agent.`) is reasoned from stagehand's subproject layout, not confirmed against a real workflow artifact. Worth a direct check in Phase 6 against an actual CI-produced JUnit XML from this repo, not just a locally-generated one.
3. **`is_current`/"currently failing" evidence** — blocked on `continuous-ci-ingestion.md`'s own deferred `TestFailure` reconciliation (§ Historical vs. Current Failure State), not on anything in this component.
4. **Evidence propagation to source files a failing test exercises** — deliberately out of scope; would require inferring source-to-test relationships beyond what `HAS_TEST`/mirrored-path coverage already provides.
5. **A `TestFile` rename/deletion after an `EXECUTES` edge was written is not reconciled.** Found during the edge-case probe: `EXECUTES` is written additively (`write_edge_by_parts`), never replaced, so an edge resolved against a since-renamed or since-deleted `TestFile` becomes stale. Treated as acceptable for now — `TestExecution`/`TestFailure` are historical records of a past run, and "this classname resolved to this file at the time the run happened" remains a true statement even if the file has since moved; retroactively re-resolving historical executions when a `TestFile` changes is a materially bigger sweep (over `TestFile` changes, not just new `TestExecution`s) than this component's stated scope. Revisit if stale `EXECUTES` edges are observed causing real confusion in practice.
6. **Unbounded reconciliation-sweep cost for structurally-unresolvable executions.** A `TestExecution` whose `classname` will never fit the pytest convention (this project's own C/C++ `agent/tests/` suite) is retried every cycle, forever — real, unbounded cost with no possible benefit for that specific execution. Found during the edge-case probe, along with a bounded-attempts fix that was drafted and then rejected as itself incorrect (§ Where Resolution Runs § Cost caveat — it risked permanently abandoning resolution for a `TestFile` registered days after its `TestExecution`). A correct fix needs an attempt counter and a threshold deliberately long enough to span realistic `TestFile` registration lag, not just CI-cycle timing — deferred until this is observed as a real operational problem on an actual project, not solved speculatively now.

## References

- `docs/high-level-design.md § Test-Coverage CI Linking, Key Design Decision #14` — why this exists and why mechanical-only was chosen
- `docs/llds/continuous-ci-ingestion.md § New Node Types, § Targeted vs. Tested Commit, § Historical vs. Current Failure State` — `TestExecution`/`TestFailure` identity, the reconciliation-sweep precedent, and the historical-only semantics this component inherits
- `docs/llds/diagnostic-retrieval-engine.md § Test Coverage (Informational), § Evidence Sources` — `covered_tests`/`test_file_evidence` mechanism this component's evidence feeds into, and where the "found live, rocketmark/stagehand#31" demotion this component completes originated
- `docs/llds/dependency-graph-ingestion.md § Reconciliation, § File Usage Mapping` — the reconciliation-sweep and never-invent-a-node precedents this component follows most directly
- `docs/llds/quine-client.md` — `write_edge_by_parts`/`node_exists_by_parts` primitives reused here
