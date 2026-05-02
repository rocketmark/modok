# `--fix` Metadata Proposal Flow

## Purpose

The `--fix` workflow helps MODOK repair missing or invalid document metadata without turning the LLM into a writer.

When MODOK finds a document with incomplete frontmatter, it can ask the LLM Gateway to propose values for the missing fields. The proposal is then mechanically validated. Only the caller decides whether to apply the fix.

The core rule is:

```text
LLM proposes.
Verifier checks.
Caller writes.
Gateway never writes.
```

## Scope

This document covers the interactive `--fix` path for metadata proposal.

It does not cover:

- ticket parsing
- similarity proposal
- offline prompt improvement
- Quine writes
- automatic prompt mutation
- autonomous document editing

## Entry Point

The `--fix` workflow uses the LLM Gateway metadata proposal call:

```python
async def propose_metadata(
    doc_path: Path,
    frontmatter: dict,
    missing_fields: list[str],
) -> MetadataProposal
```

The caller provides:

```text
- the document path
- existing frontmatter
- the list of fields that are missing or invalid
```

The gateway returns:

```python
@dataclass
class MetadataProposal:
    proposed_fields: dict[str, Any]
    confidence: float
    evidence: str
    raw_response: str
```

The gateway does not modify the document.

## High-Level Flow

```text
modok ingest --fix
   │
   ▼
parse document frontmatter
   │
   ▼
detect missing or invalid metadata
   │
   ▼
call LLM Gateway propose_metadata(...)
   │
   ▼
validate proposal mechanically
   │
   ├── valid proposal
   │      │
   │      ▼
   │   caller applies patch
   │
   └── invalid proposal
          │
          ▼
      optional bounded repair attempt
          │
          ▼
      validate repaired proposal
          │
          ├── valid → caller applies patch
          └── invalid → warn and write nothing
```

## Design Principle

`--fix` should feel helpful, but it must be conservative.

It is better to leave metadata unfixed than to write plausible but unsupported values.

The LLM may infer metadata, but every accepted proposal must satisfy deterministic checks.

## Proposal Rules

The LLM Gateway may propose values only for fields the caller explicitly requested.

For example, if the caller provides:

```yaml
missing_fields:
  - feature_slug
  - error_signatures
```

Then this is valid:

```yaml
proposed_fields:
  feature_slug: ingestion
  error_signatures:
    - "frontmatter validation failed: missing known_issue_id"
```

This is invalid:

```yaml
proposed_fields:
  feature_slug: ingestion
  error_signatures:
    - "frontmatter validation failed: missing known_issue_id"
  owner: platform
```

The `owner` field was not requested, so it must be rejected.

## Verifier Rules

The verifier is deterministic code that checks the LLM proposal before any write occurs.

### Hard Checks

```text
- proposal must match the MetadataProposal schema
- proposed_fields must be a dictionary
- confidence must be a number between 0.0 and 1.0
- evidence must be present
- every proposed field must appear in missing_fields
- existing frontmatter fields must not be overwritten
- every proposed value must have the correct type
- enum fields must use known enum values
- slug fields must use known canonical slugs
- relationship fields must use known relationship names
- list fields must be deduplicated
- empty values must be rejected unless explicitly allowed
```

### Evidence Checks

```text
- every proposed value must be supported by at least one source:
  - document path
  - filename
  - document body
  - existing frontmatter

- evidence must explain why the value was proposed
- evidence must not be generic filler
- values inferred only from vibes must be rejected
```

### Safety Checks

```text
- the gateway must not write to the file
- the gateway must not write to Quine
- invalid proposals must not be partially applied
- failed repair must degrade to a warning
- raw_response must not be persisted by the gateway
```

## Bounded CEGIS Repair

The `--fix` workflow may use a short CEGIS-style repair loop.

This loop repairs one proposal. It does not update prompts or learn globally.

```text
document + frontmatter + missing_fields
   │
   ▼
LLM proposes metadata
   │
   ▼
verifier checks proposal
   │
   ├── valid → caller may apply patch
   │
   └── invalid
          │
          ▼
      counterexample generated
          │
          ▼
      LLM gets one repair attempt
          │
          ▼
      verifier checks repaired proposal
          │
          ├── valid → caller may apply patch
          └── invalid → warn and write nothing
```

Recommended config:

```toml
[llm]
cegis_fix_enabled = true
cegis_max_iterations_propose_metadata = 1
```

Because `--fix` is interactive, the repair loop should stay short.

## Counterexample Format

When verification fails, the caller should produce concrete counterexamples.

Example:

```yaml
counterexamples:
  - field: proposed_fields.feature_slug
    reason: "The proposed feature_slug is not in the known feature registry."
    bad_value: "docs"
    allowed_values:
      - ingestion
      - retrieval
      - llm-gateway
      - quine-client
    repair_instruction: "Choose an allowed feature_slug only if supported by the document. Otherwise omit the field."

  - field: proposed_fields.owner
    reason: "The model proposed a field that was not requested."
    bad_value: "platform"
    repair_instruction: "Only propose fields listed in missing_fields."
```

The repair prompt should include:

```text
- original document context
- existing frontmatter
- missing_fields
- previous proposal
- verifier counterexamples
- instruction to return a corrected MetadataProposal
```

## Failure Behavior

If the proposal cannot be validated after the bounded repair attempt, MODOK should write nothing.

The CLI should make the failure clear and actionable.

Example output:

```text
Could not safely infer metadata for docs/known-issues/frontmatter-validation.md.

Missing fields:
- feature_slug
- error_signatures

Rejected proposal:
- feature_slug: docs
- error_signatures:
  - "validation error"

Reasons:
- feature_slug "docs" is not in the known feature registry
- error signature "validation error" is too vague and is not supported by the document text

No changes were written.
```

## Successful Fix Behavior

When a proposal passes validation, the caller may apply the patch.

Example:

```text
Detected missing metadata in docs/known-issues/frontmatter-validation.md.

Proposed fix:
- feature_slug: ingestion
- error_signatures:
  - "frontmatter validation failed: missing known_issue_id"

Evidence:
The document describes ingestion failing because required known issue metadata is absent.

Applied metadata fix.
```

The caller should write only the validated fields.

## Partial Success

If some fields are valid and others are invalid, the safest default is to reject the entire proposal.

Recommended default:

```text
all-or-nothing
```

That avoids writing a document that appears fixed but still has unresolved metadata issues.

A future enhancement could allow field-level acceptance, but v1 should keep the behavior simple and conservative.

## Caller Responsibilities

The caller owns:

```text
- detecting missing or invalid metadata
- constructing missing_fields
- calling propose_metadata(...)
- validating the proposal
- generating counterexamples
- optionally performing one repair attempt
- applying the final patch
- printing warnings
- deciding whether ingestion should continue
```

The gateway owns:

```text
- backend selection
- timeout handling
- retry handling
- structured JSON request/response
- pydantic validation
- returning MetadataProposal
```

The gateway does not own:

```text
- document mutation
- Quine writes
- frontmatter patching
- schema policy
- feature registry lookup
- relationship validation
- prompt improvement
```

## Recommended CLI Behavior

### Dry Run

```bash
modok ingest docs/known-issues/foo.md --fix --dry-run
```

Expected behavior:

```text
- detect missing fields
- propose metadata
- validate proposal
- print proposed patch
- write nothing
```

### Apply Fix

```bash
modok ingest docs/known-issues/foo.md --fix
```

Expected behavior:

```text
- detect missing fields
- propose metadata
- validate proposal
- apply patch only if valid
- continue ingestion only after metadata is valid
```

### Strict Mode

```bash
modok ingest docs/known-issues/foo.md --fix --strict
```

Expected behavior:

```text
- fail ingestion if metadata cannot be safely fixed
- write nothing on invalid proposal
```

### Non-Strict Mode

```bash
modok ingest docs/known-issues/foo.md --fix
```

Expected behavior:

```text
- warn if metadata cannot be safely fixed
- write nothing for that document
- caller decides whether to skip or continue
```

## Example

Input document:

```markdown
---
doc_type: known_issue
known_issue_id: known-issue-frontmatter-validation
---

# Frontmatter validation fails when known issue ID is missing

During ingestion, MODOK rejects known issue documents that do not include
a `known_issue_id`.

The CLI reports:

`frontmatter validation failed: missing known_issue_id`
```

Missing fields:

```yaml
missing_fields:
  - feature_slug
  - error_signatures
```

Valid proposal:

```yaml
proposed_fields:
  feature_slug: ingestion
  error_signatures:
    - "frontmatter validation failed: missing known_issue_id"
confidence: 0.88
evidence: "The document describes an ingestion-time frontmatter validation failure and quotes the exact error string."
```

Invalid proposal:

```yaml
proposed_fields:
  feature_slug: docs
  owner: platform
  error_signatures:
    - "validation error"
confidence: 0.92
evidence: "The document is about validation."
```

Rejection reasons:

```text
- feature_slug "docs" is not a known feature slug
- owner was not requested in missing_fields
- "validation error" is vague and not the exact quoted error
```

## Tests

Add unit tests for the verifier:

```text
- rejects fields not listed in missing_fields
- rejects overwriting existing frontmatter
- rejects unknown feature slugs
- rejects wrong value types
- rejects unsupported enum values
- rejects vague error signatures when exact signatures exist
- rejects missing evidence
- accepts valid proposal with supported evidence
```

Add integration tests for `--fix`:

```text
- applies valid metadata proposal
- writes nothing when proposal fails validation
- writes nothing when repair fails validation
- performs at most one repair attempt
- preserves existing frontmatter
- supports --dry-run without writing
- reports useful rejection reasons
```

## Metrics

Track:

```text
- proposal validation pass rate
- repair success rate
- rejected proposal count
- unsupported field proposal count
- unknown slug proposal count
- evidence failure count
- dry-run proposal count
- applied fix count
- no-change warning count
```

## Relationship to Offline Prompt Improvement

The `--fix` loop improves one interactive proposal.

Offline prompt improvement is separate.

```text
--fix CEGIS:
  bad proposal
    → bounded repair
    → accept or warn

offline CEGIS:
  bad proposal
    → regression fixture
    → prompt/verifier update
    → eval suite
```

The `--fix` workflow must not dynamically update prompts.

Any prompt changes should happen through normal development flow:

```text
1. capture failure
2. create fixture
3. add expected output
4. update prompt or verifier
5. rerun evals
6. review and merge
```

## Summary

The `--fix` workflow should make metadata repair convenient without weakening MODOK's write discipline.

The safe shape is:

```text
LLM proposes metadata.
Deterministic verifier checks it.
One bounded repair attempt is allowed.
Caller writes only validated fields.
Invalid proposals produce warnings, not partial writes.
```
