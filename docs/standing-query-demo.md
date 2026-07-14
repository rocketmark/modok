# Standing Query Demo Script

Demonstrates the claim in `docs/high-level-design.md § Detection / Trigger Path`: MODOK can continuously recognize when independently-arriving evidence forms an actionable investigation pattern, and advance the workflow the moment that pattern becomes true — no manual `retrieve`/`diagnose` call, no polling.

Assumes `docs/setup.md` (platform: Quine, LLM backend, `modok` installed) and `docs/project-setup.md` (a project registered, registries bootstrapped, first ingestion run) are already done. Uses the `stagehand` project as the running example; substitute your own project slug and a real error signature from its `registries/errors.yml`.

## 1. Start Quine and MODOK

```bash
modok quine start
modok stream install     # idempotent — safe to re-run; reports "already installed" on repeat
modok serve              # in a second terminal; leave running
```

`modok stream status` should now list `actionable-issue-pattern`.

## 2. Seed the "already-documented" side of the pattern

Add a known-issue doc with the new `error_signatures`/`fixes` fields (`docs/llds/ingestion-pipeline.md § Known issue and fix blocks`), e.g. in `docs/known-issues/gss-failure.md`:

```markdown
## Known Issues

### GSS re-solve corrupts calibration

​```modok
kind: known_issue
id: ki-gss-failure
summary: GSS re-solve accepted despite MP_MAXITER, corrupts calibration
status: open
affects:
  - feature:calibration
error_signatures:
  - gss-failure
fixes:
  - fix-gss-maxiter-reject
​```
```

```bash
modok ingest --project stagehand
```

At this point the pattern is **not** actionable yet — no `CustomerIssue` mentions this error. Confirm:

```bash
modok --status   # KnownIssue and Fix counts increase; no Investigation nodes exist
```

## 3. Ingest the customer issue — evidence assembles, no match yet

Open (or simulate) a GitHub issue whose body mentions the error signature's exact text (e.g. `GSS_FAILURE`). Two ways to get it into MODOK, both hit the identical write path (`run_ingest_event`'s `customer_issue` branch) and both trigger mechanical anchor-linking automatically:

- **Live, no tunnel needed**: enable the poll adapter (`[webhook] github_poll_enabled = true` in config) and open the issue on GitHub — `modok serve` picks it up within one poll cycle (default 30s).
- **Direct**: `curl -XPOST localhost:4242/webhook/stagehand/ticket -H "Authorization: Bearer <bearer_token>" -d '{"ticket_id": "1", "summary": "...", "body": "Saw GSS_FAILURE during a resolve."}'`.

Anchor-linking writes `CustomerIssue -[:HAS_ERROR]-> ErrorSignature` automatically. Since the `KnownIssue`+`Fix` side was already ingested in step 2, **this single write completes the pattern** — the standing query fires immediately.

## 4. Observe the result — no manual query

Watch `modok serve`'s log for `POST /standing-query/result` (this is Quine calling MODOK, not the other way around), then:

```bash
modok --status   # a new Investigation node is now present
```

If the `CustomerIssue` came from GitHub and `GITHUB_TOKEN` + `github_repo` are configured, the debug packet (summary, known issues, fixes, relevant files) appears as a comment on the GitHub issue within moments — this is the DRE's usual `retrieve()` output, generated automatically instead of by a person running `modok retrieve`.

## 5. Prove order independence

Reverse the order: ingest a *different* customer issue mentioning a *new* error signature first (no match — the known-issue/fix side doesn't exist yet), then add the known-issue doc with that error afterward and re-run `modok ingest`. The standing query fires on the second write this time, not the first — whichever piece of evidence completes the pattern triggers it, regardless of arrival order.

## 6. Reset and replay

```bash
modok stream remove
modok quine stop
rm -rf ~/.modok/data/quine.db   # full reset — only if you want a clean graph; otherwise skip
modok quine start
modok stream install
```

Re-running steps 2–4 with the same IDs is idempotent: re-ingesting the same doc or the same ticket does not create duplicate nodes, and a redelivered standing-query match does not create a duplicate `Investigation` (`SQ-INV-005`) or repost the GitHub comment.

## What this proves

| Claim | Where it's shown |
|---|---|
| Quine detects the pattern the instant it's true, not on a schedule | Step 3 — no code re-runs a query; the write itself triggers it |
| Order of evidence arrival doesn't matter | Step 5 |
| MODOK, not a human, decides an investigation is warranted | Step 4 — `Investigation` node appears with no `retrieve`/`diagnose` call |
| The trigger is fully explainable | `Investigation.investigation_id` encodes exactly which ticket, known issue, fix, and standing query fired; `INVESTIGATES` names the issue |
| Duplicate signals don't duplicate work | Step 6 |

## Verified vs. not yet exercised

The mechanics above (standing query fires on pattern completion, `CypherQuery` enrichment, `PostToEndpoint` delivery, MODOK writing the `Investigation` node) were confirmed against a real local Quine 1.10.0 instance during development — see `docs/llds/standing-queries.md § Live Verification Findings` for the three real bugs that surfaced only under live testing (none visible to the mocked test suite) and how they were fixed. The GitHub comment write-back itself was verified with a mocked GitHub API, not a real repository, in that same pass — confirm it against a real issue the first time you run this script for a live audience.
