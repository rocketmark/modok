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
    loader.py                      # load_definition(name) -> StandingQueryDefinition
    actionable_issue_pattern.yaml  # the v1 standing query
```

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
         ci.ticket_id AS ticket_id, ki.issue_id AS known_issue_id, fix.fix_id AS fix_id
output_name: investigation-trigger
```

Notes on this specific pattern:

- **No `:Label` syntax — filters on the `node_type` property instead.** Confirmed via live verification against a running Quine 1.10.0 (see § Live Verification Findings below): MODOK never gives nodes a real Quine label, only a `node_type` property (`SET n += {node_type: 'CustomerIssue', ...}`). `MATCH (n:CustomerIssue)` matches nothing against real Quine even when `n.node_type == "CustomerIssue"` — confirmed directly (`n.labels` comes back `[]`). The `WHERE ... node_type = '...'` clause here is not optional decoration; without it the standing query would never fire. This is a pre-existing gap across the *entire* MODOK Cypher surface (DRE, ingestion, `diagnose`), invisible to `DummyQuine`/mocked tests because none of them validate real Quine label semantics — out of scope to fix everywhere in this increment, but load-bearing enough in this one pattern that it had to be fixed here.
- **No `project_slug` filter.** Project isolation is already guaranteed by node topology, not by a WHERE clause: every node type's `idFrom()` address includes `project_slug` (`quine-client.md § ID Scheme`), so a `CustomerIssue` in project A can never share an `ErrorSignature` *node* with project B — the two nodes have entirely different addresses. This also means **one single standing query serves every project** sharing the Quine instance — `modok stream install` takes no `--project` flag, unlike most other CLI commands (see Decisions & Alternatives).
- **`RETURN DISTINCT id(ci) AS id`** satisfies DistinctId mode's constraint (exactly one value, the `id`/`strId` of a node bound in the `MATCH`) — the explicit `AS id` alias is required, not optional (see § Live Verification Findings).
- **Order independence is structural, not coded.** Nothing in the pattern assumes `CustomerIssue`, `KnownIssue`, or `Fix` arrives first — Quine's incremental evaluation fires on whichever write completes the pattern, regardless of order. Confirmed live: writing three of the four required edges produces no match; adding the fourth (tried both as the `CustomerIssue→ErrorSignature` edge and, separately, as the `KnownIssue→Fix` edge) fires it immediately.
- **`enrichment_query`** is the `CypherQuery` `andThen` stage: it re-fetches the identifying fields Quine's own output pipeline needs to build the `PostToEndpoint` body — `$that.data.id` is the result-field key produced by the pattern's `AS id` alias. This is *not* the full debug packet; it is the minimum needed to identify the match. The full packet is assembled by the Diagnostic Retrieval Engine after MODOK receives the callback (see § Standing Query Result Route).

### Live Verification Findings

Verified end-to-end against a running `~/.modok/quine.jar` (v1.10.0): `modok stream install`, real graph writes via raw Cypher completing the pattern, a real `modok serve` listening on `:4242`, confirmed by querying Quine afterward for the resulting `Investigation` node and `INVESTIGATES` edge. Three real bugs were found this way — none visible to `DummyQuine`/mocked tests, all now fixed:

1. **`node_type` is a property, not a label.** `MATCH (n:CustomerIssue)` matches nothing against real Quine even when `n.node_type == "CustomerIssue"` (`n.labels` comes back `[]`). Fixed: the pattern and `enrichment_query` filter on `WHERE n.node_type = '...'` instead of `:Label` syntax. This is a pre-existing gap across the *entire* MODOK Cypher surface (DRE, ingestion, `diagnose`) — out of scope to fix everywhere in this increment, but load-bearing enough in this one pattern that it had to be fixed here.
2. **An unaliased `RETURN DISTINCT id(ci)` produces a result-field key of `id(ci)`, not `id`.** Confirmed via a minimal `PrintToStandardOut`-output standing query showing the exact match payload Quine produces. `$that.data.id` in the `enrichment_query` therefore silently referenced a field that didn't exist. Fixed: `RETURN DISTINCT id(ci) AS id`.
3. **Quine's default output `structure` wraps the row as `{"meta": {...}, "data": {...}}`, not flat.** Confirmed by inspecting Quine's own error log for the failed `PostToEndpoint` attempt (visible only *after* fix #2 above let the pipeline get that far) — the delivered body was the fields directly, but MODOK's route expected them at the top level, not nested under `data`. Fixed on the MODOK side rather than by fighting Quine's `structure` config: the route unwraps `{"meta", "data"}`-shaped bodies (`SQ-ROUTE-006`), since a flat body must also continue to work and guessing Quine's exact `structure` semantics further was less reliable than handling both shapes.

With all three fixes applied, a full cycle was confirmed working: partial evidence written (no match) → the completing `KnownIssue -[:RESOLVED_BY]-> Fix` edge written → standing query fires → `CypherQuery` enrichment runs → `PostToEndpoint` delivers to `modok serve` → `POST /standing-query/result` returns `200 OK` → querying Quine directly shows the `Investigation` node (`investigation_id: "github-7000--ki-demo8--fix-demo8--actionable-issue-pattern"`, `status: "open"`, `trigger_type: "standing_query"`) and its `INVESTIGATES` edge to the `CustomerIssue`, with no manual `retrieve`/`diagnose` call at any point. Test data was cleaned up and Quine/the test server were stopped afterward, restoring original state. GitHub write-back itself (posting to a real issue) was not exercised in this pass — it depends on `GITHUB_TOKEN` and a real repo, and is covered by unit tests against a mocked GitHub API instead.

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
8. `ticket_kind` (bug vs. feature-request classification) is explicitly **out of scope** for this increment — see Open Questions.

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

1. Computes `investigation_id` and its Quine address. Checks `client.node_exists(investigation_address)` **first**. Because `investigation_id` fully encodes evidence identity (source_system, ticket_id, known_issue_id, fix_id, standing_query_name), an existing node means this exact match has already been recorded — the branch stops here, no re-upsert, no DRE call, no GitHub comment. This is what actually prevents duplicate GitHub comments on redelivery: node-write idempotency alone is not enough, since steps 3–4 below are side effects that a naive "upsert then always notify" design would repeat on every redelivery of the same match.
2. Only if the node did not already exist: upserts the `Investigation` node and writes `Investigation -[:INVESTIGATES]-> CustomerIssue`.
3. Resolves the `CustomerIssue`'s Quine node id via `idFrom('customer-issue', project_slug, source_system, ticket_id)` and calls `retrieve()` (`modok.retrieval.engine`) to assemble the debug packet — the same call `modok retrieve`/`diagnose` make, including its existing LLM Gateway summary step.
4. If `source_system == "github"`: looks up `github_repo` for `project_slug` (`ModokConfig.load()`) and `GITHUB_TOKEN` from the environment. If both present, formats the packet as markdown (`format_debug_packet_markdown`, new function in `src/modok/retrieval/formatting.py`) and calls `post_issue_comment` (new function in `src/modok/ingestion/github.py`, alongside the existing GitHub HTTP helpers) to `POST /repos/{github_repo}/issues/{ticket_id}/comments`.
5. Any failure in steps 3–4 (LLM unavailable, GitHub API error, missing token) is caught, logged to stderr, and does **not** roll back the `Investigation` write from step 2. The graph write is MODOK's authoritative record regardless of whether the external notification succeeded — matching the HLD's authority model. Note this does mean a GitHub API failure on first delivery is not retried on a later redelivery of the same match (step 1 would now see the node and stop) — accepted for v1; see Open Questions.

## GitHub Write-Back

`post_issue_comment(github_repo: str, token: str, issue_number: str, body: str) -> None` — one `httpx` POST, same header shape (`Authorization: Bearer`, `Accept: application/vnd.github+json`) as `GithubIngester` already uses. Best-effort: any non-2xx response or exception is logged, never raised past this function.

`format_debug_packet_markdown(packet: DebugPacket, investigation_id: str, standing_query_name: str) -> str` renders:

```markdown
## 🔎 MODOK investigation triggered

Standing query `{standing_query_name}` matched this issue against existing graph evidence.

**Summary:** {packet.summary}

**Known issues:** {for each: `- {id}: {summary}`}
**Prior fixes:** {for each: `- {id} ({commit}): {summary}`}
**Relevant files:** {up to N, as a list}
**Relevant tests:** {up to N, as a list}

_Investigation: `{investigation_id}`_
```

Exact field ordering/omission-when-empty mirrors `diagnose.py`'s `_print_packet` (skip a section header entirely when its list is empty).

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

Gated by `WebhookConfig.github_poll_enabled` (new field, default `False`) — off unless a project opts in, consistent with `enabled_sources` gating push adapters. Applies to every configured project with `github_repo` set and `GITHUB_TOKEN` present in the environment; projects without those are silently skipped (not an error — most MODOK projects aren't GitHub-backed).

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
| `ticket_kind` (bug vs. feature-request) classification | Dropped from this increment entirely | LLM-classified as part of the same `parse_ticket` call; mechanical keyword heuristic (fixed word list) | User-selected: keyword heuristics for free-form sentence *shape* (unlike entity-name matching) would misfire often enough to not be worth adding yet; deferred as a distinct, separate increment |

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

### Deferred

1. **`ticket_kind` (bug vs. feature-request) classification** is out of scope for this increment — no field, mechanical or LLM-derived, records it. A future increment could add it as a `CustomerIssue` property, likely riding the same `parse_ticket` call the LLM fallback classifier already makes, once a reliable enough signal (LLM-based, since mechanical keyword heuristics for sentence *shape* were rejected as too unreliable) is designed.
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
