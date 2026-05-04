# LLM Gateway

## Context and Design Philosophy

The LLM Gateway is MODOK's controlled boundary between mechanical graph operations and language model inference. Every LLM call in the system routes through this component.

Four uses exist:

1. **Ticket parsing** — convert freeform customer issue text into structured anchors (feature slugs, error signatures, symptoms, mentioned files).
2. **Metadata proposal** — suggest missing frontmatter fields for a doc when `--fix` is specified.
3. **Similarity proposal** — given a new CustomerIssue, suggest candidate KnownIssue matches with evidence anchors.
4. **Packet summary** — given a resolved debug packet (files, errors, symptoms, matched elements), produce a single diagnostic sentence naming the most specific available signal.

One rule governs all four: **the gateway returns proposals, never writes**. No gateway output touches Quine or doc files directly; the caller owns the write decision.

## Interface

The gateway exposes a single async function per use case:

```python
async def parse_ticket(raw_text: str, project_slug: str, ...) -> TicketParseResult
async def propose_metadata(
    doc_path: Path,
    frontmatter: dict,
    missing_fields: list[str],
    repair_context: list[dict] | None = None,   # counterexamples from prior failed attempt
) -> MetadataProposal
async def propose_similarity(issue: CustomerIssue, candidates: list[KnownIssueSummary]) -> list[SimilarityProposal]
async def summarise_packet(
    issue_text: str,
    module_slugs: list[str],
    error_signatures: list[str],
    symptoms: list[str],
    relevant_files: list[str],
    relevant_tests: list[str],
    matched_elements: list[str],
    recent_commits: list[dict],
    known_issues: list[str],
    backend: str = "local",
) -> str
```

`propose_similarity` requires the caller to pass `candidates` — a pre-fetched list of `KnownIssueSummary` structs (id, summary, error_signatures). The gateway does not query Quine; the Diagnostic Retrieval Engine fetches candidates and passes them in. This keeps the gateway stateless.

`summarise_packet` returns a single plain string (the one-sentence summary). `matched_elements` is a list of registered element names that were confirmed to match the ticket's symptom/error tokens; when present, the prompt instructs the LLM to name them explicitly in the summary.

```python
@dataclass
class KnownIssueSummary:
    known_issue_id: str
    summary: str
    error_signatures: list[str]
```

All four are thin wrappers around `_chat_completion(messages, response_format)`, which handles backend selection, retry, and timeout.

## Backend Model

The gateway is backend-agnostic. It communicates via the OpenAI-compatible chat completions endpoint (`POST /v1/chat/completions`) with structured JSON output (`response_format: {type: "json_object"}`). Both Ollama and Claude/GPT-4 support this interface.

### Backend selection

```
local  →  Ollama (http://localhost:11434/v1)        default
remote →  configured provider endpoint + api_key    optional escalation
```

Backend is selected per-call by the `backend` parameter: `"local"` (default), `"remote"`, or `"auto"`.

`"auto"` runs local first, then escalates to remote if the local response fails pydantic validation. Remote is only attempted if configured; if not configured, `auto` behaves as `local`.

Note: Ollama exposes the OpenAI-compatible endpoint for any hosted model including Gemma. Gemma variants may not reliably honour `response_format: json_object`. The response validator attempts JSON extraction from raw text as a fallback before raising `LLMResponseError`, which handles this case without escalation for minor formatting deviations.

### Configuration (from `~/.modok/config.toml`)

```toml
[llm]
local_endpoint  = "http://localhost:11434/v1"
local_model     = "llama3.2"
remote_endpoint = "https://api.anthropic.com/v1"   # optional
remote_model    = "claude-sonnet-4-6"               # optional
remote_api_key  = ""                                # optional; read from env if absent
timeout_seconds = 30          # default; overridden per call type below
timeout_parse_ticket    = 30  # background-safe; can be slow
timeout_propose_metadata = 15 # interactive (--fix workflow); must feel fast
timeout_propose_similarity = 15  # interactive; surfaced to user
timeout_summarise_packet   = 30  # background; called after retrieval
max_retries     = 2
cegis_fix_enabled = true
cegis_max_iterations_propose_metadata = 1   # one repair attempt; total max 2 LLM calls
counterexample_fixture_dir = ""             # required when using --emit-counterexamples; points to modok's own tests/fixtures/llm_gateway/
```

Per-call-type timeouts take precedence over `timeout_seconds`. If a per-call-type key is absent, `timeout_seconds` is used.

API key is read from `remote_api_key` in config first, then from the environment variable `MODOK_LLM_API_KEY`. If neither is set and a remote call is attempted, the gateway raises `LLMConfigError`.

## Retry and Timeout

- Each attempt uses the per-call-type timeout (15s for interactive calls, 30s for background; see Configuration).
- On timeout or 5xx response: retry up to `max_retries` times with 1s fixed delay (no exponential — LLM calls are already slow; backoff adds negligible benefit).
- On 4xx (auth, rate limit): raise `LLMGatewayError` immediately without retry — these are caller/config errors, not transient failures.
- After all retries exhausted: raise `LLMUnavailableError`.
- `LLMResponseError` is always a hard exception; the caller decides whether to degrade gracefully. For `propose_metadata` the CLI catches and warns; for `parse_ticket` the caller falls back to an empty parse result. The gateway does not swallow errors.

## Structured Output

All three call types use `response_format: {"type": "json_object"}` to constrain output. The gateway validates the response against a pydantic model before returning. If the model ignores the format hint (common with smaller local models), the validator attempts to extract JSON from the raw text before raising. If validation fails after all retries, raises `LLMResponseError`.

### Prompt discipline

Each call type has a fixed system prompt template stored in `modok/llm/prompts.py`. Templates are frozen strings — no runtime prompt construction beyond interpolating the specific values (raw text, missing field names, etc.). This keeps the gateway testable and auditable.

## Response Types

### `TicketParseResult`

```python
@dataclass
class TicketParseResult:
    feature_slugs: list[str]   # validated against valid_slugs if provided; legacy "feature_slug" field accepted
    error_signatures: list[str]
    environment: dict[str, str]
    symptoms: list[str]
    confidence: float          # 0.0–1.0; model self-reported; 0.0 if absent
    raw_response: str          # in-memory only; caller logs if desired; not persisted by gateway
    mentioned_files: list[str] # file paths explicitly named in ticket text; empty list if none
```

`parse_ticket` accepts optional context parameters forwarded from the DRE: `valid_slugs`, `feature_slugs`, `module_slugs`, `feature_descriptions`, `module_descriptions`, `module_elements`, `module_source_files`. These are interpolated into the prompt to guide the LLM toward valid slugs and reduce hallucination. The gateway validates that returned `feature_slugs` are in `valid_slugs` when provided.

### `summarise_packet` return

Returns a plain `str` — a single diagnostic sentence. The system prompt instructs the LLM to prioritize signals in this order: matched elements (named explicitly) > named errors or known issues > relevant files > recent commits. The response is a JSON object `{"summary": "..."}` parsed and validated before the string is returned.

### `MetadataProposal`

```python
@dataclass
class MetadataProposal:
    proposed_fields: dict[str, Any]   # field_name → proposed_value
    confidence: float
    evidence: str                     # one-sentence rationale
    raw_response: str
```

### `SimilarityProposal`

```python
@dataclass
class SimilarityProposal:
    known_issue_id: str
    score: float
    method: str          # always "llm" from this gateway
    evidence_anchors: list[str]
    raw_response: str
```

## Error Types

```python
class LLMGatewayError(Exception): pass
class LLMConfigError(LLMGatewayError): pass       # missing/invalid config
class LLMUnavailableError(LLMGatewayError): pass  # all retries exhausted
class LLMResponseError(LLMGatewayError): pass     # response failed validation
```

## Metadata Proposal Verifier

The verifier is a pure function in `modok/ingestion/verifier.py`. It is called by the ingestion pipeline after receiving a `MetadataProposal` from the gateway. The gateway itself does not verify — it has no registry access.

```python
@dataclass
class VerificationResult:
    valid_fields: dict[str, Any]      # fields that passed all checks
    rejected_fields: list[RejectedField]  # fields that failed, with reasons
    is_valid: bool                    # True if rejected_fields is empty

@dataclass
class RejectedField:
    field: str
    bad_value: Any
    reason: str
    repair_instruction: str           # included in CEGIS repair prompt

def verify_proposal(
    proposal: MetadataProposal,
    missing_fields: list[str],
    existing_frontmatter: dict,
    registry: Registry,
) -> VerificationResult
```

### Hard checks (all fields)

- `proposed_fields` must be a dict.
- `confidence` must be a float in [0.0, 1.0].
- `evidence` must be a non-empty string.
- Every key in `proposed_fields` must appear in `missing_fields` — extra fields are rejected individually, not as a whole-proposal failure.
- No key in `proposed_fields` may already exist in `existing_frontmatter` — overwrite attempts are rejected.
- Every proposed value must have the correct type for its field.
- Enum fields must use known enum values.
- Slug fields (`feature_slug`, `module_slug`, error signature slugs) must exist in the registry.
- List fields must be deduplicated.
- Empty values are rejected unless the field explicitly allows them.

### Evidence checks (proposal-level)

The `evidence` field is a single string on the whole `MetadataProposal`, not per-field. If evidence fails, all proposed fields are rejected.

Two-tier check:
- **Hard**: fewer than 15 characters — objectively too short to be useful (catches "N/A", "See above.", blank).
- **Soft pattern**: matches a hardcoded filler list (strings starting with "The document is about", "This document describes", "Based on the document", "The file mentions"). Not configurable — new patterns become code changes with fixtures.

A short but specific string (e.g. `"shtp.c line 42"` at 14 chars) fails the hard check. This is intentional: if the evidence is that specific, it should be written out as a sentence.

### Safety checks (structural)

These are enforced by the pipeline, not the verifier function, but documented here for completeness:
- The gateway must not write to the doc file.
- The gateway must not write to Quine.
- Invalid proposals must not be partially applied without the caller's explicit field-level decision.
- `raw_response` must not be persisted by the gateway.

### Verification result handling

**Default mode (`--fix` without `--strict`)**: accept `valid_fields`, skip `rejected_fields`. Each rejected field becomes a structured warning in the ingestion report. The doc is updated with passing fields only; remaining missing fields re-surface on the next ingest run.

**Strict mode (`--fix --strict`)**: if `rejected_fields` is non-empty, write nothing. Emit a structured error per rejected field.

## Bounded CEGIS Repair Loop

When the verifier rejects one or more fields, the ingestion pipeline may attempt one repair. The gateway is not aware of the repair loop — it receives a second `propose_metadata` call with an augmented prompt that includes the counterexamples.

```
propose_metadata(doc, frontmatter, missing_fields)
  │
  ▼
verify_proposal(...)
  │
  ├── all valid → accept valid_fields
  │
  └── some rejected
         │
         ▼
     build counterexamples from rejected_fields
         │
         ▼
     propose_metadata(doc, frontmatter, remaining_missing_fields,
                      repair_context=counterexamples)   ← second call
         │
         ▼
     verify_proposal(...)
         │
         ├── valid fields → accept (accumulated with initial valid_fields)
         └── still rejected → warn, skip those fields
```

Config:
```toml
[llm]
cegis_fix_enabled = true
cegis_max_iterations_propose_metadata = 1   # one repair attempt; total max 2 LLM calls per field set
```

The repair loop runs only when `cegis_fix_enabled = true`. In non-interactive mode (CLI-INGEST-004), both the initial proposal and the repair attempt are suppressed — no LLM calls are made.

### Counterexample format (repair prompt input)

```yaml
counterexamples:
  - field: proposed_fields.feature_slug
    reason: "Proposed slug is not in the known feature registry."
    bad_value: "docs"
    allowed_values: [ingestion, retrieval, llm-gateway, quine-client]
    repair_instruction: "Choose an allowed feature_slug only if supported by the document body. Otherwise omit the field."

  - field: proposed_fields.owner
    reason: "Field was not requested in missing_fields."
    bad_value: "platform"
    repair_instruction: "Only propose fields listed in missing_fields."
```

### Counterexample file emission (`--emit-counterexamples`)

When `--emit-counterexamples` is passed to `modok ingest --fix`, rejected fields from both the initial proposal and the repair attempt are written as a YAML counterexample file to the path configured in `llm.counterexample_fixture_dir` (pointing to modok's own `tests/fixtures/llm_gateway/`, not the project repo). This feeds the offline CEGIS eval corpus directly. The file is named `{doc_slug}_{iso_timestamp}.yaml` and follows the format above extended with `input`, `expected`, and `actual` sections (see `docs/Offline-cegis-brainstorm.md`). When `counterexample_fixture_dir` is not configured, the command exits `1` with a clear error before making any LLM calls.

## ID Scheme

The LLM Gateway writes no nodes — it has no Quine ID concerns. Callers own node creation.

## Decisions & Alternatives

| Decision | Chosen | Alternatives Considered | Rationale |
|---|---|---|---|
| OpenAI-compatible endpoint | Single interface for all backends | Separate Ollama SDK + Anthropic SDK | One code path; Ollama, Claude, GPT-4, and any future model support it; no provider lock-in |
| Local-first with optional escalation | Ollama default; remote opt-in | Remote-first; always-local; always-remote | Local keeps costs zero for normal use; remote available when local model is insufficient |
| Fixed 1s retry delay | Simple fixed delay | Exponential backoff | LLM calls are already 5–30s; backoff adds negligible benefit and complicates reasoning |
| API key from env fallback | `MODOK_LLM_API_KEY` env var | Config file only; keychain | Env var is the standard CI/server pattern; config file is for local dev; both supported |
| `response_format: json_object` | JSON mode on all calls | Free-form text parsed with regex; function calling / tool use | JSON mode is the most portable structured output across all providers; function calling is provider-specific |
| Prompts in `prompts.py` | Fixed frozen templates | Loaded from YAML/TOML at runtime; user-configurable | Fixed templates are auditable and testable; runtime loading adds attack surface and complexity |
| Gateway never writes | Proposals returned to caller | Gateway writes directly to Quine; gateway writes to doc file | Keeps write path mechanical; gateway is purely read/inference; audit trail stays in caller |
| `auto` escalation trigger | Validation failure only | Confidence only; validation + confidence | Confidence scores from small models are unreliable; validation failure is a concrete, deterministic signal |
| `propose_similarity` caller supplies candidates | Caller pre-fetches `KnownIssueSummary` list and passes to gateway | Gateway queries Quine directly | Gateway stays stateless; retrieval logic belongs in the retrieval engine |
| `LLMResponseError` is hard exception | Always raise; caller handles degradation | Gateway returns empty/partial result | Caller knows its UX context; swallowing errors in the gateway hides failures |
| `raw_response` persistence | In-memory only; returned in result struct | Gateway persists to audit log | Persistence is a future audit-log concern; current callers only need it for debug logging |
| Per-call-type timeouts | 15s interactive (`propose_metadata`, `propose_similarity`), 30s background (`parse_ticket`, `summarise_packet`) | Single global timeout | Interactive paths must feel fast; background calls can be slower |
| `matched_elements` in `summarise_packet` | Passed explicitly by DRE; named in prompt priority list | Derive from files only | Without element names the LLM focuses on the file path; naming the element produces a more actionable summary |
| `summarise_packet` returns plain string | `str` return | `SummaryResult` dataclass | No additional metadata from the summary call is needed by callers; a plain string avoids a wrapper type |
| `repair_context` in `propose_metadata` | Optional parameter; gateway includes counterexamples in repair prompt when present | Separate `repair_metadata` function; always include repair context | Single function keeps the interface simple; `None` repair_context = initial call, non-None = repair call; no behavioral difference from gateway's perspective beyond prompt construction |
| CEGIS repair iteration cap | 1 (total 2 LLM calls max per field set) | Unlimited; 2 or 3 iterations | One repair is sufficient to distinguish "model can self-correct" from "prompt needs improvement"; more iterations delay interactive `--fix` UX with diminishing returns |
| Verifier location | `modok/ingestion/verifier.py`; not in gateway | `modok/llm/verifier.py` | Registry access required for slug/enum validation; gateway is stateless and registry-unaware |

## Open Questions & Future Decisions

### Resolved
1. ✅ Backend protocol — OpenAI-compatible chat completions endpoint for all backends (including Ollama-hosted Gemma).
2. ✅ Backend selection — local-first, remote as opt-in escalation, `auto` mode with configurable threshold.
3. ✅ API key source — config file first, `MODOK_LLM_API_KEY` env var fallback, `LLMConfigError` if remote attempted without key.
4. ✅ Retry strategy — fixed 1s delay, `max_retries` attempts, immediate raise on 4xx.
5. ✅ Structured output — `json_object` response format, pydantic validation with raw-text JSON extraction fallback, `LLMResponseError` on failure.
6. ✅ Write responsibility — gateway returns proposals; callers own all writes.
7. ✅ `auto` escalation trigger — validation failure only.
8. ✅ `propose_similarity` inputs — caller passes pre-fetched `KnownIssueSummary` list; gateway is stateless.
9. ✅ `LLMResponseError` handling — hard exception always; caller decides degradation strategy.
10. ✅ `raw_response` storage — in-memory only; no gateway persistence.
11. ✅ Per-call-type timeouts — 15s for interactive paths, 30s for background `parse_ticket`.

### Deferred
1. **Streaming responses** — currently uses non-streaming completions. Streaming would improve perceived latency for long similarity proposals but adds response assembly complexity. Deferred until a concrete UX need arises.
2. **Prompt versioning** — prompts in `prompts.py` are frozen strings. If prompt tuning becomes a workflow, a versioning scheme (hash in log, stored alongside response) would help audit which prompt produced which result. Not needed at current scale.
3. **Local model selection per call type** — currently one `local_model` for all three call types. Ticket parsing may benefit from a smaller/faster model than similarity proposals. Deferred until model diversity in the local stack justifies it.

## References

- `docs/llds/ingestion-pipeline.md` — primary caller for metadata proposal
- `docs/llds/quine-client.md` — write primitives (gateway does not use directly)
- `docs/high-level-design.md §2` — LLM-agnostic gateway decision
