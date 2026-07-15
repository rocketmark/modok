# Standing Queries Specs

Specs for the standing-query detection path: the `actionable-issue-pattern` Quine standing query, mechanical anchor linking, the `Investigation` node, the `POST /standing-query/result` route, GitHub write-back, and the `GitHubPollAdapter`.

LLD: `docs/llds/standing-queries.md`

---

## Test Level Convention

- **[U]** — Unit test. At least one `@spec`-annotated test directly exercises the specified behavior with mocked dependencies (`DummyQuine` where the Cypher fingerprint is simulatable, otherwise a mocked `httpx` transport).
- **[P]** — Property test (`hypothesis`). The invariant must hold across arbitrary inputs, not just handpicked examples.
- **[C]** — Contract test. Runs against a live local Quine instance (`~/.modok/quine.jar`, v1.10.0). Applied to specs whose correctness depends on Quine's actual standing-query wire behavior, which `DummyQuine`'s Cypher-fingerprint dispatch cannot simulate.

Levels are cumulative: `[P]` implies `[U]`; `[C]` implies `[U]`.

---

## Standing Query Definition

- [x] **SQ-DEF-001** [U]: The system shall load standing query definitions from checked-in YAML artifacts under `src/modok/quine/standing_queries/`, never as inline Python strings.
- [x] **SQ-DEF-002** [U]: The `actionable-issue-pattern` definition shall use `DistinctId` mode with pattern `MATCH (ci:CustomerIssue)-[:HAS_ERROR]->(e:ErrorSignature)<-[:HAS_ERROR]-(ki:KnownIssue)-[:RESOLVED_BY]->(fix:Fix) RETURN DISTINCT id(ci)`.
- [x] **SQ-DEF-003** [U]: The `actionable-issue-pattern` definition's pattern shall not filter on `project_slug` — project isolation is provided by `idFrom()` node-address topology (`docs/llds/quine-client.md § ID Scheme`), under which a `CustomerIssue` in one project can never share an `ErrorSignature`, `KnownIssue`, or `Fix` node with another project.
- [x] **SQ-DEF-004** [C]: The `actionable-issue-pattern` definition shall filter each of its four bound nodes by a `WHERE ... node_type = '...'` equality check rather than Cypher `:Label` syntax, and its `RETURN` clause shall alias the matched id as `RETURN DISTINCT id(ci) AS id` — both confirmed required by live verification against Quine 1.10.0 (`node_type` is a property, not a real label; an unaliased `id(ci)` produces a result-field key of `id(ci)`, not `id`, breaking the `$that.data.id` reference in the enrichment stage).
- [x] **SQ-DEF-005** [U, C]: The `new-bug-report-pattern` definition shall use `DistinctId` mode with pattern `MATCH (ci) WHERE ci.node_type = 'CustomerIssue' AND ci.ticket_kind = 'bug' RETURN DISTINCT id(ci) AS id` — firing on a `CustomerIssue` alone, independent of any `HAS_ERROR`/`KnownIssue`/`Fix` evidence. Confirmed live that combining this condition with `actionable-issue-pattern`'s via a single `OPTIONAL MATCH ... WHERE ... OR ...` query silently breaks node_type filtering against real Quine (returned thousands of unrelated nodes instead of the matching few) — this is therefore a separate standing query rather than a broadened single pattern.
- [x] **SQ-DEF-006** [U, C]: The `error-flagged-pattern` definition shall use `DistinctId` mode with pattern `MATCH (ci)-[:HAS_ERROR]->(e) WHERE ci.node_type = 'CustomerIssue' AND e.node_type = 'ErrorSignature' RETURN DISTINCT id(ci) AS id` — firing on any `HAS_ERROR` match to an `ErrorSignature`, whether or not that signature is linked to a `KnownIssue`/`Fix` (unlike `actionable-issue-pattern`, which requires the full chain).
- [x] **SQ-DEF-007** [U, C]: Every standing query definition's `enrichment_query` shall return its own name as a literal `AS standing_query_name` field (e.g. `'new-bug-report-pattern' AS standing_query_name`), never relying on `/standing-query/result`'s fallback default. Found live: that default (`"actionable-issue-pattern"`, kept for backward compatibility) silently mislabeled a real `new-bug-report-pattern` match before this was added, since its enrichment query returned no `standing_query_name` field at all.

---

## `QuineClient` Standing Query Methods

- [x] **SQ-CLIENT-001** [C]: `standing_query_exists(name)` shall return `True` if a standing query with that name is registered in Quine (`GET /api/v1/query/standing/{name}` succeeds) and `False` if it is not registered (404).
- [x] **SQ-CLIENT-002** [U, C]: When `install_standing_query` is called for a name `standing_query_exists` reports absent, the system shall register it via `POST /api/v1/query/standing/{name}` — pattern, mode, and a `CypherQuery`-then-`PostToEndpoint` output built from the definition's `enrichment_query` and the supplied `callback_url` — and return `True`.
- [x] **SQ-CLIENT-003** [U, C]: When `install_standing_query` is called for a name that already exists, the system shall send no request to Quine and return `False`.
- [x] **SQ-CLIENT-004** [C]: `list_standing_queries()` shall return the names of all standing queries currently registered in Quine via `GET /api/v1/query/standing`.
- [x] **SQ-CLIENT-005** [U, C]: When `remove_standing_query(name)` is called for a name that exists, the system shall delete it via `DELETE /api/v1/query/standing/{name}` and return `True`; if it does not exist, the system shall send no request and return `False`.

---

## CLI (`modok stream`)

- [x] **SQ-CLI-001** [U]: `modok stream install` shall call `install_standing_query` for every definition returned by `all_definitions()` and report, per definition, whether it was newly installed or already present.
- [x] **SQ-CLI-002** [U]: `modok stream install`, `modok stream status`, and `modok stream remove` shall not accept a `--project` option (SQ-DEF-003 — standing queries are Quine-instance-level, not per-project).
- [x] **SQ-CLI-003** [U]: `modok stream status` shall print the names returned by `list_standing_queries()`.
- [x] **SQ-CLI-004** [U]: `modok stream remove` shall call `remove_standing_query` for each checked-in definition's name and report, per definition, whether it was removed or was not present.
- [x] **SQ-CLI-005** [U]: If Quine is unreachable, `modok stream install`/`status`/`remove` shall exit non-zero with a message naming the configured Quine URL, matching the convention `modok quine start` already uses for the same failure.

---

## Mechanical Anchor Linking

- [x] **SQ-ANCH-001** [U]: When a `CustomerIssue` node is written with non-empty `raw_text` (write-time mechanical linking — distinct from, and does not replace, the Diagnostic Retrieval Engine's independent read-time LLM anchor-extraction fallback in `docs/llds/diagnostic-retrieval-engine.md`), the system shall word-boundary match `raw_text` (case-insensitive) against every `normalized_error` value in the project's `errors.yml` registry.
- [x] **SQ-ANCH-002** [U]: For each `normalized_error` match found (SQ-ANCH-001), the system shall treat it as a candidate `HAS_ERROR` target only if an `ErrorSignature` node with that `normalized_error` already exists in the graph; the system shall never create an `ErrorSignature` node from this step.
- [x] **SQ-ANCH-003** [U]: The system shall compute the full current set of matched `ErrorSignature` targets for a `CustomerIssue` write and call `replace_edges` once for its outbound `HAS_ERROR` edges, rather than writing individual matches additively.
- [x] **SQ-ANCH-004** [U]: If `raw_text` is `None` or empty, the system shall perform no error or feature anchor matching (SQ-ANCH-001, SQ-ANCH-008) and write no `HAS_ERROR` or `AFFECTS` edges for that `CustomerIssue`.
- [x] **SQ-ANCH-005** [U]: If the project's registries cannot be loaded (`RegistryNotFoundError`), the system shall log a warning and continue for both error anchor matching (SQ-ANCH-001) and feature anchor matching (SQ-ANCH-008); the `CustomerIssue` node write shall not fail because anchor linking could not run.
- [x] **SQ-ANCH-006** [U]: Every code path that writes a `CustomerIssue` node — the webhook `customer_issue` ingest branch (covering the push adapters and the poll adapter, both of which call `on_event`), `GithubIngester.ingest_issue` (covering batch `ingest-github`), and `_ingest_customer_ticket` (covering `modok ingest <ticket_file>`, see `CLI-INGEST-010`) — shall invoke mechanical anchor linking (both error and feature) immediately after the node write.
- [x] **SQ-ANCH-007** [U]: If resolving the calling project's `repo_root` (to load its registries) fails for any reason — no project config found, config file absent, or any other exception — the `customer_issue` ingest branch shall log a warning and continue; the `CustomerIssue` node write itself shall already have completed and shall not be affected.
- [x] **SQ-ANCH-008** [U]: When a `CustomerIssue` node is written with non-empty `raw_text`, the system shall tokenize `raw_text` (word extraction, then camelCase/snake_case/kebab-case splitting into lowercase tokens of length > 2 characters, excluding common English stopwords per `docs/specs/diagnostic-retrieval-engine.md § DRE-TOKEN-004`) and check for token overlap against the same tokenization of every registered `Feature`'s slug and name in the project's `features.yml` registry.
- [x] **SQ-ANCH-009** [U]: For each `Feature` token-overlap match found (SQ-ANCH-008), the system shall treat it as a candidate `AFFECTS` target only if a `Feature` node with that slug already exists in the graph; the system shall never create a `Feature` node from this step.
- [x] **SQ-ANCH-010** [U]: The system shall compute the full current set of matched `Feature` targets for a `CustomerIssue` write and call `replace_edges` once for its outbound `AFFECTS` edges, rather than writing individual matches additively.

---

## LLM Fallback Anchor Classification

- [x] **SQ-LLMANCH-001** [U]: When a `CustomerIssue` node is written with non-empty `raw_text`, and both mechanical error anchor linking (SQ-ANCH-001) and mechanical feature anchor linking (SQ-ANCH-008) find zero matches, the system shall call the LLM Gateway's `parse_ticket` with the project's registry context (`feature_slugs`, `module_slugs`, `valid_slugs`, `feature_descriptions`, `module_descriptions`, `module_elements`, `module_source_files`) before ingestion of that `CustomerIssue` completes.
- [x] **SQ-LLMANCH-002** [U]: If either mechanical error anchor linking or mechanical feature anchor linking finds at least one match, the system shall not call `parse_ticket` for that `CustomerIssue` — mechanical/graph evidence is always preferred over an LLM call, mirroring the Diagnostic Retrieval Engine's existing read-time precedent.
- [x] **SQ-LLMANCH-003** [U]: From a successful `parse_ticket` result, the system shall write `AFFECTS` edges only for feature slugs that are present in the project's `feature_slugs()` registry (not `module_slugs()`) and for which a `Feature` node already exists in the graph.
- [x] **SQ-LLMANCH-004** [U]: From a successful `parse_ticket` result, the system shall write `HAS_ERROR` edges only for error signatures that match a `normalized_error` value in the project's `errors.yml` registry and for which an `ErrorSignature` node already exists in the graph.
- [x] **SQ-LLMANCH-005** [U]: The system shall call `replace_edges` once per edge type (`AFFECTS`, `HAS_ERROR`) with the full validated set of matches from a single `parse_ticket` call, including when that set is empty.
- [x] **SQ-LLMANCH-006** [U]: If `parse_ticket` raises `LLMUnavailableError` or `LLMGatewayError`, the system shall log the failure and write no `AFFECTS` or `HAS_ERROR` edges from this step; the `CustomerIssue` node write shall not fail or roll back.
- [x] **SQ-LLMANCH-007** [U]: If the project's registries cannot be loaded (`RegistryNotFoundError`), the system shall log a warning and skip LLM fallback classification entirely, without calling `parse_ticket`.
- [D] **SQ-LLMANCH-008**: Classifying a `CustomerIssue` as `ticket_kind` (bug vs. feature-request) is deferred — no field, mechanical or LLM-derived, records this in the current increment.

---

## `Investigation` Node and Deduplication

- [x] **SQ-INV-001** [U]: When a standing-query match result is received naming `project_slug`, `source_system`, `ticket_id`, and the firing standing query's name — plus `known_issue_id`/`fix_id` when the firing pattern provides them (`actionable-issue-pattern`); `new-bug-report-pattern` and `error-flagged-pattern` do not, and those two positions are empty strings — the system shall compute a deterministic `investigation_id` from exactly those values.
- [x] **SQ-INV-002** [U]: The system shall address `Investigation` nodes with `idFrom('investigation', project_slug, investigation_id)`.
- [x] **SQ-INV-003** [U]: If an `Investigation` node already exists at the computed address, the system shall not re-upsert it, shall not call the Diagnostic Retrieval Engine, and shall not attempt a GitHub write-back for that match. This existence check shall be performed via `node_exists_by_parts` (`docs/specs/quine-client.md § QC-NR-004`), never a Python-computed `idFrom()` value — the latter is not a valid Quine node ID and would make this check permanently return "not found" regardless of whether a matching `Investigation` actually exists (found live; see `docs/llds/quine-client.md`).
- [x] **SQ-INV-004** [U]: If no `Investigation` node exists at the computed address, the system shall upsert one with `status="open"`, `trigger_type="standing_query"`, `triggered_at` set to the current time, and the firing standing query's name, then write `Investigation -[:INVESTIGATES]-> CustomerIssue`.
- [x] **SQ-INV-005** [P]: Two standing-query match deliveries carrying identical `project_slug`/`source_system`/`ticket_id`/`known_issue_id`/`fix_id`/standing-query-name shall never result in more than one `Investigation` node, regardless of delivery order or repetition.
- [x] **SQ-INV-006** [U]: When a single `CustomerIssue` matches the standing query pattern via more than one distinct `(known_issue_id, fix_id)` combination, the system shall create a separate `Investigation` node per combination, each with its own `investigation_id`.

---

## Standing Query Result Route

- [x] **SQ-ROUTE-001** [U]: The system shall expose `POST /standing-query/result`, accepting either a single match object or a JSON array of match objects in the request body.
- [x] **SQ-ROUTE-002** [U]: For each match object, if its `project_slug` is not a known configured project, the system shall respond 404 for that object; if `known_project_slugs` is `None` (unconfigured), every `project_slug` shall be accepted, mirroring `WH-ROUTE-001`.
- [x] **SQ-ROUTE-003** [U]: If a match object is missing any of `project_slug`, `source_system`, `ticket_id`, the system shall respond 400 for that object without writing to Quine. `known_issue_id`/`fix_id` are optional — absent when the match came from `new-bug-report-pattern` or `error-flagged-pattern` (SQ-DEF-005, SQ-DEF-006) — and default to `""` when missing.
- [x] **SQ-ROUTE-004** [U]: For each valid match object, the system shall construct an `IngestEvent(kind="investigation", ...)` and process it through the same `run_ingest_event` path used by other adapters.
- [x] **SQ-ROUTE-005** [U]: The route shall require no request authentication (no HMAC or bearer check) — it is reachable only from the co-located Quine instance in MODOK's single-user, trusted-deployment model (HLD Non-Goals).
- [x] **SQ-ROUTE-006** [U, C]: If the request body (or an element of it, when a list) is of the shape `{"meta": {...}, "data": {...}}`, the route shall process the `data` object as the match — confirmed live that Quine 1.10.0's default `PostToEndpoint` output structure wraps the enrichment row this way rather than posting it flat. A flat body (no envelope) shall continue to work unchanged.

---

## GitHub Write-Back

- [x] **SQ-GH-001** [U]: When an `Investigation` is newly recorded (SQ-INV-004) for a `CustomerIssue` whose `source_system` is `"github"`, and both the project's `github_repo` and the environment's `GITHUB_TOKEN` are present, the system shall call the Diagnostic Retrieval Engine's `retrieve()` for that `CustomerIssue` and post the resulting debug packet, formatted as markdown, as a comment on the originating GitHub issue.
- [x] **SQ-GH-002** [U]: The markdown comment shall be the **full** debug packet — the same content `ui/src/components/modok/DebugPacketView.tsx` renders in the demo app, not a subset. It shall include the firing standing query's name, the packet's summary (LLM-generated, or its `issue.summary` fallback per `docs/llds/diagnostic-retrieval-engine.md § LLM Summary`), anchors (features/errors/symptoms), affected areas, scored candidates ("Top suspects" — path, confidence, score, and evidence breakdown, in ranked order), known issues, prior fixes, relevant files, relevant tests, and recent commits — each section omitted when its underlying list is empty — and the `investigation_id`. `retrieve()` already computes all of this; the earlier version of this formatter silently rendered only a subset (summary, known issues, prior fixes, relevant files, relevant tests), discarding anchors/affected-areas/scored-candidates/recent-commits even though they were already present on the packet — found live when the GitHub comment for a real, over-matched ticket showed a flat, unranked file list with no way to tell a strong match from a weak one.
- [x] **SQ-GH-003** [U]: If `github_repo` or `GITHUB_TOKEN` is not configured for the project, the system shall skip the GitHub write-back without error; the `Investigation` node write is unaffected.
- [x] **SQ-GH-004** [U]: If the GitHub comment API call fails (non-2xx response or exception), the system shall log the failure and shall not roll back the `Investigation` node or its `INVESTIGATES` edge.
- [x] **SQ-GH-005** [U]: Before calling `retrieve()`, the system shall resolve the `CustomerIssue`'s real Quine node ID via a property-match query (`node_type`, `project_slug`, `source_system`, `ticket_id`) rather than a Python-computed `idFrom()` value — the `CustomerIssue` was addressed at write time via Quine's own `idFrom()` embedded in Cypher (`upsert_node`), so no Python-side function can independently compute its real ID (found live — this was silently making every GitHub write-back fail with `DRENotFoundError`, swallowed by SQ-GH-004, so no comment was ever posted regardless of configuration).
- [x] **SQ-GH-006** [U]: In the "Top suspects" section, `scored_candidates` whose evidence includes a `doc_penalty` item shall be collapsed into a single grouped `[LOW]` summary line (count, no per-file evidence breakdown, no score) followed by each file's path on its own indented line — not a comma-separated inline list, so paths stay individually readable — rather than one full entry per file. Non-doc-penalized candidates are rendered individually as before, in ranked order, ahead of the grouped line. Found live: a feature reachable from several modules could surface 10+ near-identical low-scoring doc/config files (LLDs, specs, arrow docs, systemd unit files) as separate "Top suspects" entries, burying the handful of genuinely scored source/test candidates in noise; an initial comma-separated single-line version of the grouped fallback was itself hard to read once the count grew past a handful.
- [x] **SQ-GH-008** [U]: In the "Top suspects" section, `scored_candidates` of `kind == "test"` whose evidence is *solely* a single `test_coverage` item (no other evidence type) shall be collapsed into a single grouped `[LOW]` line, same one-path-per-line format as `SQ-GH-006`'s doc-penalty grouping, rendered after the individually-listed candidates and before the doc-penalty group. A test file carrying any additional evidence (e.g. `ticket_mention`, `recent_commit`, `function_anchor_match`) is more specific and shall remain listed individually — only the undifferentiated "this test merely covers the feature" case is grouped. Found live: a feature spanning several modules surfaced 5+ test files, each with identical single-item evidence and identical scores, as separate "Top suspects" entries — the same noise problem `SQ-GH-006` fixed for doc files.
- [x] **SQ-GH-009** [U]: For an individually-listed candidate (not grouped by `SQ-GH-006`/`SQ-GH-008`), evidence items carrying a `commit_sha` (`recent_commit`, `commit_message_match`, `function_anchor_match`) shall be grouped under one `- Recent commit {sha}:` header per distinct commit, with each signal found for that commit as an indented sub-bullet (`Touched` / `Commit message: {text}` / `Function match: {names}`, with the commit SHA suffix stripped from the sub-bullet text since it's already the group header). The SHA in the header line shall be bare text, not wrapped in an inline code span — GitHub auto-links a bare commit SHA to the commit within the same repo, and wrapping it in backticks suppresses that auto-link (found live: an initial version of this grouping wrapped the SHA in backticks for visual consistency with other code references, which silently broke the commit links that existed before this grouping was introduced). Commit groups shall be sorted by number of distinct signals descending — a commit that is both recently-touched *and* has a matching message or matching function is a stronger "look here first" signal than one that is merely recent, and shall sort ahead of it regardless of which commit is more recent. Non-commit evidence (`feature_primary_file`, `element_anchor_match`, `ticket_mention`, etc.) renders as flat bullets before any commit groups, unchanged from before.
- [x] **SQ-GH-007** [U]: For each GitHub write-back, the system shall post **two** separate comments rather than one: (1) a "triggered" comment, posted immediately (before calling `retrieve()`), containing the standing query name and a summary from `quick_investigation_summary` (`docs/specs/diagnostic-retrieval-engine.md § Quick Investigation Summary`); (2) a "results" comment, posted after `retrieve()` completes, containing the full debug packet per `SQ-GH-002` (header text changed from "investigation triggered" to "investigation results" to distinguish the two). If the triggered comment's summary generation fails, it shall still post with an empty summary rather than being skipped. If posting the triggered comment fails, the system shall still proceed to call `retrieve()` and attempt to post the results comment — the two comments are independent best-effort attempts, not a single atomic unit. Found live: a full `retrieve()` call can take several minutes (traversal, scoring, and the summary LLM call), during which the reporter previously saw no acknowledgment at all that MODOK was working on their ticket.

---

## GitHub Poll Adapter

- [x] **SQ-POLL-001** [U]: The system shall provide a `GitHubPollAdapter` implementing the existing `PullAdapter` protocol (`docs/specs/webhook-receiver.md`) with no changes to that protocol's signature.
- [x] **SQ-POLL-002** [U]: While `WebhookConfig.github_poll_enabled` is `True`, the adapter shall, for every configured project with a non-empty `github_repo`, call `GithubIngester.run(since=last_github_sync)` every `github_poll_interval_seconds` seconds (default 30) and persist the resulting `last_github_sync`.
- [x] **SQ-POLL-003** [U]: While `WebhookConfig.github_poll_enabled` is `False` (the default), the adapter shall not poll any project.
- [x] **SQ-POLL-004** [U]: A project with no `github_repo` configured shall be silently skipped by the poll loop without any log output — most MODOK projects aren't GitHub-backed, so this is the expected common case, not a signal worth surfacing every cycle. A project that *does* have `github_repo` configured but `GITHUB_TOKEN` unset in the environment shall instead be skipped with a one-line warning to stderr naming the project — a configured-but-tokenless project is more likely a real misconfiguration (e.g. a fresh shell that lost an exported token) than an absent GitHub integration, and silent skipping of this case previously made poll failures indistinguishable from a healthy idle poller.
- [x] **SQ-POLL-005** [U]: `stop()` shall cancel and await the adapter's background polling task(s) without raising.
- [x] **SQ-POLL-006** [U]: On successful completion of `GithubIngester.run()` for a project, the adapter shall print a one-line summary to stdout naming the project slug and the number of issues and PRs synced, so a running `modok serve` shows visible evidence of each poll cycle instead of producing output only on failure.

---

## References

- `docs/high-level-design.md § Detection / Trigger Path`, `§ Key Design Decisions #10`
- `docs/llds/standing-queries.md`
- `docs/specs/quine-client.md` — `idFrom()` scheme, `replace_edges`, connection/retry
- `docs/specs/diagnostic-retrieval-engine.md` — `retrieve()`, reused unchanged
- `docs/specs/webhook-receiver.md` — `PullAdapter`/`PushAdapter` protocols, `WH-ROUTE-001`
- `docs/specs/github-ingestion.md` — `GithubIngester`, reused unchanged except the SQ-ANCH-006 call site
- `docs/specs/ingestion-pipeline.md` — `SI-BLOCK-004/005/006`, the known-issue block fields this path depends on
- `src/modok/text_utils.py` — shared `tokenize`/`extract_text_tokens` helpers used by both `SQ-ANCH-008` (mechanical feature linking) and the DRE's `_pre_match_modules` (`docs/specs/diagnostic-retrieval-engine.md`)
