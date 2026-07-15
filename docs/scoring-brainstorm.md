# MODOK Evidence Weighting Rubric

Evidence should be weighted by how strongly it points to a specific place to investigate.

The score is not "truth."
The score is "inspection priority."

A high-scoring result should mean:

> "This is probably one of the first places an engineer or agent should inspect."

---

## Core principle

Do not start with:

> "How important is this file?"

Start with:

> "What evidence points here, how specific is it, how direct is it, and can the agent act on it?"

The best MODOK result is not the node with the most graph edges.

The best MODOK result is the candidate with the strongest explainable evidence bundle.

---

## Core dimensions

Each evidence item should be evaluated across these dimensions:

1. Specificity
2. Directness
3. Source reliability
4. Recency
5. Frequency / corroboration
6. Actionability
7. Negative evidence

---

## 1. Specificity

How narrow is the evidence?

More specific evidence gets more weight.

| Evidence | Weight |
|---|---:|
| Exact function/class/module name match | Very high |
| Exact error message match | Very high |
| Exact config key / feature flag / endpoint match | High |
| Same subsystem/package | Medium |
| Same broad domain | Low |
| Generic keyword overlap | Very low |

Example:

- `TimeoutException in RetryPolicy.apply()` is strong.
- `timeout` mentioned somewhere in docs is weak.

Specificity matters because MODOK should avoid sending the agent into a giant neighborhood of vaguely related things.

---

## 2. Directness

How close is the evidence to the thing being investigated?

| Evidence path | Weight |
|---|---:|
| Ticket directly names code symbol | Very high |
| Ticket error appears in code/logging path | Very high |
| Prior ticket with same error was fixed by this file | Very high |
| Runbook says this component owns this failure mode | High |
| Related doc links to related subsystem | Medium |
| Shared tag/category only | Low |

Direct evidence should beat broad contextual evidence.

A direct edge like:

```text
Ticket -> mentions_symbol -> CodeUnit
```

should count more than:

```text
Ticket -> has_tag -> Payments -> contains -> CodeUnit
```

---

## 3. Source reliability

Some sources are more trustworthy than others.

Suggested order:

| Source | Reliability |
|---|---:|
| Source code | Highest |
| Tests | Very high |
| Recent merged PRs / commits | Very high |
| Incident tickets with confirmed fix | Very high |
| Runbooks / operational docs | High |
| Design docs | Medium-high |
| Customer ticket text | Medium |
| LLM-extracted metadata | Medium-low unless validated |
| Generic docs / README prose | Low-medium |
| Keyword/vector recall only | Low |

Important rule:

> LLM-extracted claims should not be treated the same as parsed source-code facts.

For example:

```text
Parsed import graph says file A depends on file B
```

should weigh more than:

```text
LLM summary says this file seems related to billing
```

---

## 4. Recency

Newer evidence usually matters more, especially for active debugging.

| Evidence | Weight |
|---|---:|
| Code changed recently near same symbol | High |
| Recent ticket with same error | High |
| Recent incident involving same component | High |
| Old ticket fixed two years ago | Medium-low |
| Stale design doc | Low unless still linked from active docs |

But recency should be a modifier, not the main signal.

A recent vague keyword match should not beat an older exact prior fix.

Good rule:

```text
specificity and directness dominate;
recency breaks ties.
```

**A recent commit is not, by itself, a first-class evidence item.** "Recent commit touched candidate and related symbol" (see weights below) is strong because of the *related symbol* half — the commit's diff overlaps something already implicated by other evidence (a matched function, an anchored error). Strip that half away and a bare "this file was in a commit sometime recently, unrelated to anything else about this ticket" is not stronger than generic keyword overlap — it should carry roughly the same low weight, and multiple such commits on the same file should not each add their own evidence slot (that's just one file being frequently edited, not corroborating evidence). Do not let recency alone earn its own corroboration-bonus type; it should only stack additional weight when it is tied to a specific, already-anchored symbol.

---

## 5. Frequency / corroboration

Multiple independent signals should increase confidence.

For example, this is strong:

```text
Ticket mentions symbol X
Ticket error appears in file Y
Prior fix for same error modified file Y
Runbook for this failure mode points to component Y
Tests for file Y cover the failing behavior
```

But avoid blindly rewarding duplicate evidence.

Five docs all copying the same stale statement should not count as five independent confirmations.

So distinguish:

```text
corroborated evidence > repeated evidence
```

Recommended rule:

```text
same evidence type has diminishing returns
different evidence types stack strongly
```

Example:

```text
3 exact symbol matches:
  useful, but diminishing

1 symbol match + 1 prior fix + 1 test match + 1 runbook match:
  much stronger
```

---

## 6. Actionability

Does the evidence point to something the agent can actually inspect or change?

| Evidence | Weight |
|---|---:|
| Specific file/function/test to inspect | High |
| Specific owner/component/runbook | Medium-high |
| Specific prior fix | High |
| Abstract architecture concept | Medium |
| Broad product area | Low |

MODOK should prefer evidence that helps the next step.

A result is better when it can say:

```text
Look at src/retries/policy.py, especially RetryPolicy.apply().
There is also a prior fix in PR #412 and a test in tests/test_retry_policy.py.
```

rather than:

```text
This seems related to reliability.
```

---

## 7. Negative evidence

Negative evidence should reduce score.

Examples:

| Negative signal | Effect |
|---|---:|
| File/component is deprecated | Strong penalty |
| Code path is not reachable from affected endpoint | Strong penalty |
| Prior ticket looked similar but had different root cause | Medium penalty |
| Test/docs say this behavior is owned elsewhere | Medium penalty |
| Evidence comes only from stale generated docs | Medium penalty |
| Candidate is too broad, like repo root or top-level package | Penalty |

This keeps MODOK from ranking obvious-but-useless hubs too highly.

For example:

```text
utils/
common/
core/
shared/
```

These often look connected to everything. They need hub penalties unless there is very specific evidence.

---

# Recommended scoring model

Use a two-layer model:

1. Evidence items get individual weights.
2. Candidate locations get aggregated scores from their evidence.

---

## Evidence item shape

Each evidence item should carry:

```text
type
source
target
specificity
directness
reliability
recency
actionability
confidence
explanation
```

Example:

```json
{
  "type": "same_error_message",
  "source": "ticket",
  "target": "src/payments/retry_policy.py",
  "specificity": 5,
  "directness": 5,
  "reliability": 4,
  "recency": 4,
  "actionability": 5,
  "confidence": 0.92,
  "explanation": "The ticket error exactly matches an error raised by RetryPolicy.apply()."
}
```

---

## Simple formula

A practical first scoring formula:

```text
evidence_score =
  base_weight(type)
  * specificity_multiplier
  * directness_multiplier
  * reliability_multiplier
  * recency_multiplier
  * actionability_multiplier
  * confidence
```

Then:

```text
candidate_score =
  sum(top evidence scores)
  + corroboration_bonus
  - hub_penalty
  - stale_penalty
  - contradiction_penalty
```

Do not sum unlimited evidence. Cap or dampen repeated evidence.

Example:

```text
candidate_score =
  sum(distinct_evidence_scores)
  + 3 * number_of_independent_evidence_types
  - penalties
```

---

# Suggested starting weights

| Evidence type | Base weight |
|---|---:|
| Exact error message appears in code/logs | 10 |
| Ticket directly mentions symbol/function/class | 10 |
| Stack trace points to candidate | 10 |
| Prior confirmed fix modified candidate | 9 |
| Test directly covers failing behavior | 8 |
| Runbook maps failure mode to component | 8 |
| Recent commit touched candidate **and** an already-anchored symbol | 7 |
| Ticket mentions endpoint owned by candidate | 7 |
| Design doc maps feature to component | 5 |
| Same customer / tenant had prior issue here | 4 |
| Same broad component tag (e.g. reached only via a feature/module rollup, not a direct edge) | 3 |
| Recent commit touched candidate, no other established relevance | 1 |
| Keyword overlap only | 1 |
| Vector similarity only | 1-3 |

Note the two "recent commit" rows above are deliberately 7 points apart. The high one requires corroboration with a specific symbol; the low one is what's left when you strip that corroboration away. An implementation that scores every recently-touched file at the high weight — regardless of whether the commit touched anything relevant — will let frequently-edited-but-unrelated files (health-check scripts, chroot/build tooling, anything under constant maintenance) outrank a candidate that is directly named in the ticket but hasn't been touched recently. This is not a hypothetical: it is the failure mode that motivated this note.

---

# MODOK evidence-type mapping

MODOK's graph does not have stack traces, runbooks, endpoint ownership, or tenant history — the schema is `CustomerIssue` / `Feature` / `Module` / `File` / `TestFile` / `Commit` / `KnownIssue` / `Fix` / `ErrorSignature`. Translating the generic types above onto what the DRE (`src/modok/retrieval/engine.py`) actually produces:

| MODOK evidence type | Maps to | Why |
|---|---|---|
| `ticket_mention` | "Ticket directly mentions symbol/function/class" (10) | File path named verbatim in ticket text |
| `element_anchor_match`, `function_anchor_match` | "Exact symbol/function match" territory — high specificity (1.5x), one-hop directness (1.2x) | Registered element or git-hunk function def token-matches an anchored term |
| `feature_primary_file` | "File/module-level match" (specificity 1.25x) | A source file in the feature's *own* declared `source_files` list (registry-curated) — narrower and more trustworthy than "reachable via some module of this feature" |
| `feature_anchor` | "Same broad component tag" (3) — **not** an exact match | `Feature -[IMPLEMENTED_BY]-> Module -[DEFINED_IN]-> File` is a two-hop rollup (0.8x directness at best), the same shape as the rubric's `Ticket -> has_tag -> Payments -> contains -> CodeUnit` broad-category example. Only files reachable *solely* through a module (not in the feature's own `source_files`) get this weaker tier |
| `test_coverage` | "Test directly covers failing behavior" (8) | Direct `Feature -[HAS_TEST]-> TestFile` edge |
| `recent_commit` (correlates with a `function_anchor_match` on the same commit) | "Recent commit + related symbol" (7) | |
| `recent_commit` (file touched, no anchored symbol in that commit's hunks) | "Recent commit, no other relevance" (1) | See recency note above |
| `commit_message_match` | "Prior confirmed fix modified candidate" (9) | The commit's own message — not its diff — names the same thing the ticket describes. Distinct from `function_anchor_match`: a commit can be topically on-target (message) without its diff touching a matched symbol (e.g. an OS-image build script fix for "wifi provisioning" that never touches the application-level wifi logic file) |
| `doc_penalty` | Negative evidence — non-source file | |

`feature_anchor` is the one row worth calling out explicitly: it was originally implemented at a weight comparable to direct symbol-level evidence, which is the broad-category mistake this document warns against elsewhere. Splitting it into `feature_primary_file` (the feature's own curated files) and a demoted `feature_anchor` (everything else reachable via the module graph) fixes this without requiring every project's registry to keep an artificially narrow module list.

# Suggested multipliers

Use simple bounded multipliers at first.

## Specificity multiplier

| Specificity | Multiplier |
|---|---:|
| Exact symbol, error, endpoint, config key | 1.5 |
| File/module-level match | 1.25 |
| Component/subsystem match | 1.0 |
| Product area/domain match | 0.7 |
| Generic keyword match | 0.4 |

## Directness multiplier

| Directness | Multiplier |
|---|---:|
| Direct edge to candidate | 1.5 |
| One-hop inferred relationship | 1.2 |
| Two-hop relationship | 0.8 |
| Broad category relationship | 0.5 |

## Reliability multiplier

| Reliability | Multiplier |
|---|---:|
| Parsed source code / stack trace / test | 1.5 |
| Confirmed incident / merged PR | 1.4 |
| Runbook / operational doc | 1.2 |
| Design doc / architecture doc | 1.0 |
| Ticket prose | 0.9 |
| LLM-extracted metadata | 0.7 |
| Keyword/vector recall only | 0.4 |

## Recency multiplier

| Recency | Multiplier |
|---|---:|
| Current active ticket / current code | 1.3 |
| Last 30 days | 1.2 |
| Last 90 days | 1.1 |
| Last year | 1.0 |
| Older than one year | 0.7 |
| Known stale/deprecated | 0.3 |

## Actionability multiplier

| Actionability | Multiplier |
|---|---:|
| Points to exact file/function/test | 1.4 |
| Points to exact component/runbook/owner | 1.2 |
| Points to broad subsystem | 0.8 |
| Points to abstract concept only | 0.5 |

---

# Corroboration bonus

Reward independent evidence types.

Suggested formula:

```text
corroboration_bonus =
  3 * min(number_of_independent_evidence_types - 1, 4)
```

This gives:

| Independent evidence types | Bonus |
|---|---:|
| 1 | 0 |
| 2 | 3 |
| 3 | 6 |
| 4 | 9 |
| 5+ | 12 |

Independent evidence types might include:

```text
symbol_match
error_match
stack_trace_match
prior_fix_match
test_match
runbook_match
ownership_match
recent_commit_match
endpoint_match
```

Do not count duplicate copies of the same evidence as independent.

---

# Diminishing returns

Repeated evidence of the same type should have diminishing value.

Suggested formula:

```text
same_type_score =
  strongest_evidence
  + 0.5 * second_strongest
  + 0.25 * third_strongest
```

Ignore or heavily dampen the rest unless there is a good reason.

Example:

```text
5 keyword matches across docs
```

should not beat:

```text
1 exact stack trace match
```

---

# Penalties

## Hub penalty

Apply when a candidate is too broad or connected to everything.

Examples:

```text
src/common/
src/utils/
src/core/
shared/
root package
global config
```

Suggested penalty:

```text
hub_penalty =
  log(candidate_degree) * hub_factor
```

Simple starting version:

| Candidate type | Penalty |
|---|---:|
| Exact function | 0 |
| Specific file | 0-2 |
| Focused module | 2-4 |
| Broad package | 5-8 |
| Repo root / shared utility hub | 10+ |

## Stale penalty

Apply when the evidence is old, deprecated, or no longer linked from active systems.

| Staleness | Penalty |
|---|---:|
| Slightly old but still valid | 1-2 |
| Old and unconfirmed | 3-5 |
| Deprecated component | 8-12 |
| Removed/unreachable code | 15+ |

## Contradiction penalty

Apply when evidence points away from the candidate.

Examples:

```text
Candidate is not reachable from affected endpoint
Runbook says another component owns this failure mode
Prior similar ticket had a different root cause
Tests show this behavior is mocked or delegated elsewhere
```

Suggested penalty:

| Contradiction | Penalty |
|---|---:|
| Weak contradiction | 2-4 |
| Medium contradiction | 5-8 |
| Strong contradiction | 10-15 |

---

# Example ranked result

A strong candidate:

```text
1. src/payments/retry_policy.py
   Score: 42

   Why:
   - Exact error message match: "Retry budget exhausted"
   - Ticket mentions RetryPolicy
   - Prior confirmed fix for same error modified this file
   - tests/test_retry_policy.py covers the failing path
   - Runbook for payment retry failures points to this component

   Confidence:
   High

   Suggested next step:
   Inspect RetryPolicy.apply() and run tests/test_retry_policy.py
```

A weak candidate:

```text
7. src/common/timeouts.py
   Score: 9

   Why:
   - Shares generic keyword "timeout"
   - Connected to payments through common dependency

   Confidence:
   Low

   Suggested next step:
   Only inspect if higher-ranked candidates fail
```

---

# Implementation guidance

Start simple.

Do not train a model first.

Use deterministic scoring first so the system is explainable.

Recommended first version:

```text
candidate_score =
  sum(dampened_distinct_evidence_scores)
  + corroboration_bonus
  - hub_penalty
  - stale_penalty
  - contradiction_penalty
```

Each candidate should return:

```json
{
  "candidate": "src/payments/retry_policy.py",
  "score": 42,
  "confidence": "high",
  "evidence": [
    {
      "type": "same_error_message",
      "score": 15,
      "explanation": "Exact error message appears in RetryPolicy.apply()."
    },
    {
      "type": "prior_confirmed_fix",
      "score": 12,
      "explanation": "Prior confirmed ticket with same error modified this file."
    }
  ],
  "penalties": [
    {
      "type": "none",
      "score": 0
    }
  ],
  "suggested_next_step": "Inspect RetryPolicy.apply() and run tests/test_retry_policy.py."
}
```

---

# Calibration rule

After MODOK suggests candidates, compare against what engineers actually inspect or change.

Track:

```text
Was the top candidate inspected?
Was it changed?
Was it part of the final fix?
Was the true fix in the top 3?
Was the true fix in the top 10?
Which evidence types were misleading?
Which evidence types were decisive?
```

Then adjust weights.

Do not optimize only for top-1 accuracy.

For an investigation assistant, top-3 and top-10 recall matter a lot.

A good first target:

```text
The correct file/component should usually appear in the top 5,
with enough explanation for the agent to decide what to inspect first.
```

---

# Final rule

The MODOK score should answer:

> "Where should I look first, and why?"

Not:

> "What is mathematically guaranteed to be correct?"

A high score means:

```text
This candidate has a strong, specific, direct, reliable, recent, actionable,
and corroborated evidence bundle pointing to it.
```
