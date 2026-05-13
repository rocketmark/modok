# LLM Gateway Specs

Specs for `modok.llm` — the abstract LLM boundary that returns proposals for ticket parsing, metadata suggestion, similarity matching, and debug packet summarisation.

LLD: `docs/llds/llm-gateway.md`

---

## Test Level Convention

See `docs/testing-standard.md` for full definitions.

- **[U]** — Unit test with mocked dependencies.
- **[P]** — Property test (`hypothesis`). Implies [U].
- **[C]** — Contract test against live Quine instance. Implies [U].

---

## Backend Selection and Protocol

- [x] **LLM-BACK-001** [U]: When `backend="local"` (or `mode="first"` with a single-entry backends list), the system shall send the request to the first configured backend using its endpoint and model, regardless of whether additional backends are configured.
- [x] **LLM-BACK-002** [U]: When `backend="remote"` and only one backend is configured (no second entry to use as the remote), the system shall raise `LLMConfigError` without making any network call.
- [x] **LLM-BACK-003** [U]: When `backend="auto"` (or `mode="auto"`), the system shall attempt backends in list order. If a backend's response fails validation, the system shall escalate to the next backend. Escalation to any given backend happens at most once per gateway call.
- [x] **LLM-BACK-005** [U]: When `backend="auto"` and only one backend is configured, the system shall behave identically to `backend="local"` and shall not raise an error due to the absence of additional backends.
- [x] **LLM-BACK-006** [U]: The API key shall be read from the backend entry's `api_key` field first; if absent or empty, from the `MODOK_LLM_API_KEY` environment variable. If neither is set and a backend with `protocol="openai"` is called, the system shall raise `LLMConfigError`. Backends with `protocol="ollama"` do not require an API key.

---

## Protocol Discrimination

- [x] **LLM-PROTO-001** [U]: A backend entry with `protocol="ollama"` shall use the Ollama native `/api/chat` endpoint. A backend entry with `protocol="openai"` shall use the OpenAI-compatible `/chat/completions` endpoint. The protocol selection shall be determined solely by the `protocol` field — not by the host or port of the endpoint. A `protocol="openai"` backend on localhost shall use `/chat/completions`; a `protocol="ollama"` backend on a remote host shall use `/api/chat`.

---

## Multi-Backend List

- [x] **LLM-MULTI-001** [U]: When two or more backends are configured and `mode="auto"`, the system shall walk the list in order, escalating to the next backend only when the current backend's response fails validation (`LLMResponseError`). When a backend returns a response that passes validation, the system shall return immediately without calling subsequent backends. When all backends are exhausted and none produced a valid response, the system shall raise `LLMResponseError`.
- [x] **LLM-MULTI-002** [U]: The legacy flat config keys (`local_endpoint`, `local_model`, `remote_endpoint`, `remote_model`, `remote_api_key`) shall be accepted and synthesized into an equivalent ordered backends list at runtime: the `local_*` keys produce a first entry with `protocol="ollama"`; the `remote_*` keys, when present, produce a second entry with `protocol="openai"`. Behaviour shall be identical to an equivalent explicit `[[llm.backends]]` configuration.

---

## Retry and Timeout

- [x] **LLM-RETRY-001** [U]: On a timeout or 5xx response, the system shall retry up to `max_retries` times with a 1-second fixed delay between attempts, on the same backend. Retry does not trigger backend escalation — escalation is triggered only by validation failure (LLM-BACK-003).
- [x] **LLM-RETRY-002** [U]: On a 4xx response, the system shall raise `LLMGatewayError` immediately without retrying.
- [x] **LLM-RETRY-003** [U]: After all retries are exhausted without a valid response, the system shall raise `LLMUnavailableError`.
- [x] **LLM-RETRY-004** [U]: `parse_ticket` and `summarise_packet` calls shall use `timeout_parse_ticket` and `timeout_summarise_packet` respectively (default 30s each). `propose_metadata` and `propose_similarity` calls shall use `timeout_propose_metadata` and `timeout_propose_similarity` respectively (default 15s each). When a per-call-type timeout key is absent from config, the system shall fall back to `timeout_seconds`.
- [x] **LLM-RETRY-005** [P]: The total number of network attempts for any single gateway call shall never exceed `max_retries + 1`.

---

## Structured Output and Validation

- [x] **LLM-VAL-001** [U]: All gateway calls shall set `response_format: {"type": "json_object"}` in the request.
- [x] **LLM-VAL-002** [U]: When the model response is valid JSON matching the expected pydantic schema, the system shall return the typed result without error.
- [x] **LLM-VAL-003** [U]: When the model response is not valid JSON but contains an extractable JSON object in the raw text, the system shall extract and validate it. A successful extraction counts as a valid response — no retry is triggered and no error is raised. A failed extraction does not consume a retry attempt; the LLM call is retried instead.
- [x] **LLM-VAL-004** [U]: When the model response cannot be parsed as JSON after extraction, the system shall raise `LLMResponseError` after all retries are exhausted.
- [x] **LLM-VAL-005** [U]: When the model response is valid JSON but fails pydantic schema validation, the system shall raise `LLMResponseError` after all retries are exhausted.
- [x] **LLM-VAL-006** [U]: `LLMResponseError` shall always propagate to the caller; the gateway shall never swallow it or return a partial result in its place.

---

## Ticket Parsing

- [x] **LLM-TICKET-001** [U]: `parse_ticket` shall send the raw ticket text and project slug to the LLM and return a `TicketParseResult` containing `feature_slugs` (list), `error_signatures`, `environment`, `symptoms`, `confidence`, `raw_response`, and `mentioned_files`.
- [x] **LLM-TICKET-002** [U]: When the model does not report a confidence value, `TicketParseResult.confidence` shall default to `0.0`.
- [x] **LLM-TICKET-003** [U]: `parse_ticket` shall accept optional context parameters (`valid_slugs`, `feature_slugs`, `module_slugs`, `feature_descriptions`, `module_descriptions`, `module_elements`, `module_source_files`) interpolated into the prompt to guide slug selection. When `valid_slugs` is provided, returned `feature_slugs` shall be filtered to only those present in `valid_slugs`.
- [x] **LLM-TICKET-004** [U]: `parse_ticket` shall never write to Quine or to any file; it returns a result struct only.
- [x] **LLM-TICKET-005** [U]: When the model returns a legacy `feature_slug` (singular string) instead of `feature_slugs` (list), the system shall accept both and normalise to a list before returning.

---

## Metadata Proposal

- [x] **LLM-META-001** [U]: `propose_metadata` shall send the doc path, current frontmatter, and list of missing field names to the LLM and return a `MetadataProposal` containing `proposed_fields`, `confidence`, `evidence`, and `raw_response`.
- [x] **LLM-META-002** [U]: `propose_metadata` shall use the frozen system prompt template from `modok.llm.prompts`.
- [x] **LLM-META-003** [U]: `propose_metadata` shall never write to Quine or to any file; the caller (`apply_llm_proposals` in the ingestion pipeline) owns all writes.
- [x] **LLM-META-004** [U]: When `propose_metadata` raises `LLMResponseError` or `LLMUnavailableError`, the ingestion pipeline shall catch the exception, emit a structured warning, and skip writing for that doc — it shall not halt ingestion of other files.

---

## Metadata Proposal — `repair_context`

- [x] **LLM-META-005** [U]: When `propose_metadata` is called with a non-None `repair_context`, the system shall include the counterexample list in the prompt sent to the LLM so it can correct the previously rejected fields.
- [x] **LLM-META-006** [U]: When `propose_metadata` is called with `repair_context=None`, the system shall send the standard metadata proposal prompt with no counterexample content.
- [x] **LLM-META-007** [U]: `propose_metadata` shall use a distinct prompt template for repair calls (with `repair_context`) vs. initial calls (without), both defined as frozen constants in `modok.llm.prompts`.

---

## Metadata Proposal Verifier

- [x] **LLM-VER-001** [U]: `verify_proposal` shall reject any proposed field whose key does not appear in `missing_fields`, recording a `RejectedField` with reason "field was not requested".
- [x] **LLM-VER-002** [U]: `verify_proposal` shall reject any proposed field whose key already exists in `existing_frontmatter`, recording a `RejectedField` with reason "would overwrite existing field".
- [x] **LLM-VER-003** [U]: `verify_proposal` shall reject any proposed field whose value has an incorrect type for that field, recording a `RejectedField` with reason and expected type.
- [x] **LLM-VER-004** [U]: `verify_proposal` shall reject any proposed field whose value is a slug (feature slug, module slug, error signature slug) not present in the registry, recording a `RejectedField` with `allowed_values` populated from the registry.
- [x] **LLM-VER-005** [U]: `verify_proposal` shall reject any proposed field whose value is an enum value not in the known set for that field, recording a `RejectedField` with `allowed_values`.
- [x] **LLM-VER-006** [U]: `verify_proposal` shall reject any proposed list field whose value contains duplicates, recording a `RejectedField` with reason "list contains duplicates".
- [x] **LLM-VER-007** [U]: `verify_proposal` shall reject any proposed field whose value is empty (empty string, empty list, or None) unless the field explicitly permits empty values, recording a `RejectedField` with reason "empty value not permitted".
- [x] **LLM-VER-008** [U]: `verify_proposal` shall reject the entire proposal (all fields move to `rejected_fields`) when the `MetadataProposal.evidence` string is absent, empty, fewer than 15 characters, or matches a known filler pattern (hardcoded list: strings starting with "The document is about", "This document describes", "Based on the document", "The file mentions"). The rejection reason shall be "evidence insufficient" on every rejected field.
- [x] **LLM-VER-009** [P]: `verify_proposal` shall never modify `existing_frontmatter`, `missing_fields`, or `proposal` — it is a pure function with no side effects.
- [x] **LLM-VER-010** [U]: `verify_proposal` shall return a `VerificationResult` with `is_valid=True` and empty `rejected_fields` when all proposed fields pass all checks.
- [x] **LLM-VER-011** [U]: `verify_proposal` shall return a `VerificationResult` with `is_valid=False` and a populated `rejected_fields` list when one or more fields fail any check; fields that pass shall appear in `valid_fields`.

---

## Bounded CEGIS Repair

- [x] **LLM-CEGIS-001** [U]: When `cegis_fix_enabled = true` and `verify_proposal` returns `is_valid=False`, the ingestion pipeline shall call `propose_metadata` exactly once more with `repair_context` populated from the `rejected_fields` of the failed result.
- [x] **LLM-CEGIS-002** [U]: The repair attempt shall only include in `missing_fields` the fields that were rejected in the initial attempt; fields that passed the initial verification shall not be re-proposed. The pipeline shall accumulate `valid_fields` from both passes — fields accepted in the initial pass are retained regardless of whether the repair call omits them.
- [x] **LLM-CEGIS-003** [U]: The ingestion pipeline shall not make more than one repair attempt per doc regardless of the repair result.
- [x] **LLM-CEGIS-004** [U]: When `cegis_fix_enabled = false`, the ingestion pipeline shall not make any repair attempt; initial verification failure is final.
- [x] **LLM-CEGIS-005** [U]: The total number of `propose_metadata` calls for a single doc shall never exceed 2 (one initial + one repair).

---

## Similarity Proposal

- [x] **LLM-SIM-001** [U]: `propose_similarity` shall accept a `CustomerIssue` and a list of `KnownIssueSummary` structs and return a list of `SimilarityProposal` structs; it shall not query Quine.
- [x] **LLM-SIM-002** [U]: Each `SimilarityProposal` shall contain `known_issue_id`, `score`, `method` (always `"llm"`), `evidence_anchors`, and `raw_response`.
- [x] **LLM-SIM-003** [U]: When `candidates` is an empty list, `propose_similarity` shall return an empty list without making any LLM call, regardless of the `backend` parameter value.
- [x] **LLM-SIM-004** [U]: `propose_similarity` shall use the frozen system prompt template from `modok.llm.prompts`.
- [x] **LLM-SIM-005** [U]: `propose_similarity` shall never write to Quine or to any file.

---

## Packet Summary

- [x] **LLM-SUMM-001** [U]: `summarise_packet` shall send `issue_text`, `module_slugs`, `error_signatures`, `symptoms`, `matched_elements`, `relevant_files`, `relevant_tests`, `recent_commits` (up to 5), and `known_issues` to the LLM and return a single plain string containing one diagnostic sentence.
- [x] **LLM-SUMM-002** [U]: The `summarise_packet` system prompt shall instruct the LLM to prioritize signals in this order: matched elements (named explicitly) > named errors or known issues > relevant files > recent commits. When `matched_elements` is non-empty, the summary shall name at least one element explicitly.
- [x] **LLM-SUMM-003** [U]: `summarise_packet` shall use the frozen `SUMMARISE_PACKET_SYSTEM` prompt from `modok.llm.prompts`. The response shall be a JSON object `{"summary": "..."}` validated before the string is extracted and returned.
- [x] **LLM-SUMM-004** [U]: `summarise_packet` shall never write to Quine or to any file; it returns a plain string only.

---

## Gateway Write Boundary

- [x] **LLM-WRITE-001** [U]: No gateway function shall call `upsert_node`, `write_edge`, or any Quine client method.
- [x] **LLM-WRITE-002** [U]: No gateway function shall write to any file on disk.
- [x] **LLM-WRITE-003** [P]: The `raw_response` field in any result struct shall contain the raw model output string and shall not be persisted by the gateway; it is returned in-memory to the caller only.

---

## Prompt Discipline

- [x] **LLM-PROMPT-001** [U]: All prompt templates shall be defined as module-level string constants in `modok.llm.prompts`; no prompt text shall be constructed at runtime outside of field interpolation.
- [x] **LLM-PROMPT-002** [U]: Each of the four call types (`parse_ticket`, `propose_metadata`, `propose_similarity`, `summarise_packet`) shall use a distinct system prompt template.
