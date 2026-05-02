## Offline CEGIS for Prompt Improvement

In addition to bounded runtime repair, MODOK should use an offline CEGIS-style loop to improve the ticket parsing and metadata proposal prompts over time.

The goal is not to let the model rewrite its own prompts in production. The goal is to collect failures, reduce them into concrete counterexamples, and use those counterexamples to improve the fixed prompt templates and regression corpus.

This fits the current LLM Gateway design because prompts are fixed templates in `modok/llm/prompts.py`, which keeps them auditable and testable. The gateway also returns proposals rather than writing to Quine or doc files directly, so offline evaluation can safely exercise the full inference path without mutating real state.

## Runtime CEGIS vs Offline CEGIS

| Loop | Purpose | Runs When | Changes Production Behavior? |
|---|---|---|---|
| Runtime CEGIS | Repair one bad proposal | During `parse_ticket` or `propose_metadata` | Only within a bounded call |
| Offline CEGIS | Improve prompts and validators | In tests / CI / local eval | Only after a human-reviewed prompt change |

## Offline Loop

```text
golden corpus
   │
   ▼
run current prompt against fixtures
   │
   ▼
validate output mechanically
   │
   ▼
compare against expected parse/proposal
   │
   ▼
emit counterexamples
   │
   ▼
update prompt, verifier, or fixture
   │
   ▼
rerun regression suite
```

## What We Store

```text
tests/fixtures/llm_gateway/tickets/
  ticket_timeout_001.txt
  ticket_auth_region_mismatch_001.txt
  ticket_ambiguous_feature_001.txt

tests/golden/llm_gateway/tickets/
  ticket_timeout_001.yaml
  ticket_auth_region_mismatch_001.yaml
  ticket_ambiguous_feature_001.yaml

tests/fixtures/llm_gateway/docs/
  known_issue_missing_metadata_001.md
  feature_doc_missing_owner_001.md

tests/golden/llm_gateway/metadata/
  known_issue_missing_metadata_001.yaml
  feature_doc_missing_owner_001.yaml
```

## Example Ticket Counterexample

```yaml
case_id: ticket_timeout_001
input: tests/fixtures/llm_gateway/tickets/ticket_timeout_001.txt

expected:
  feature_slug: retrieval
  error_signatures:
    - "Standing query timed out after 30s"
  environment:
    quine_backend: rocksdb
  symptoms:
    - "MODOK retrieve returns no candidates after ingestion"

actual:
  feature_slug: quine
  error_signatures:
    - "timeout"
  environment: {}
  symptoms:
    - "query failed"

counterexamples:
  - field: feature_slug
    reason: "The model chose the implementation substrate instead of the MODOK feature area."
    expected_behavior: "Prefer MODOK feature slugs over internal dependency names unless the ticket is specifically about the dependency."

  - field: error_signatures[0]
    reason: "The model summarized the error instead of extracting the exact signature."
    expected_behavior: "Prefer exact error strings when present in the ticket."

  - field: environment.quine_backend
    reason: "The ticket explicitly mentions RocksDB but the parse omitted it."
    expected_behavior: "Extract supported environment fields when directly stated."
```

## Example Metadata Counterexample

```yaml
case_id: known_issue_missing_metadata_001
input: tests/fixtures/llm_gateway/docs/known_issue_missing_metadata_001.md

missing_fields:
  - feature_slug
  - severity
  - error_signatures

expected:
  proposed_fields:
    feature_slug: ingestion
    severity: medium
    error_signatures:
      - "frontmatter validation failed: missing known_issue_id"

actual:
  proposed_fields:
    feature_slug: docs
    severity: high
    owner: platform

counterexamples:
  - field: proposed_fields.feature_slug
    reason: "The proposed feature slug is not supported by the document body."
    expected_behavior: "Infer feature_slug from the behavior described, not from the file being a document."

  - field: proposed_fields.severity
    reason: "The model escalated severity without evidence."
    expected_behavior: "Do not infer high severity unless impact language is present."

  - field: proposed_fields.owner
    reason: "The model proposed a field that was not requested."
    expected_behavior: "Only propose fields listed in missing_fields."
```

## CLI Shape

```bash
modok eval llm-gateway --case ticket-parsing
modok eval llm-gateway --case metadata
modok eval llm-gateway --case all
```

Useful options:

```bash
modok eval llm-gateway \
  --case ticket-parsing \
  --backend local \
  --emit-counterexamples \
  --write-report reports/llm-gateway-eval.md
```

And for comparing prompt revisions:

```bash
modok eval llm-gateway \
  --case all \
  --prompt-version current \
  --compare-to main
```

## Metrics

Track both correctness and behavior quality.

```text
Ticket parsing:
- schema pass rate
- feature_slug exact match rate
- exact error signature recall
- exact error signature precision
- environment field precision/recall
- unsupported hallucination count
- ambiguous cases correctly left blank

Metadata proposal:
- schema pass rate
- missing_fields compliance
- enum validity
- existing-field overwrite attempts
- evidence-supported proposal rate
- hallucinated field count
- accepted proposal rate
```

## How Prompt Updates Happen

Prompt updates should be boring and reviewable:

```text
1. Add or update failing fixture.
2. Add expected golden output.
3. Run eval and capture counterexample.
4. Update fixed prompt template in `modok/llm/prompts.py`.
5. Rerun all evals.
6. Accept only if the new prompt improves the target case without regressing old cases.
```

Do not automatically mutate prompts from eval output. The model may help draft a better prompt, but the repository should only accept prompt changes through normal code review.

## What This Gives Us

Offline CEGIS turns LLM quality into an engineering loop:

```text
bad parse
  → concrete counterexample
  → regression fixture
  → prompt/verifier update
  → measurable improvement
```

Instead of saying “the model is flaky,” we get specific failures like:

```text
- summarized exact error strings
- invented feature slugs
- proposed unrequested metadata fields
- treated Quine internals as MODOK feature areas
- inferred severity without evidence
- failed to extract explicitly stated environment
```

Those are fixable, testable behaviors.
