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

The gateway supports two wire protocols:

- **`ollama`** — Ollama's native `/api/chat` endpoint. Sends Ollama-specific fields (`think`, `format`, `keep_alive`, `options.num_ctx`). Use for a locally-running Ollama instance.
- **`openai`** — OpenAI-compatible `/chat/completions` endpoint with `response_format: {type: "json_object"}`. Use for oMLX, LM Studio, vllm, actual OpenAI/Anthropic, or any other OpenAI-compatible server — local or remote.

The protocol is independent of where the server lives. An oMLX server on localhost uses `protocol = "openai"`; an Ollama server on a remote host uses `protocol = "ollama"`.

### Backend list and dispatch mode

Backends are configured as an ordered list. The gateway walks the list according to `mode`:

- **`auto`** (default) — tries backends in order. If a backend returns a response that passes validation, returns immediately. If validation fails (`LLMResponseError`), escalates to the next backend. Escalation to any given backend happens at most once per call.
- **`first`** — uses only the first backend in the list; never escalates regardless of the result.

The `backend` parameter on each public function is a legacy shim: `"local"` maps to `first` mode using backends[0]; `"remote"` maps to `first` mode using backends[-1]; `"auto"` maps to `auto` mode over the full list.

### Configuration (from `~/.modok/config.toml`)

```toml
# Backends tried in order. protocol: "ollama" or "openai".
[[llm.backends]]
name     = "local-mlx"
protocol = "openai"
endpoint = "http://localhost:10240"
model    = "your-model-name"
api_key  = ""                      # optional; read from MODOK_LLM_API_KEY env var if absent

[[llm.backends]]
name     = "cloud-fallback"
protocol = "openai"
endpoint = "https://api.anthropic.com/v1"
model    = "claude-haiku-4-5-20251001"
api_key  = ""                      # or set MODOK_LLM_API_KEY

[llm]
mode     = "auto"                  # "auto" | "first"
timeout_seconds = 30               # default; overridden per call type below
timeout_parse_ticket    = 30       # background-safe; can be slow
timeout_propose_metadata = 15      # interactive (--fix workflow); must feel fast
timeout_propose_similarity = 15    # interactive; surfaced to user
timeout_summarise_packet   = 30    # background; called after retrieval
max_retries     = 2
cegis_fix_enabled = true
cegis_max_iterations_propose_metadata = 1   # one repair attempt; total max 2 LLM calls
counterexample_fixture_dir = ""             # required when using --emit-counterexamples; points to modok's own tests/fixtures/llm_gateway/
```

**Legacy flat keys** (`local_endpoint`, `local_model`, `remote_endpoint`, `remote_model`, `remote_api_key`) are still accepted and synthesized into a two-entry backends list at runtime. New configs should use `[[llm.backends]]` instead.

Per-call-type timeouts take precedence over `timeout_seconds`. If a per-call-type key is absent, `timeout_seconds` is used.

API key is read from the backend entry's `api_key` field first, then from the environment variable `MODOK_LLM_API_KEY`. If neither is set and a backend with `protocol = "openai"` is called, the gateway raises `LLMConfigError`. Ollama backends do not require an API key.

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
| Two wire protocols | `ollama` (native `/api/chat`) and `openai` (`/chat/completions`) | Single OpenAI-compatible path for all backends | Ollama's native API supports `think`, `format`, `keep_alive`, `num_ctx`; forcing OpenAI compat on Ollama loses those knobs. Keeping both lets each server speak its own dialect. |
| Protocol is independent of host location | `protocol = "openai"` works for localhost oMLX or remote Claude | `local` = Ollama, `remote` = OpenAI | "Local vs remote" is a network topology fact, not a wire-format fact. An oMLX server is local but speaks OpenAI; naming the protocol directly removes the ambiguity. |
| Ordered backends list | `[[llm.backends]]` TOML array | Single primary + single fallback flat keys | A list is the natural generalisation: one, two, or ten backends all fit the same config shape. The old two-slot model was a one-way door. |
| `auto` mode walks list; escalates only on validation failure | Walk in order; stop on first valid response | Escalate on low confidence; always try all | Confidence scores from small models are unreliable; validation failure is a concrete, deterministic signal. Stopping on success avoids unnecessary cloud calls. |
| Legacy flat keys preserved as compat shim | `_resolve_backends()` synthesizes from flat keys when `backends` list is absent | Hard break; require migration | Existing configs keep working without change. The shim is a one-way read — it never writes flat keys back. |
| Fixed 1s retry delay | Simple fixed delay | Exponential backoff | LLM calls are already 5–30s; backoff adds negligible benefit and complicates reasoning |
| API key per backend entry | `api_key` in each `[[llm.backends]]` entry; env var fallback | Single global `remote_api_key` | Each backend has its own credentials. A global key only makes sense when there is exactly one remote — which is now just a special case of one entry. |
| `response_format: json_object` | JSON mode on openai-protocol calls | Free-form text parsed with regex; function calling / tool use | JSON mode is the most portable structured output across all OpenAI-compatible providers; function calling is provider-specific |
| Prompts in `prompts.py` | Fixed frozen templates | Loaded from YAML/TOML at runtime; user-configurable | Fixed templates are auditable and testable; runtime loading adds attack surface and complexity |
| Gateway never writes | Proposals returned to caller | Gateway writes directly to Quine; gateway writes to doc file | Keeps write path mechanical; gateway is purely read/inference; audit trail stays in caller |
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
1. ✅ Backend protocol — two protocols: `ollama` (native `/api/chat`) and `openai` (OpenAI-compatible `/chat/completions`). Protocol is independent of host location.
2. ✅ Backend selection — ordered `[[llm.backends]]` list; `auto` mode walks list escalating on validation failure; `first` mode uses only backends[0].
3. ✅ API key source — per-backend `api_key` field first, `MODOK_LLM_API_KEY` env var fallback, `LLMConfigError` if an openai-protocol backend is called without a key.
4. ✅ Retry strategy — fixed 1s delay, `max_retries` attempts, immediate raise on 4xx.
5. ✅ Structured output — `json_object` response format on openai-protocol calls; `format: json` on ollama-protocol calls. Pydantic validation with raw-text JSON extraction fallback; `LLMResponseError` on failure.
6. ✅ Write responsibility — gateway returns proposals; callers own all writes.
7. ✅ `auto` escalation trigger — validation failure only; confidence score not used.
8. ✅ `propose_similarity` inputs — caller passes pre-fetched `KnownIssueSummary` list; gateway is stateless.
9. ✅ `LLMResponseError` handling — hard exception always; caller decides degradation strategy.
10. ✅ `raw_response` storage — in-memory only; no gateway persistence.
11. ✅ Per-call-type timeouts — 15s for interactive paths, 30s for background `parse_ticket`.
12. ✅ Per-call-type model selection — the backends list naturally supports different models per position; callers can supply a custom list if needed.
13. ✅ Legacy flat-key compat — `local_endpoint`/`local_model`/`remote_endpoint`/`remote_model`/`remote_api_key` synthesized into a two-entry backends list at load time; no migration required.

### Deferred
1. **Streaming responses** — currently uses non-streaming completions. Streaming would improve perceived latency for long similarity proposals but adds response assembly complexity. Deferred until a concrete UX need arises.
2. **Prompt versioning** — prompts in `prompts.py` are frozen strings. If prompt tuning becomes a workflow, a versioning scheme (hash in log, stored alongside response) would help audit which prompt produced which result. Not needed at current scale.

## References

- `docs/llds/ingestion-pipeline.md` — primary caller for metadata proposal
- `docs/llds/quine-client.md` — write primitives (gateway does not use directly)
- `docs/high-level-design.md §2` — LLM-agnostic gateway decision
