# Standing Queries

## Context and Design Philosophy

Every other component in MODOK is caller-initiated: a CLI command, an API route, or a webhook delivery triggers a read or a write. This component is different — it is the one place where Quine itself initiates behavior, the moment accumulated graph evidence satisfies a registered pattern. See `docs/high-level-design.md § Detection / Trigger Path` for how this fits the rest of the system, and `§ Key Design Decisions #10` for why standing queries were chosen over polling and over MODOK-side enrichment.

Three principles govern this component:

**Detection is mechanical; enrichment is layered.** The standing query pattern itself, and the `CypherQuery` stage that enriches its match inside Quine, never touch an LLM. The one LLM call in this whole path — the Diagnostic Retrieval Engine's `summarise_packet` — runs strictly *after* the match is settled, and only to produce prose for a field humans read. If it fails, the match, the `Investigation` node, and the write-back all still happen; only the prose degrades to `issue.summary`.

**Every trigger is explainable.** An `Investigation` node never appears without a traceable cause: which standing query fired, which `CustomerIssue` it investigates, and (via the Diagnostic Retrieval Engine, called fresh at write-back time) which evidence composed the packet.

**A small, fixed, maintained set of patterns — not a query-authoring surface.** Standing queries live as YAML artifacts under version control (`src/modok/quine/standing_queries/`), not as strings embedded in Python or accepted at runtime from a caller. `modok stream install` installs the checked-in set; it does not take an arbitrary Cypher pattern as an argument. See HLD Non-Goals.

## Component Map

```
CustomerIssue write (webhook push, GitHub poll, or GitHub batch ingest)
        │
        ▼
Mechanical Anchor Linking  ──► CustomerIssue -[:HAS_ERROR]-> ErrorSignature
        │                  ──► CustomerIssue -[:AFFECTS]-> Feature
        │
        ▼ (only if BOTH of the above found nothing)
LLM Fallback Anchor Classification (parse_ticket, validated, write-back)
        │                  ──► CustomerIssue -[:HAS_ERROR]-> ErrorSignature
        │                  ──► CustomerIssue -[:AFFECTS]-> Feature
        │
        ▼
Quine Memory Graph  ──(incremental evaluation, every write)──►  Standing Query
                                                                       │
                                                          CypherQuery enrichment (andThen)
                                                                       │
                                                                       ▼
                                                              PostToEndpoint
                                                                       │
                                                                       ▼
                                                    POST /standing-query/result  (MODOK)
                                                                       │
                                              ┌────────────────────────┴───────────────────────┐
                                              ▼                                                  ▼
                                  writes Investigation node                          calls Diagnostic Retrieval
                                  + INVESTIGATES edge (idempotent)                    Engine → debug packet
                                                                                                   │
                                                                                                   ▼
                                                                              if source_system == "github":
                                                                              posts packet as a GitHub issue comment
```

## Standing Query Definition

Standing query definitions are YAML artifacts, one file per query, under `src/modok/quine/standing_queries/`. Each is loaded by name, never hand-built as an inline string.

```
src/modok/quine/standing_queries/
    __init__.py
    loader.py                       # load_definition(name) -> StandingQueryDefinition
    actionable_issue_pattern.yaml   # known, already-fixed defect rediscovered
    new_bug_report_pattern.yaml     # any ticket_kind='bug' CustomerIssue
    error_flagged_pattern.yaml      # any CustomerIssue with an error anchor
```

Three patterns exist because they represent three genuinely different strengths of evidence, not three ways of saying the same thing:

| Pattern | Fires when | Strength |
|---|---|---|
| `actionable-issue-pattern` | Full chain: error anchor traces to a `KnownIssue` that already has a `Fix` | Strongest — this is *definitely* the same defect as something already resolved |
| `error-flagged-pattern` | `CustomerIssue` has any `HAS_ERROR` anchor at all | Medium — a real, identifiable error signature, but not (yet, or ever) matched to a known fix |
| `new-bug-report-pattern` | `CustomerIssue.ticket_kind == 'bug'` | Broadest — the reporter said it's a bug (via GitHub label, `docs/llds/github-ingestion.md § Ticket Kind from Labels`); no anchor required at all |

All three post a debug-packet comment via the same downstream machinery (`run_ingest_event`'s `investigation` branch, `_maybe_notify_github`) — they differ only in *when* they trigger it, not in what happens afterward. A single ticket can fire more than one of these over its lifetime (e.g. `new-bug-report-pattern` on arrival, then `actionable-issue-pattern` later if a matching `KnownIssue`+`Fix` is subsequently ingested) — each produces its own `Investigation` node and its own comment, which is intended: an immediate "we're looking into this" followed later by a stronger "this is the same thing we already fixed."

### `actionable_issue_pattern.yaml`

```yaml
name: actionable-issue-pattern
mode: DistinctId
pattern: |
  MATCH (ci)-[:HAS_ERROR]->(e)<-[:HAS_ERROR]-(ki)-[:RESOLVED_BY]->(fix)
  WHERE ci.node_type = 'CustomerIssue' AND e.node_type = 'ErrorSignature'
    AND ki.node_type = 'KnownIssue' AND fix.node_type = 'Fix'
  RETURN DISTINCT id(ci) AS id
enrichment_query: |
  MATCH (ci) WHERE id(ci) = $that.data.id
  MATCH (ci)-[:HAS_ERROR]->(e)<-[:HAS_ERROR]-(ki)-[:RESOLVED_BY]->(fix)
  WHERE e.node_type = 'ErrorSignature' AND ki.node_type = 'KnownIssue' AND fix.node_type = 'Fix'
  RETURN ci.project_slug AS project_slug, ci.source_system AS source_system,
         ci.ticket_id AS ticket_id, ki.issue_id AS known_issue_id, fix.fix_id AS fix_id,
         'actionable-issue-pattern' AS standing_query_name
output_name: investigation-trigger
```

Notes on this specific pattern:

- **No `:Label` syntax — filters on the `node_type` property instead.** Confirmed via live verification against a running Quine 1.10.0 (see § Live Verification Findings below): MODOK never gives nodes a real Quine label, only a `node_type` property (`SET n += {node_type: 'CustomerIssue', ...}`). `MATCH (n:CustomerIssue)` matches nothing against real Quine even when `n.node_type == "CustomerIssue"` — confirmed directly (`n.labels` comes back `[]`). The `WHERE ... node_type = '...'` clause here is not optional decoration; without it the standing query would never fire. This is a pre-existing gap across the *entire* MODOK Cypher surface (DRE, ingestion, `diagnose`), invisible to `DummyQuine`/mocked tests because none of them validate real Quine label semantics — out of scope to fix everywhere in this increment, but load-bearing enough in this one pattern that it had to be fixed here.
- **No `project_slug` filter.** Project isolation is already guaranteed by node topology, not by a WHERE clause: every node type's `idFrom()` address includes `project_slug` (`quine-client.md § ID Scheme`), so a `CustomerIssue` in project A can never share an `ErrorSignature` *node* with project B — the two nodes have entirely different addresses. This also means **one single standing query serves every project** sharing the Quine instance — `modok stream install` takes no `--project` flag, unlike most other CLI commands (see Decisions & Alternatives).
- **`RETURN DISTINCT id(ci) AS id`** satisfies DistinctId mode's constraint (exactly one value, the `id`/`strId` of a node bound in the `MATCH`) — the explicit `AS id` alias is required, not optional (see § Live Verification Findings).
- **Order independence is structural, not coded.** Nothing in the pattern assumes `CustomerIssue`, `KnownIssue`, or `Fix` arrives first — Quine's incremental evaluation fires on whichever write completes the pattern, regardless of order. Confirmed live: writing three of the four required edges produces no match; adding the fourth (tried both as the `CustomerIssue→ErrorSignature` edge and, separately, as the `KnownIssue→Fix` edge) fires it immediately.
- **`enrichment_query`** is the `CypherQuery` `andThen` stage: it re-fetches the identifying fields Quine's own output pipeline needs to build the `PostToEndpoint` body — `$that.data.id` is the result-field key produced by the pattern's `AS id` alias. This is *not* the full debug packet; it is the minimum needed to identify the match. The full packet is assembled by the Diagnostic Retrieval Engine after MODOK receives the callback (see § Standing Query Result Route).
- **`'actionable-issue-pattern' AS standing_query_name`** is a literal, not a graph read — found live to be necessary once a second and third standing query existed. The `/standing-query/result` route defaults a missing `standing_query_name` to `"actionable-issue-pattern"` for backward compatibility, but every enrichment query must now return its own name explicitly; relying on the default silently mislabels matches from any other pattern (confirmed live — see § Second Live Verification Pass).

### `new_bug_report_pattern.yaml`

```yaml
name: new-bug-report-pattern
mode: DistinctId
pattern: |
  MATCH (ci) WHERE ci.node_type = 'CustomerIssue' AND ci.ticket_kind = 'bug'
  RETURN DISTINCT id(ci) AS id
enrichment_query: |
  MATCH (ci) WHERE id(ci) = $that.data.id
  RETURN ci.project_slug AS project_slug, ci.source_system AS source_system,
         ci.ticket_id AS ticket_id, 'new-bug-report-pattern' AS standing_query_name
output_name: investigation-trigger
```

Fires on a single-node match — no relationship traversal at all, unlike `actionable-issue-pattern`. The enrichment query does not return `known_issue_id`/`fix_id`; `InvestigationData` defaults both to `""` (`docs/specs/webhook-receiver.md` model, `SQ-INV-001`), and the resulting `investigation_id` simply has empty segments where those would go — still unique per ticket per pattern, since `standing_query_name` is part of the composite string.

**Why this isn't just `actionable-issue-pattern` with an added `OR` clause.** The first attempt was `MATCH (ci) OPTIONAL MATCH (ci)-[:HAS_ERROR]->(e) WHERE ci.node_type = 'CustomerIssue' AND (ci.ticket_kind = 'bug' OR e.node_type = 'ErrorSignature') RETURN DISTINCT id(ci) AS id`, tested directly against a live Quine 1.10.0 instance before being adopted anywhere. It returned 2774 rows on a graph with exactly 27 `CustomerIssue` nodes total — `OPTIONAL MATCH` combined with a `WHERE` clause referencing the *original* `MATCH` variable silently stopped filtering on `ci.node_type` at all. Removing the `OR ticket_kind` condition and leaving only `OPTIONAL MATCH (ci)-[:HAS_ERROR]->(e) WHERE ci.node_type = 'CustomerIssue'` reproduced the same 2774-row result, isolating the bug to `OPTIONAL MATCH` itself, not the `OR`. A `UNION` of two plain `MATCH...WHERE` queries returned the correct 3 rows when tested as an ad-hoc query, but whether Quine's `DistinctId` standing-query mode accepts a `UNION`ed pattern at all was untested and judged too risky to introduce without a live registration to confirm it — so this became two separate standing queries instead, each reusing the exact single-`MATCH`-clause shape `actionable-issue-pattern` already proves works in production.

### `error_flagged_pattern.yaml`

```yaml
name: error-flagged-pattern
mode: DistinctId
pattern: |
  MATCH (ci)-[:HAS_ERROR]->(e)
  WHERE ci.node_type = 'CustomerIssue' AND e.node_type = 'ErrorSignature'
  RETURN DISTINCT id(ci) AS id
enrichment_query: |
  MATCH (ci) WHERE id(ci) = $that.data.id
  RETURN ci.project_slug AS project_slug, ci.source_system AS source_system,
         ci.ticket_id AS ticket_id, 'error-flagged-pattern' AS standing_query_name
output_name: investigation-trigger
```

Same two-hop shape `actionable-issue-pattern`'s first segment already uses, just without continuing on to `KnownIssue`/`Fix` — fires on *any* `HAS_ERROR` anchor, mechanical or LLM-derived (`docs/llds/standing-queries.md § LLM Fallback Anchor Classification`), whether or not that `ErrorSignature` happens to already be linked to a resolved `KnownIssue`.

### Live Verification Findings

Verified end-to-end against a running `~/.modok/quine.jar` (v1.10.0): `modok stream install`, real graph writes via raw Cypher completing the pattern, a real `modok serve` listening on `:4242`, confirmed by querying Quine afterward for the resulting `Investigation` node and `INVESTIGATES` edge. Three real bugs were found this way — none visible to `DummyQuine`/mocked tests, all now fixed:

1. **`node_type` is a property, not a label.** `MATCH (n:CustomerIssue)` matches nothing against real Quine even when `n.node_type == "CustomerIssue"` (`n.labels` comes back `[]`). Fixed: the pattern and `enrichment_query` filter on `WHERE n.node_type = '...'` instead of `:Label` syntax. This is a pre-existing gap across the *entire* MODOK Cypher surface (DRE, ingestion, `diagnose`) — out of scope to fix everywhere in this increment, but load-bearing enough in this one pattern that it had to be fixed here.
2. **An unaliased `RETURN DISTINCT id(ci)` produces a result-field key of `id(ci)`, not `id`.** Confirmed via a minimal `PrintToStandardOut`-output standing query showing the exact match payload Quine produces. `$that.data.id` in the `enrichment_query` therefore silently referenced a field that didn't exist. Fixed: `RETURN DISTINCT id(ci) AS id`.
3. **Quine's default output `structure` wraps the row as `{"meta": {...}, "data": {...}}`, not flat.** Confirmed by inspecting Quine's own error log for the failed `PostToEndpoint` attempt (visible only *after* fix #2 above let the pipeline get that far) — the delivered body was the fields directly, but MODOK's route expected them at the top level, not nested under `data`. Fixed on the MODOK side rather than by fighting Quine's `structure` config: the route unwraps `{"meta", "data"}`-shaped bodies (`SQ-ROUTE-006`), since a flat body must also continue to work and guessing Quine's exact `structure` semantics further was less reliable than handling both shapes.

With all three fixes applied, a full cycle was confirmed working: partial evidence written (no match) → the completing `KnownIssue -[:RESOLVED_BY]-> Fix` edge written → standing query fires → `CypherQuery` enrichment runs → `PostToEndpoint` delivers to `modok serve` → `POST /standing-query/result` returns `200 OK` → querying Quine directly shows the `Investigation` node (`investigation_id: "github-7000--ki-demo8--fix-demo8--actionable-issue-pattern"`, `status: "open"`, `trigger_type: "standing_query"`) and its `INVESTIGATES` edge to the `CustomerIssue`, with no manual `retrieve`/`diagnose` call at any point. Test data was cleaned up and Quine/the test server were stopped afterward, restoring original state. GitHub write-back itself (posting to a real issue) was not exercised in this pass — it depends on `GITHUB_TOKEN` and a real repo, and is covered by unit tests against a mocked GitHub API instead.

### Second Live Verification Pass — the ID-scheme bug

A later session ran the mechanical feature linker (SQ-ANCH-008) against a real GitHub issue (mentioning "wifi") in a fully-ingested `stagehand` project — a real `Feature` node (`wifi-provisioning`) existed, and the ticket text should have token-matched it. It didn't: querying Quine directly after the poll cycle completed showed the `CustomerIssue` node with no `AFFECTS` edge at all.

Root cause, found by directly comparing values: `modok.quine.ids.idFrom("feature", "stagehand", "wifi-provisioning")` computed `-1717440451565874260` (a Python SHA-256-truncated int64); the real node's `id(f)` in Quine was `"1b26161f-898a-3aa0-aa63-fc1489ed339d"` (a UUID). These can never be equal — `node_exists(computed_int)` was silently, permanently asking about a node type Quine never assigns. This is exactly the design this project's own `docs/llds/quine-client.md § Decisions & Alternatives` table already rejected ("SHA-256 int64 — wrong ID type; Quine uses UUIDs"), but `anchor_linking.py` (mechanical error linking, plus the same session's new feature linking and LLM-fallback classification) had adopted it anyway by copying the wrong reference pattern, and it had silently spread into two other call sites written in earlier sessions:

- `GithubIngester.ingest_pr`'s `IMPLEMENTED_IN`/`RESOLVED_BY` existence gates (`docs/llds/github-ingestion.md`) — merged-PR edges were never actually written against real Quine.
- `_process_investigation`'s `Investigation` dedup check, and `_maybe_notify_github`'s resolution of the `CustomerIssue` node ID before calling `retrieve()` — meaning the GitHub write-back (this component's centerpiece) always failed with `DRENotFoundError`, silently swallowed by SQ-GH-004's broad exception handler, so no comment was ever actually posted regardless of configuration.

None of this was caught by the unit test suite, because every test mocks `node_exists`/`get_node`/`replace_edges` directly and supplies whatever ID the test author chose — the mismatch only exists between a *real* Quine-assigned ID and a *Python-computed* one, which a mock can't detect since it never talks to real Quine. It also wasn't caught by the first live-verification pass above, which seeded evidence via raw Cypher writes (bypassing `anchor_linking.py` entirely) and explicitly did not exercise the GitHub write-back path live.

**Fix**: `QuineClient.node_exists_by_parts` and `.replace_edges_by_parts` (`docs/llds/quine-client.md § node_exists_by_parts`), mirroring `write_edge_by_parts`'s existing correct pattern — embed Quine's own `idFrom()` in the query text instead of requiring a precomputed ID. All three call sites above were switched to use them; `_maybe_notify_github` additionally now resolves the `CustomerIssue`'s real ID via a property-match query (the same pattern `modok retrieve`'s CLI command already used correctly) rather than any `idFrom()` variant, since there is no `idFrom()`-based approach that works for a node whose real ID must come from Quine itself.

**A third live pass (testing `new-bug-report-pattern`, below) found the ID-type fix alone was insufficient.** A real bug-labeled `CustomerIssue` write produced a confirmed standing-query match and a `200 OK` `PostToEndpoint` delivery, but no `Investigation` node ever appeared. Root cause: `node_exists`/`node_exists_by_parts` still returned `True` for an address nothing had ever been written to — Quine's `MATCH (n) WHERE id(n) = <any address> RETURN n` always returns a row (an empty-property shell), even for a brand-new address, so `bool(results)` could never report "doesn't exist." `_process_investigation`'s dedup check hit exactly this, concluded the `Investigation` was "already recorded," and skipped the real upsert on the very first delivery. Fixed by requiring a `node_type` property on the returned node — the same discipline `collect_nodes()` already applies (`CLI-REC-009`). Full detail: `docs/llds/quine-client.md § node_exists_by_parts`.

**A fourth live pass (checking the actual GitHub comment content for a real ticket) found the DRE's graph-first anchoring had never worked at all.** With the three fixes above in place, `new-bug-report-pattern` correctly fired and posted a comment — but the comment contained only a summary line, no known issues, fixes, files, or tests, despite a real `AFFECTS` edge to a real `Feature` sitting in the graph. Root cause, in `modok.retrieval.engine`: (1) `_graph_anchors` and four other traversal functions used the same broken `:Label` Cypher syntax established elsewhere in this document, so graph-first anchoring was a dead code path that always fell through to the LLM fallback; (2) `RETURN f.feature_slug` projects a scalar, which real Quine returns as a raw value, not a node dict — the code's `row[0]["properties"]["feature_slug"]` silently extracted nothing even after fix (1). Separately, `_maybe_notify_github`'s call to `retrieve()` passed no registry context at all, crippling the LLM fallback that was — until fix (1) — the *only* path anchoring ever took. All fixed; full detail and the exact Cypher: `docs/llds/diagnostic-retrieval-engine.md § Anchor Extraction`, `§ Graph Traversal`.

### Loader and installer interface

```python
# src/modok/quine/standing_queries/loader.py
@dataclass
class StandingQueryDefinition:
    name: str
    mode: str                  # "DistinctId" in v1; MultipleValues not used
    pattern: str
    enrichment_query: str
    output_name: str

def load_definition(name: str) -> StandingQueryDefinition: ...
def all_definitions() -> list[StandingQueryDefinition]: ...
```

### `QuineClient` additions

Four new async methods on `QuineClient` (`src/modok/quine/client.py`), following the same v1 REST family the client already targets (`/api/v1/query/cypher`):

```python
async def standing_query_exists(self, name: str) -> bool: ...
    # GET /api/v1/query/standing/{name} — 200 exists, 404 does not

async def install_standing_query(
    self, definition: StandingQueryDefinition, callback_url: str
) -> bool:
    # POST /api/v1/query/standing/{name}
    # body: {"pattern": {"query": definition.pattern, "type": "Cypher", "mode": definition.mode},
    #        "outputs": {definition.output_name: {"type": "CypherQuery", "query": definition.enrichment_query,
    #                    "andThen": {"type": "PostToEndpoint", "url": callback_url}}}}
    # Returns True if newly installed, False if standing_query_exists() was already True (no-op, no request sent).

async def list_standing_queries(self) -> list[str]: ...
    # GET /api/v1/query/standing — returns registered names

async def remove_standing_query(self, name: str) -> bool: ...
    # DELETE /api/v1/query/standing/{name}. Returns True if removed, False if it did not exist (no-op).
```

`callback_url` is not part of the checked-in YAML artifact — it is assembled at install time from MODOK's own webhook config (`http://{host}:{port}/standing-query/result`), so the artifact stays portable across dev machines and the shared Mac mini without hardcoding a host.

**Exact wire shape is an open risk.** The `pattern`/`outputs`/`mode` JSON nesting above is reconstructed from Quine's public documentation, not confirmed against a running instance. Before the demo is considered done, a contract test (`[C]` level, matching the convention in `docs/specs/quine-client.md § Test Level Convention`) runs `install_standing_query` against the real local Quine JAR (`~/.modok/quine.jar`, v1.10.0) and confirms the query actually fires. See Open Questions.

## Mechanical Anchor Linking

Module: `src/modok/ingestion/anchor_linking.py`. Two independent mechanical (LLM-free) linkers — one for errors, one for features — plus one LLM-fallback classifier that only ever runs when both linkers find nothing.

```python
async def link_customer_issue_error_anchors(
    client: Any,
    project_slug: str,
    repo_root: Path,
    source_system: str,
    ticket_id: str,
    raw_text: str | None,
) -> list[str]:
    """Substring-match raw_text against the project's registered error signatures
    and write HAS_ERROR edges to the ones that already exist as ErrorSignature
    nodes in the graph. Returns the list of normalized_error strings linked.
    Never invents an ErrorSignature node; never calls an LLM."""

async def link_customer_issue_feature_anchors(
    client: Any,
    project_slug: str,
    repo_root: Path,
    source_system: str,
    ticket_id: str,
    raw_text: str | None,
) -> list[str]:
    """Token-match raw_text against the project's registered Feature slugs/names
    and write AFFECTS edges to the ones that already exist as Feature nodes in
    the graph. Returns the list of feature slugs linked. Never invents a
    Feature node; never calls an LLM."""
```

### Error linking algorithm (unchanged)

1. If `raw_text` is `None` or empty, return `[]` immediately — no anchors possible.
2. Load the project's `Registry` from `repo_root / "registries"`. If `RegistryNotFoundError` is raised (no registries bootstrapped yet for this project), log a warning to stderr and return `[]` — ticket ingestion must still succeed even if anchor linking cannot run.
3. For each error slug in `registry.error_slugs()` (new method — mirrors the existing `feature_slugs()`/`module_slugs()`), read its `normalized_error` string.
4. Match on a **word boundary**, not a raw substring — `re.search(rf"\b{re.escape(normalized_error)}\b", raw_text, re.IGNORECASE)`. A raw `in` check would false-positive on short or generic `normalized_error` values that happen to appear inside an unrelated larger word (e.g. `"GSS"` inside `"GSSAPI"`); word-boundary matching is nearly as cheap and closes that off.
5. For each match, check `client.node_exists(idFrom('error', project_slug, normalized_error))` — only matches against `ErrorSignature` nodes that already exist survive to the next step; nothing is invented.
6. Compute the **full current set** of matched `normalized_error` values, then call `client.replace_edges(idFrom('customer-issue', project_slug, source_system, ticket_id), "HAS_ERROR", [idFrom('error', project_slug, e) for e in matched])` **once**, rather than writing each match individually. `HAS_ERROR` is a shared edge-type *name* written by three separate owners at v1 (`Feature -[:HAS_ERROR]->` from doc frontmatter, `KnownIssue -[:HAS_ERROR]->` from the new known-issue block fields, `CustomerIssue -[:HAS_ERROR]->` here) — `replace_edges` only ever touches edges outbound from the single `from_id` it's given, so this reconciliation can never delete another owner's `HAS_ERROR` edges, even though all three share a label. This matters for the `issues: edited` GitHub webhook action (`GitHubAdapter` already routes edits through the same `customer_issue` ingest path): without `replace_edges`, an edit that removes a mention of an error would leave the stale `HAS_ERROR` edge in place forever, since individual `write_edge_by_parts` calls are additive-only. `replace_edges` is exactly the reconciliation primitive `quine-client.md` documents for this (§ "the reconciliation primitive for authoritative relationships").
7. Return the list of linked `normalized_error` strings (used for logging and tests).

### Feature linking algorithm (new)

1. If `raw_text` is `None` or empty, return `[]` immediately.
2. Load the project's `Registry`. If `RegistryNotFoundError`, log a warning and return `[]` — same non-fatal behavior as error linking.
3. Tokenize `raw_text` into a set of lowercase word tokens via `modok.text_utils.extract_text_tokens` (word extraction, then camelCase/snake_case/kebab-case splitting, length > 2) — the exact same tokenizer the Diagnostic Retrieval Engine's `_pre_match_modules` already uses at read time, now extracted into a shared module so both call sites stay in sync.
4. For each registered feature (`registry.feature_names()` → slug, name), tokenize the slug and the name the same way, and check for any token overlap with the ticket's tokens. E.g. a ticket mentioning "wifi" overlaps feature slug `wifi-provisioning` (tokenizes to `{wifi, provisioning}`).
5. For each token-match, check `client.node_exists(idFrom('feature', project_slug, slug))` — same never-invent-a-node discipline as error linking.
6. Compute the full current set of matched feature slugs, call `replace_edges(ci_id, "AFFECTS", [...])` once — same reconciliation rationale as step 6 above (a ticket edit that removes a feature mention should drop the stale `AFFECTS` edge, not accumulate it).
7. Return the list of linked feature slugs.

Both linkers are deliberately narrower than the Diagnostic Retrieval Engine's LLM fallback (`docs/llds/diagnostic-retrieval-engine.md § Anchor Extraction`): no LLM, no scoring. They only ever confirm a match against a *registered* string against a node *already present* in the graph — the same "convention + registries are truth" invariant the rest of ingestion follows. Token matching is more forgiving than the error linker's exact word-boundary match (necessary because organically-written ticket text essentially never contains a feature's literal slug string), but it is still a fixed, deterministic, LLM-free rule — not classification.

**Call sites** (every place a `CustomerIssue` node is written runs both linkers immediately after the `upsert_node`, then conditionally the LLM fallback classifier below):

- `run_ingest_event`'s `customer_issue` branch (`src/modok/webhook/server.py`) — covers the webhook push path (`GitHubAdapter`, `GenericTicketAdapter`) and the GitHub poll adapter (both call `on_event`, which wraps `run_ingest_event`).
- `GithubIngester.ingest_issue` (`src/modok/ingestion/github.py`) — covers the batch `ingest-github` CLI path, which writes `CustomerIssue` nodes directly without going through `IngestEvent`/`on_event`. This is a small, additive, single-call touch to a different LLD's segment (`docs/llds/github-ingestion.md`) — flagged per LID cascade discipline; no other change to that file's interfaces.
- `_ingest_customer_ticket` (`src/modok/cli/commands/ingest.py`), the `modok ingest <ticket_file>` path — covers direct single-ticket-file ingestion via the CLI. Unlike the other two call sites, `repo_root` here needs no resilient-fallback resolution: `ingest_cmd` already calls `config.project(project)` before reaching this branch, so a project that doesn't resolve has already caused the command to exit before this point. A small, additive touch to the CLI LLD segment (`docs/llds/cli.md`) — flagged per LID cascade discipline.

Both call sites need `repo_root`, resolved via `ModokConfig.load()` → the matching `ProjectConfig.repo` for `project_slug`. This resolution step is wrapped in a broad `try/except` at the call site itself (not inside either linker, which only handles `RegistryNotFoundError`) — a project not present in config, a missing config file, or any other resolution failure logs a warning and is otherwise ignored. This matters concretely: `run_ingest_event`'s pre-existing test coverage calls it directly with a bare mock client and no config file on disk, and must keep working unchanged.

## LLM Fallback Anchor Classification

New function, same module (`src/modok/ingestion/anchor_linking.py`):

```python
async def classify_customer_issue_anchors(
    client: Any,
    project_slug: str,
    repo_root: Path,
    source_system: str,
    ticket_id: str,
    raw_text: str | None,
    backend: str = "local",
) -> None:
    """LLM fallback anchor classification. Only called by a call site when both
    mechanical linkers (error, feature) found nothing for this CustomerIssue.
    Calls the LLM Gateway's parse_ticket with the project's registry context,
    validates the result against the registry and existing graph nodes — the
    same never-invent-a-node discipline as the mechanical linkers — and
    persists any resulting HAS_ERROR/AFFECTS edges. Never raises: LLM
    unavailability, a rejected response, or a missing registry all degrade to
    "no anchors written", not an ingestion failure."""
```

This is the one point in the ingestion path where LLM output is written to Quine directly (HLD Key Design Decision #3). It exists because exact/token mechanical matching — by design, conservative — misses the common case: an organically-written ticket that describes a known bug or feature without using the registry's exact words. Rather than leave that ticket anchor-less until some later caller happens to invoke the DRE's read-time fallback (the pre-existing gap this closes), ingestion itself now runs the same fallback and keeps the result.

Algorithm:

1. If `raw_text` is `None` or empty, return immediately — no anchors possible.
2. Load the project's `Registry`. If `RegistryNotFoundError`, log a warning and return — same non-fatal behavior as both mechanical linkers.
3. Build the same registry-derived context `modok retrieve`/`diagnose` already assemble for the DRE (`src/modok/cli/commands/retrieve.py`): `feature_slugs`, `module_slugs`, `valid_slugs = feature_slugs + module_slugs`, `feature_descriptions`, `module_descriptions`, `module_elements`, `all_module_source_files()`.
4. Call `gateway.parse_ticket(raw_text, project_slug, backend=backend, valid_slugs=valid_slugs, ...)` — the identical call the DRE's read-time fallback makes (`engine.py`). `parse_ticket` already filters `feature_slugs` in its result against `valid_slugs` before returning (`gateway.py:771-772`); `error_signatures` are **not** pre-filtered by `parse_ticket` and must be validated here.
5. If `parse_ticket` raises `LLMUnavailableError` or `LLMGatewayError`, log to stderr and return — write nothing. The `CustomerIssue` node write (which already completed before this function was ever called) is unaffected.
6. **Feature validation and write**: keep only slugs that are (a) in `registry.feature_slugs()` specifically — not `module_slugs` — since `AFFECTS` targets are `Feature` nodes only, and (b) confirmed via `client.node_exists(idFrom('feature', ...))`. Call `replace_edges(ci_id, "AFFECTS", [...])` once with the full validated set (possibly empty), mirroring the mechanical linkers' reconciliation discipline.
7. **Error validation and write**: keep only signatures that are (a) present in `registry.error_normalized_values()`, and (b) confirmed via `client.node_exists(idFrom('error', ...))`. Call `replace_edges(ci_id, "HAS_ERROR", [...])` once with the full validated set.
8. `ticket_kind` (bug vs. feature-request classification) is out of scope for *this function* — it is never inferred from `raw_text`, mechanically or via LLM. It is instead derived from GitHub issue labels at ingestion time, a structured/explicit signal rather than a text classifier — see `docs/llds/github-ingestion.md § Ticket Kind from Labels`.

**Call-site gating** — both call sites run this after both mechanical linkers, only if both returned empty:

```python
matched_errors = await link_customer_issue_error_anchors(...)
matched_features = await link_customer_issue_feature_anchors(...)
if not matched_errors and not matched_features:
    await classify_customer_issue_anchors(...)
```

This mirrors the DRE's own existing precedent (`engine.py` — `if not feature_slugs and not error_sigs:` before invoking the LLM fallback at read time): mechanical/graph evidence, when present, is always preferred over an LLM call, at both read time and now write time. A ticket that mechanically matches on *either* type skips the LLM step entirely for both types — accepted trade-off, documented in Open Questions.

## `Investigation` Node

```python
class Investigation(QuineNode):
    node_type: Literal["Investigation"]
    project_slug: str
    investigation_id: str
    status: str              # "open" — no resolution lifecycle in v1
    trigger_type: str         # "standing_query" — only value in v1; named generically for future triggers
    triggered_at: str         # ISO 8601, set by the adapter at write time
    standing_query_name: str
```

`idFrom('investigation', project_slug, investigation_id)` — mirroring `KnownIssue`/`Fix`'s single-composite-string convention rather than `ResolutionEvent`'s multi-part one. `investigation_id` itself is built deterministically by the adapter from the enrichment payload:

```python
investigation_id = f"{source_system}-{ticket_id}--{known_issue_id}--{fix_id}--{standing_query_name}"
```

This keeps the node schema to exactly the five fields the task's schema guidance specifies — no `known_issue_id`/`fix_id` fields on the node itself, since they're already encoded in `investigation_id` and traceable by re-running the same DRE traversal `INVESTIGATES` implies.

`known_issue_id`/`fix_id` default to `""` (`InvestigationData`, `src/modok/webhook/models.py`) — only `actionable-issue-pattern`'s enrichment query returns them; `new-bug-report-pattern` and `error-flagged-pattern` fire on a `CustomerIssue` alone and leave both blank. The formula above still produces a valid, unique `investigation_id` with empty segments in that case (e.g. `"github-42------new-bug-report-pattern"`) — uniqueness per ticket per pattern comes from `standing_query_name`, not from `known_issue_id`/`fix_id` being non-empty.

Edge: `Investigation -[:INVESTIGATES]-> CustomerIssue`.

Because `investigation_id` fully encodes the evidence identity, redelivery of the same match — Quine retrying a `PostToEndpoint` call, or a caller replaying a captured payload — always resolves to the same node address. `upsert_node` on an unchanged address is a no-op in effect (same properties re-set); the `INVESTIGATES` edge write is idempotent via `MERGE`. This satisfies "duplicate events do not create duplicate investigations" without any deduplication logic beyond the ID scheme itself.

## Standing Query Result Route

This is **not** a `PushAdapter`. The existing push-adapter model (`POST /webhook/{project_slug}/{source}`) assumes the project slug is already known from the URL — but a Quine `PostToEndpoint` output posts to one static, configured URL per output, and (per the "no `project_slug` filter" note above) one standing query serves every project. Templating the callback URL per match is not something the enrichment stage can plausibly do reliably, so `project_slug` travels in the POST body instead, returned by the `enrichment_query` alongside the other fields.

New dedicated route in `build_app` (`src/modok/webhook/server.py`), a sibling to `/health` and `/webhook/{project_slug}/{source}`, not registered through `PUSH_ADAPTERS`:

```
POST /standing-query/result
Body: {"project_slug": ..., "source_system": ..., "ticket_id": ...,
       "known_issue_id": ..., "fix_id": ...}
      — or a JSON array of such objects (the enrichment CypherQuery stage
        is not itself DistinctId-constrained and could in principle
        return more than one row per match; the route accepts both shapes)
```

Behavior:

1. Parse body; if it's a single object, treat as a one-element list.
2. For each row: validate required fields present (400 if not); look up `project_slug` against `known_project_slugs` (404 if unknown, same as the existing push route).
3. Build `IngestEvent(kind="investigation", project_slug=..., data=InvestigationData(...))` and run it through `run_ingest_event` in a thread pool, same pattern as the push route.
4. Return `{"status": "ok", "investigations_written": N}`.

**No authentication on this route in v1.** Every other push adapter verifies a signature or bearer token; this one doesn't, because the caller is Quine itself, co-located on `127.0.0.1` with MODOK's webhook server in the only deployment MODOK supports today (HLD Non-Goals: "MODOK does not enforce access control in v1. It is a single-user or trusted-team tool"). Documented here as an explicit, accepted decision rather than an oversight — revisit if Quine and MODOK are ever split across hosts.

## `run_ingest_event` — `investigation` branch

New `IngestEvent.kind = "investigation"` and `InvestigationData` (`src/modok/webhook/models.py`):

```python
@dataclass(frozen=True, eq=True)
class InvestigationData:
    source_system: str
    ticket_id: str
    known_issue_id: str
    fix_id: str
    standing_query_name: str
```

In `run_ingest_event`, the new branch:

1. Computes `investigation_id` and checks `client.node_exists_by_parts(("investigation", project_slug, investigation_id))` **first** — embedding `idFrom()` in the query text (`docs/llds/quine-client.md § node_exists_by_parts`), not a Python-computed value (a bug found live: `modok.quine.ids.idFrom()` returns a SHA-256 int64, not a Quine UUID, so a `node_exists()` call given one always returns `False` regardless of whether the node exists — this made SQ-INV-003's dedup check a permanent no-op against real Quine). Because `investigation_id` fully encodes evidence identity (source_system, ticket_id, known_issue_id, fix_id, standing_query_name), an existing node means this exact match has already been recorded — the branch stops here, no re-upsert, no DRE call, no GitHub comment. This is what actually prevents duplicate GitHub comments on redelivery: node-write idempotency alone is not enough, since steps 3–4 below are side effects that a naive "upsert then always notify" design would repeat on every redelivery of the same match.
2. Only if the node did not already exist: upserts the `Investigation` node and writes `Investigation -[:INVESTIGATES]-> CustomerIssue`.
3. Resolves the `CustomerIssue`'s real Quine node id via a property-match query (`MATCH (n) WHERE n.node_type = 'CustomerIssue' AND n.project_slug = $p AND n.source_system = $s AND n.ticket_id = $t RETURN id(n)`, the same pattern `modok retrieve`'s CLI command already uses) and calls `retrieve()` (`modok.retrieval.engine`) to assemble the debug packet. This also replaces a Python-computed `idFrom()` value that was found live to always resolve to a nonexistent ID — `retrieve()`'s internal `get_node()` call would raise `QuineNodeNotFoundError` every time, silently swallowed by step 5's broad exception handler, so the GitHub write-back never actually posted a comment for any issue, regardless of configuration (SQ-GH-005).
4. If `source_system == "github"`: looks up `github_repo` for `project_slug` (`ModokConfig.load()`) and `GITHUB_TOKEN` from the environment. If both present, formats the packet as markdown (`format_debug_packet_markdown`, new function in `src/modok/retrieval/formatting.py`) and calls `post_issue_comment` (new function in `src/modok/ingestion/github.py`, alongside the existing GitHub HTTP helpers) to `POST /repos/{github_repo}/issues/{ticket_id}/comments`.
5. Any failure in steps 3–4 (LLM unavailable, GitHub API error, missing token) is caught, logged to stderr, and does **not** roll back the `Investigation` write from step 2. The graph write is MODOK's authoritative record regardless of whether the external notification succeeded — matching the HLD's authority model. Note this does mean a GitHub API failure on first delivery is not retried on a later redelivery of the same match (step 1 would now see the node and stop) — accepted for v1; see Open Questions.

## GitHub Write-Back

`post_issue_comment(github_repo: str, token: str, issue_number: str, body: str) -> None` — one `httpx` POST, same header shape (`Authorization: Bearer`, `Accept: application/vnd.github+json`) as `GithubIngester` already uses. Best-effort: any non-2xx response or exception is logged, never raised past this function.

### Two comments per investigation (SQ-GH-007)

`_maybe_notify_github` posts **two** independent comments, not one:

1. **Triggered** (`format_investigation_triggered_markdown`) — posted immediately, before `retrieve()` runs. Just the header, the standing query name, a summary from `quick_investigation_summary` (`docs/llds/diagnostic-retrieval-engine.md § Quick Investigation Summary`), and the investigation ID. No anchors, candidates, or commits — those require the traversal this comment is specifically posted *before*.
2. **Results** (`format_debug_packet_markdown`) — posted after `retrieve()` completes, with the full packet as before.

Found live: a full `retrieve()` call — traversal, scoring, and the summary LLM call — can take several minutes, during which the reporter previously saw nothing at all confirming MODOK had picked up their ticket. Splitting the notification lets the fast, traversal-free summary post right away while the slow work continues in the background.

The two posts are independent best-effort attempts: if generating the quick summary fails, the triggered comment still posts (with an empty summary line) rather than being skipped; if *posting* the triggered comment fails (GitHub API error), `retrieve()` still runs and the results comment is still attempted. Neither failure blocks or is blocked by the other — this mirrors the existing SQ-GH-004 discipline (log and continue) applied to two posts instead of one.

```markdown
## 🔎 MODOK investigation triggered

Standing query `{standing_query_name}` matched this issue against existing graph evidence.

**Summary:** {quick_summary}

_Investigation: `{investigation_id}`_
```

`format_debug_packet_markdown(packet: DebugPacket, investigation_id: str, standing_query_name: str) -> str` renders the **full** packet — the same content `ui/src/components/modok/DebugPacketView.tsx` shows in the demo app, not the thinner subset `diagnose.py`'s `_print_packet` prints to a terminal. `retrieve()` already computes anchors, affected areas, scored candidates, and recent commits regardless of caller; the original version of this formatter silently dropped all of them, discarding real evidence that was already sitting on the packet — found live when a real, over-matched ticket's comment showed only a flat file list with no way to distinguish a strong match from a weak one:

```markdown
## 🔍 MODOK investigation results

Standing query `{standing_query_name}` matched this issue against existing graph evidence.

**Summary:** {packet.summary}

**Anchors:** Features: {...} · Errors: {...} · Symptoms: {...}

**Affected areas:** {⬡|○} {name}, ...

**Top suspects:**
- `[{CONFIDENCE}]` `{path}` (score {score})
  - {evidence.type}: {evidence.explanation}
  ...
- `[LOW]` {N} supporting doc/config files (non-source, low relevance): `{path}`, `{path}`, ...

**Known issues:** {for each: `- {id}: {summary}`}
**Prior fixes:** {for each: `- {id} ({commit}): {summary}`}
**Relevant files:** {up to N, as a list}
**Relevant tests:** {up to N, as a list}

**Recent commits:** {for each: `- {sha[:7]} ({date}) {author} — {message}`}

_Investigation: `{investigation_id}`_
```

The header changed from "investigation triggered" to "investigation results" (SQ-GH-007) specifically to distinguish this comment from the first one now that both exist — otherwise two identically-headed comments on the same issue would be confusing to tell apart at a glance.

Every section is omitted entirely when its underlying list is empty, same discipline as before. Section order mirrors `DebugPacketView.tsx`'s rendering order (summary → anchors → affected areas → top suspects → known issues/fixes → files/tests → recent commits) rather than `_print_packet`'s, since parity with the demo app's richer view — not the terminal's thinner one — is the goal here.

**Doc-penalized candidates are grouped, not listed individually** (SQ-GH-006). Any `scored_candidates` entry whose evidence includes a `doc_penalty` item is pulled out of the ranked list and collapsed into one trailing `[LOW]` line (count + comma-separated paths), after all non-doc-penalized candidates. Found live: a feature with several modules can pull in 10+ LLDs/specs/arrow docs/systemd unit files, all scoring similarly low — listing each individually (path, confidence, full evidence breakdown) buried the handful of genuinely-scored source/test candidates the reader actually needs. This grouping is purely a rendering choice in `format_debug_packet_markdown` — `retrieve()`'s own `scored_candidates` list (and the JSON/CLI/API surfaces built on it) is untouched, so no data is lost, only the GitHub comment's presentation is condensed.

## GitHub Poll Adapter

New `src/modok/webhook/adapters/github_poll.py`, implementing the existing `PullAdapter` protocol (`docs/llds/webhook-receiver.md § Pull adapter`) exactly as documented — no protocol changes.

```python
class GitHubPollAdapter:
    async def start(self, config: WebhookConfig, on_event) -> None:
        # spawns one background asyncio task per opted-in project
    async def stop(self) -> None:
        # cancels and awaits the task(s)
```

Design choice: the adapter does **not** convert issues to `IngestEvent` and push them through `on_event` — it constructs a `GithubIngester` per opted-in project (same class `ingest-github` already uses, unmodified) and calls `await ingester.run(since=proj.last_github_sync)` on an interval, then persists the new `last_github_sync` the same way the CLI command does. This reuses 100% of `GithubIngester`'s existing, tested pagination/incremental-fetch/edge-writing logic (`IMPLEMENTED_IN`, `RESOLVED_BY` on the PR side) with zero refactor to `ingestion/github.py`'s public shape — the only change to that file is the one-line anchor-linking call inside `ingest_issue` described above. The trade-off: this adapter's `on_event` parameter goes unused (the `PullAdapter` protocol still requires the signature; `GithubIngester` writes directly via its own `QuineClient` reference, constructed the same way the CLI command constructs one). Both push (webhook) and pull (poll) ticket-ingestion paths converge on the same `CustomerIssue` write and the same mechanical anchor-linking call, so the standing query fires identically regardless of which path an issue arrived through.

Registered in `PULL_ADAPTERS` (`src/modok/webhook/router.py`):

```python
PULL_ADAPTERS: dict[str, object] = {
    "github-poll": GitHubPollAdapter(),
}
```

Gated by `WebhookConfig.github_poll_enabled` (new field, default `False`) — off unless a project opts in, consistent with `enabled_sources` gating push adapters. Applies to every configured project with `github_repo` set and `GITHUB_TOKEN` present in the environment.

**Visibility.** Early versions of this adapter produced output only on failure (`sync failed for {slug}`), which made a healthy idle poller indistinguishable from one silently doing nothing — discovered live when a restarted `modok serve` process lost its exported `GITHUB_TOKEN` and produced zero console output either way. Current behavior distinguishes three cases (`SQ-POLL-004`, `SQ-POLL-006`):

- No `github_repo` configured for a project → silent, no log line. This is the expected common case (most MODOK projects aren't GitHub-backed) and would otherwise spam the console every cycle for every non-GitHub project.
- `github_repo` configured but `GITHUB_TOKEN` unset → a one-line warning to stderr naming the project. This combination is far more likely a real misconfiguration (an expired or unexported token) than an intentional non-GitHub project, so it earns a log line even though it's still not treated as an error.
- Successful sync → a one-line summary to stdout naming the project and the issue/PR counts synced, so each poll cycle is visible in `modok serve`'s console.

New `WebhookConfig` fields (`src/modok/webhook/models.py`):

```python
github_poll_enabled: bool = False
github_poll_interval_seconds: int = 30
```

30s default: short enough that a live demo (open an issue, watch it appear) doesn't feel like a long wait, long enough to stay well clear of GitHub's REST rate limits for a single-repo poll.

## CLI

New `src/modok/cli/commands/stream.py`, group `stream`, registered in `cli/main.py` as `modok stream`:

```
modok stream install   # idempotent: installs actionable-issue-pattern if not already present
modok stream status    # lists installed standing queries by name
modok stream remove    # removes actionable-issue-pattern
```

No `--project` flag — standing queries are Quine-instance-level infrastructure (like `modok quine start/stop/status`), not per-project data, because of the topology-based isolation argument above. `install` calls `client.standing_query_exists(name)` first; if `True`, prints `"{name} is already installed"` and exits 0 without a network write — this is the literal idempotency the task's acceptance criteria ask for, not just "won't error on retry."

## Error Types

No new exception hierarchy — this component is a consumer of `QuineClient` (raises its existing `QuineNodeNotFoundError`/`QuineDeserializationError`/`httpx` errors) and the DRE (raises its existing `DREError` family). Standing-query-specific failure surfaces:

- `install_standing_query` against an unreachable Quine → the existing `QuineClient` retry/timeout behavior applies unchanged (`docs/specs/quine-client.md § Connection and Reliability`); `modok stream install` surfaces the resulting exception with a clear message and non-zero exit, same convention as `modok quine start`.
- GitHub write-back failure → logged, non-fatal (§ `run_ingest_event` above).
- Registry missing during anchor linking → logged, non-fatal (§ Mechanical Anchor Linking above).

## Decisions & Alternatives

| Decision | Chosen | Alternatives Considered | Rationale |
|---|---|---|---|
| Enrichment location | Quine-side `CypherQuery andThen` for match identification; DRE (MODOK-side) for the full packet | All-Quine-side (no DRE call); all-MODOK-side (bare id only from Quine) | User-selected (see HLD §10): proves Quine does real work beyond flagging an id, while reusing the already-built, already-tested DRE for the richer packet rather than duplicating its traversal/scoring logic in Cypher |
| Standing query mode | `DistinctId` | `MultipleValues` (beta) | DistinctId is the non-beta, better-documented mode; the pattern only needs to identify *which* `CustomerIssue` matched, which fits DistinctId's single-id-return constraint exactly |
| Project scoping | No `project_slug` filter; one global standing query | Per-project standing query, name-suffixed by slug | Node topology (`project_slug` baked into every relevant `idFrom()` address) already makes cross-project matches structurally impossible; a WHERE filter would be redundant and DistinctId's WHERE grammar is narrow enough to avoid stretching it |
| Callback routing | Dedicated `POST /standing-query/result` route, project_slug in body | Reuse `POST /webhook/{project}/{source}` via a new push adapter | The push-adapter model assumes project_slug is already known from the URL; Quine's `PostToEndpoint` posts to one static URL and can't plausibly template it per match given the single-global-query design |
| Callback authentication | None | HMAC or bearer token, matching other adapters | Caller is Quine itself, co-located on `127.0.0.1`; consistent with HLD's existing "no access control in v1, single-user/trusted-team tool" non-goal |
| `Investigation` idFrom scheme | Single composite `investigation_id` string (mirrors `KnownIssue`/`Fix`) | Multi-part idFrom (mirrors `ResolutionEvent`) | Keeps the node to exactly the five fields the task's schema guidance specifies; the composite string still fully encodes evidence identity for dedup |
| GitHub poll adapter internals | Reuse `GithubIngester.run()` unchanged, called on a timer, bypassing `on_event` | Refactor `GithubIngester` to expose fetch-only methods consumable via `IngestEvent`/`on_event` | Reuses 100% of existing tested pagination/edge-writing code with zero interface changes to a second, already-covered LLD segment (`docs/llds/github-ingestion.md`); the only touch to that file is one added function call in `ingest_issue` |
| Known-issue evidence for the demo | Extend `_write_known_issue_block` (ingestion-pipeline segment) to write `HAS_ERROR`/`RESOLVED_BY` edges from new `error_signatures`/`fixes` block fields | Seed the edges directly for the demo only, leaving ingestion untouched | User-selected: makes the demo's pre-existing evidence side real doc-ingestion rather than scripted seed data, closing a pre-existing gap (these edges were schema-documented and DRE-consumed but never written by any production code path) |
| Feature anchor matching mechanism | Token/keyword match (tokenize slug+name, check overlap with tokenized ticket text) — same tokenizer the DRE's `_pre_match_modules` already uses at read time | Exact substring match (mirroring the error linker exactly); LLM-only (no mechanical feature matching at all) | User-selected: exact substring match was rejected because organically-written tickets essentially never contain a feature's literal slug string (unlike error text, which is often copy-pasted verbatim from a stack trace) — token overlap catches the common case ("wifi" in prose matching `wifi-provisioning`) while staying fully mechanical |
| LLM fallback trigger condition | Gate on **both** mechanical linkers (error, feature) returning empty | Always run the LLM classifier regardless of mechanical results; gate independently per anchor type | User-selected: mirrors the DRE's own existing read-time precedent (mechanical/graph evidence, when present, is always preferred over an LLM call) and keeps the LLM call reserved for tickets with no mechanical signal at all, rather than doubling ticket-level LLM cost |
| `ticket_kind` (bug vs. feature-request) classification | Dropped from *this* increment entirely — later implemented via GitHub issue labels (`docs/llds/github-ingestion.md`), not text classification | LLM-classified as part of the same `parse_ticket` call; mechanical keyword heuristic (fixed word list) | User-selected: keyword heuristics for free-form sentence *shape* (unlike entity-name matching) would misfire often enough to not be worth adding; labels are explicit, structured metadata a reporter (or issue template) already assigns, sidestepping the classification problem entirely |
| Broader trigger for the debug-packet workflow | Two new, separate standing queries (`new-bug-report-pattern` on `ticket_kind='bug'`; `error-flagged-pattern` on any `HAS_ERROR`), `actionable-issue-pattern` unchanged | Broaden `actionable-issue-pattern` itself with an `OR` condition; require `ticket_kind='bug'` AND some anchor as a single stricter combined condition | User-selected: keeps the strongest signal (`actionable-issue-pattern` — an exact match to something already fixed) undiluted as its own case, while still surfacing weaker-but-useful signals (a labeled bug, or any identified error) immediately. A combined single query was also ruled out on technical grounds — see `new_bug_report_pattern.yaml`'s live-verification note on why `OPTIONAL MATCH` couldn't be used |
| `InvestigationData.known_issue_id`/`fix_id` | Made optional (default `""`) | A separate `InvestigationData` variant per pattern shape | The five-field `investigation_id` formula already produces valid, unique output with empty segments — no need for two dataclass shapes when one with defaults covers both cases cleanly |

## Open Questions & Future Decisions

### Resolved

1. ✅ Enrichment split between Quine (`CypherQuery andThen`, match identification) and MODOK (DRE, full packet) — user-selected, HLD §10.
2. ✅ Anchor linking runs inline at every `CustomerIssue` write site, not as a separate CLI step — user-selected.
3. ✅ GitHub write-back posts the full debug packet as a comment, including the DRE's local-LLM summary — user-selected.
4. ✅ A `GitHubPollAdapter` (interval polling, reusing `GithubIngester.run()` unchanged) supplements the webhook push path so the demo needs no public tunnel — user-selected.
5. ✅ `_write_known_issue_block` is extended (not bypassed) to write the `HAS_ERROR`/`RESOLVED_BY` edges the pattern needs — user-selected.

### Resolved (via live verification against `~/.modok/quine.jar` v1.10.0)

7. ✅ **Standing-query wire shape** (`pattern`/`outputs`/`mode` JSON nesting) — confirmed correct as designed; `modok stream install/status/remove` all work against real Quine unchanged.
8. ✅ **`node_type` is a property, not a label** — found live, fixed in the pattern and `enrichment_query` (§ Live Verification Findings). This was the actual blocker for the pattern ever firing against real data; not something a mocked-Quine test could have caught.
9. ✅ **The core detection mechanism fires reliably and order-independently** — confirmed across three independent evidence sets in one session.
10. ✅ **`PostToEndpoint` delivery to MODOK, end to end.** Two more real bugs found and fixed via live debugging: the unaliased `RETURN DISTINCT id(ci)` broke the `$that.data.id` reference (fixed with `AS id`), and Quine's default `{"meta", "data"}` output structure didn't match the route's expected flat body (fixed by unwrapping it in the route, `SQ-ROUTE-006`, rather than fighting Quine's `structure` config further). A full cycle — evidence written, completing edge triggers the match, `Investigation` node and `INVESTIGATES` edge appear in Quine, `200 OK` from MODOK — is confirmed working with no manual query in between.
11. ✅ **Python-computed `idFrom()` values silently broke every `node_exists`-gated write** (§ Second Live Verification Pass) — mechanical anchor linking's `Feature`/`ErrorSignature` existence checks, `ingest_pr`'s `IMPLEMENTED_IN`/`RESOLVED_BY` existence checks, and the `Investigation` dedup check plus GitHub write-back's `CustomerIssue` lookup all silently failed against real Quine because they compared a Quine-assigned UUID against a Python-computed SHA-256 int64 that Quine never produces. Fixed via `node_exists_by_parts`/`replace_edges_by_parts` (embed `idFrom()` in the query text, mirroring `write_edge_by_parts`) and, for the `CustomerIssue` lookup specifically, a property-match query. None of this was visible to the unit test suite, which mocks node existence directly and never exercises real ID computation.

### Deferred

1. ~~`ticket_kind` (bug vs. feature-request) classification is out of scope~~ — **resolved**: implemented via GitHub issue labels rather than text classification. See `docs/llds/github-ingestion.md § Ticket Kind from Labels`.
2. **A ticket that mechanically matches on one anchor type but not the other never gets the missing type filled in.** E.g. a ticket whose text happens to contain an exact registered error string, but also mentions an unrelated feature by name only loosely (no token overlap), gets `HAS_ERROR` from mechanical linking and no `AFFECTS` at all — the LLM fallback never runs because the gate is "both empty," not "either empty." Accepted trade-off (see Decisions & Alternatives) since exact mechanical matches are expected to be rare in the first place; revisit if this proves to matter in practice.
3. **Multi-row `PostToEndpoint` semantics** — whether Quine batches multiple enrichment-query result rows into one POST or sends one POST per row was not exercised (only single-row matches were tested live). The route handles both shapes defensively (§ Standing Query Result Route), but which one actually happens for a multi-combination match (`SQ-INV-006`) is unconfirmed.
4. **Poll interval tuning** — 30s default is a demo-friendly guess, not derived from GitHub rate-limit budgets at any particular scale. Fine for a single-repo local demo; revisit if this were ever used against a busy repo.
5. **`Investigation.status` lifecycle** — v1 only ever writes `"open"`; there is no transition to `"resolved"`/`"stale"` and no code that would drive one. There is also no handling for evidence *retraction* — if a `HAS_ERROR` or `RESOLVED_BY` edge that contributed to a match is later removed (e.g. a mechanical-anchor-linking re-run via `replace_edges` drops a stale anchor), the `Investigation` node that already fired is not revisited or marked stale. Deferred to the longer-term `AgentRun`/workflow-transition work explicitly excluded from this increment.
6. **A GitHub write-back failure on first delivery is not retried.** Since step 1 of the `investigation` branch treats "the `Investigation` node already exists" as "nothing left to do," a GitHub API error on the *first* successful match means that particular comment is never posted — a later redelivery of the identical match (if Quine ever resends it) would see the existing node and skip straight past the notification step. Accepted for v1: the `Investigation` node itself (MODOK's authoritative record) is never lost or duplicated; only the external notification can be silently missed on a first-attempt failure. A future fix would track notification success separately from node existence.
7. **Multiple evidence combinations for one `CustomerIssue`** — if a ticket's `raw_text` mechanically links to two different `ErrorSignature`s, each covered by its own `KnownIssue`+`Fix`, the enrichment query (not itself `DistinctId`-constrained) can return multiple rows for the same underlying match event. This is treated as intended, not a bug: each `(known_issue_id, fix_id)` combination gets its own `Investigation` node (distinct `investigation_id`) and, if GitHub-sourced, its own comment. Confirmed as desired behavior, not raised for user triage, since it correctly reflects "this issue matched N distinct actionable patterns."

## References

- `docs/high-level-design.md § Detection / Trigger Path`, `§ Authority model`, `§ Key Design Decisions #10`
- `docs/llds/quine-client.md` — `idFrom()` scheme, `write_edge_by_parts`, connection/retry behavior this component reuses unchanged
- `docs/llds/diagnostic-retrieval-engine.md` — `retrieve()`, reused unchanged for packet assembly
- `docs/llds/webhook-receiver.md § Pull adapter`, `§ PUSH_ADAPTERS/PULL_ADAPTERS` — protocols this component implements without modification
- `docs/llds/github-ingestion.md` — `GithubIngester`, reused unchanged except one added call in `ingest_issue`
- `docs/llds/ingestion-pipeline.md` — known-issue MODOK block, extended with two new fields/edges (see that document for the updated schema)
- Quine standing queries: https://docs.quine.io/components/writing-standing-queries.html
- Quine REST API (v1): https://docs.quine.io/reference/rest-api.html
