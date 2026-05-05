from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx

from modok.llm.errors import (
    LLMConfigError,
    LLMGatewayError,
    LLMResponseError,
    LLMUnavailableError,
)
from modok.llm.models import (
    KnownIssueSummary,
    MetadataProposal,
    SimilarityProposal,
    TicketParseResult,
)
from modok.llm import prompts
from modok.quine.models import CustomerIssue


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    import tomllib
    config_path = Path.home() / ".modok" / "config.toml"
    if not config_path.exists():
        return {}
    with open(config_path, "rb") as f:
        data = tomllib.load(f)
    return data.get("llm", {})


def _get_timeout(cfg: dict, key: str) -> float:
    return float(cfg.get(key) or cfg.get("timeout_seconds", 30))


def _resolve_api_key(cfg: dict) -> str:
    key = cfg.get("remote_api_key", "")
    if not key:
        key = os.environ.get("MODOK_LLM_API_KEY", "")
    return key


def _check_remote_config(cfg: dict) -> tuple[str, str, str]:
    """Return (endpoint, model, api_key) or raise LLMConfigError."""
    endpoint = cfg.get("remote_endpoint", "")
    model = cfg.get("remote_model", "")
    if not endpoint or not model:
        raise LLMConfigError("remote_endpoint and remote_model must be configured for remote backend")
    api_key = _resolve_api_key(cfg)
    if not api_key:
        raise LLMConfigError(
            "No API key found — set remote_api_key in config or MODOK_LLM_API_KEY env var"
        )
    return endpoint, model, api_key


# ---------------------------------------------------------------------------
# JSON extraction fallback
# ---------------------------------------------------------------------------

def _extract_json(raw: str) -> dict | None:
    """Attempt to extract a JSON object from raw text using bracket counting.

    Tries each '{' in the string as a candidate start, advancing past failures.
    """
    search_from = 0
    while True:
        start = raw.find("{", search_from)
        if start == -1:
            return None
        depth = 0
        in_string = False
        escape = False
        end = None
        for i, ch in enumerate(raw[start:], start):
            if escape:
                escape = False
                continue
            if ch == "\\" and in_string:
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end is None:
            search_from = start + 1
            continue
        try:
            return json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            search_from = start + 1


# ---------------------------------------------------------------------------
# Core HTTP calls
# ---------------------------------------------------------------------------

async def _ollama_chat_completion(
    messages: list[dict],
    endpoint: str,
    model: str,
    timeout: float,
    temperature: float | None = None,
    num_ctx: int | None = None,
) -> str:
    """Native Ollama API — disables thinking, requests JSON output."""
    options: dict = {}
    if temperature is not None:
        options["temperature"] = temperature
    if num_ctx is not None:
        options["num_ctx"] = num_ctx
    body: dict = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": False,
        "format": "json",
        "keep_alive": -1,
    }
    if options:
        body["options"] = options
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.post(
                f"{endpoint.rstrip('/')}/api/chat",
                headers={"Content-Type": "application/json"},
                json=body,
            )
        except httpx.TimeoutException as exc:
            raise asyncio.TimeoutError(str(exc)) from exc

    if resp.status_code >= 500:
        raise LLMUnavailableError(f"Server error {resp.status_code}")
    if resp.status_code >= 400:
        raise LLMGatewayError(f"Client error {resp.status_code}: {resp.text}")

    data = resp.json()
    return data["message"]["content"]


async def _chat_completion(
    messages: list[dict],
    response_format: dict,
    endpoint: str,
    model: str,
    timeout: float,
    api_key: str = "",
    temperature: float | None = None,
) -> str:
    """OpenAI-compatible API — used for remote backends."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    body: dict = {
        "model": model,
        "messages": messages,
        "response_format": response_format,
    }
    if temperature is not None:
        body["temperature"] = temperature

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.post(
                f"{endpoint.rstrip('/')}/chat/completions",
                headers=headers,
                json=body,
            )
        except httpx.TimeoutException as exc:
            raise asyncio.TimeoutError(str(exc)) from exc

    if resp.status_code >= 500:
        raise LLMUnavailableError(f"Server error {resp.status_code}")
    if resp.status_code >= 400:
        raise LLMGatewayError(f"Client error {resp.status_code}: {resp.text}")

    data = resp.json()
    return data["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Retry + backend selection
# ---------------------------------------------------------------------------

async def _call_with_retry(
    messages: list[dict],
    response_format: dict,
    endpoint: str,
    model: str,
    timeout: float,
    api_key: str,
    max_retries: int,
    native_ollama: bool = False,
    temperature: float | None = None,
    num_ctx: int | None = None,
) -> str:
    last_exc: Exception = LLMUnavailableError("no attempts made")
    for attempt in range(max_retries + 1):
        try:
            if native_ollama:
                return await _ollama_chat_completion(
                    messages=messages,
                    endpoint=endpoint,
                    model=model,
                    timeout=timeout,
                    temperature=temperature,
                    num_ctx=num_ctx,
                )
            return await _chat_completion(
                messages=messages,
                response_format=response_format,
                endpoint=endpoint,
                model=model,
                timeout=timeout,
                api_key=api_key,
                temperature=temperature,
            )
        except (asyncio.TimeoutError, LLMUnavailableError) as exc:
            last_exc = exc
            if attempt < max_retries:
                await asyncio.sleep(1)
        except LLMGatewayError:
            raise  # 4xx — no retry
    raise LLMUnavailableError(f"All {max_retries + 1} attempts failed") from last_exc


def _parse_and_validate(raw: str, validator) -> Any:
    """Parse raw string as JSON, try extraction fallback, run validator. Raises LLMResponseError."""
    data = None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = _extract_json(raw)

    if data is None:
        raise LLMResponseError(f"No JSON found in response: {raw[:200]!r}")

    try:
        return validator(data, raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise LLMResponseError(f"Response failed schema validation: {exc}") from exc


async def _call_auto(
    messages: list[dict],
    response_format: dict,
    timeout: float,
    cfg: dict,
    validator,
) -> Any:
    """Run local first; escalate to remote on validation failure only."""
    local_endpoint = cfg.get("local_endpoint", "http://localhost:11434")
    local_model = cfg.get("local_model", "llama3.2")
    max_retries = int(cfg.get("max_retries", 2))

    has_remote = bool(cfg.get("remote_endpoint") and cfg.get("remote_model"))

    raw = await _call_with_retry(
        messages=messages,
        response_format=response_format,
        endpoint=local_endpoint,
        model=local_model,
        timeout=timeout,
        api_key="",
        max_retries=max_retries,
        native_ollama=True,
    )

    if has_remote:
        try:
            return _parse_and_validate(raw, validator)
        except LLMResponseError:
            pass
        # Escalate to remote — at most once, only on validation failure
        r_endpoint, r_model, r_key = _check_remote_config(cfg)
        raw = await _call_with_retry(
            messages=messages,
            response_format=response_format,
            endpoint=r_endpoint,
            model=r_model,
            timeout=timeout,
            api_key=r_key,
            max_retries=max_retries,
            native_ollama=False,
        )

    return _parse_and_validate(raw, validator)


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

def _validate_ticket(data: dict, raw: str) -> TicketParseResult:
    expected = {"feature_slugs", "feature_slug", "error_signatures", "environment", "symptoms", "confidence"}
    if not expected.intersection(data.keys()):
        raise ValueError(f"response contains none of the expected ticket fields: {list(data.keys())}")
    # Accept both new list form and legacy single-slug form
    raw_slugs = data.get("feature_slugs") or []
    if not isinstance(raw_slugs, list):
        raw_slugs = [raw_slugs] if raw_slugs else []
    legacy = data.get("feature_slug")
    if legacy and legacy not in raw_slugs:
        raw_slugs.append(legacy)
    return TicketParseResult(
        feature_slugs=raw_slugs,
        error_signatures=list(data.get("error_signatures", [])),
        environment=dict(data.get("environment", {})),
        symptoms=list(data.get("symptoms", [])),
        confidence=float(data.get("confidence", 0.0)),
        raw_response=raw,
        mentioned_files=list(data.get("mentioned_files", [])),
    )


def _validate_metadata(data: dict, raw: str) -> MetadataProposal:
    if "proposed_fields" not in data:
        raise ValueError("missing proposed_fields")
    return MetadataProposal(
        proposed_fields=dict(data["proposed_fields"]),
        confidence=float(data.get("confidence", 0.0)),
        evidence=str(data.get("evidence", "")),
        raw_response=raw,
    )


def _validate_similarity(data: dict, raw: str) -> list[SimilarityProposal]:
    proposals = data.get("proposals", [])
    if not isinstance(proposals, list):
        raise ValueError("proposals must be a list")
    return [
        SimilarityProposal(
            known_issue_id=str(p["known_issue_id"]),
            score=float(p.get("score", 0.0)),
            method="llm",
            evidence_anchors=list(p.get("evidence_anchors", [])),
            raw_response=json.dumps(p),
        )
        for p in proposals
    ]


def _validate_summary(data: dict, raw: str) -> str:
    summary = data.get("summary")
    if not summary or not isinstance(summary, str):
        raise ValueError("missing or invalid summary field")
    return summary


# ---------------------------------------------------------------------------
# Synchronous enrich calls (registry proposal — sequential, no asyncio)
# ---------------------------------------------------------------------------

def _ollama_enrich_call(
    messages: list[dict],
    endpoint: str,
    model: str,
    timeout: float,
    num_ctx: int = 8192,
    temperature: float | None = None,
) -> dict:
    """Sync Ollama native /api/chat call for section enrichment. Returns parsed JSON dict."""
    options: dict = {"num_ctx": num_ctx}
    if temperature is not None:
        options["temperature"] = temperature
    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": False,
        "format": "json",
        "options": options,
    }
    try:
        resp = httpx.post(
            f"{endpoint.rstrip('/')}/api/chat",
            headers={"Content-Type": "application/json"},
            json=body,
            timeout=timeout,
        )
    except httpx.TimeoutException as exc:
        raise LLMUnavailableError(f"Timeout after {timeout}s") from exc
    except httpx.RequestError as exc:
        raise LLMUnavailableError(f"Request failed: {exc}") from exc

    if resp.status_code >= 500:
        raise LLMUnavailableError(f"Server error {resp.status_code}")
    if resp.status_code >= 400:
        raise LLMGatewayError(f"Client error {resp.status_code}: {resp.text}")

    content = resp.json()["message"]["content"]
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        parsed = _extract_json(content)
        if parsed is None:
            raise LLMResponseError(f"No JSON in response: {content[:200]!r}")
        return parsed


def _openai_enrich_call(
    messages: list[dict],
    endpoint: str,
    model: str,
    timeout: float,
    api_key: str = "",
) -> dict:
    """Sync OpenAI-compatible call for section enrichment. Returns parsed JSON dict."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    body = {
        "model": model,
        "messages": messages,
        "response_format": {"type": "json_object"},
    }
    try:
        resp = httpx.post(
            f"{endpoint.rstrip('/')}/chat/completions",
            headers=headers,
            json=body,
            timeout=timeout,
        )
    except httpx.TimeoutException as exc:
        raise LLMUnavailableError(f"Timeout after {timeout}s") from exc
    except httpx.RequestError as exc:
        raise LLMUnavailableError(f"Request failed: {exc}") from exc

    if resp.status_code >= 500:
        raise LLMUnavailableError(f"Server error {resp.status_code}")
    if resp.status_code >= 400:
        raise LLMGatewayError(f"Client error {resp.status_code}: {resp.text}")

    content = resp.json()["choices"][0]["message"]["content"]
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        parsed = _extract_json(content)
        if parsed is None:
            raise LLMResponseError(f"No JSON in response: {content[:200]!r}")
        return parsed


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def enrich_section(section: Any, cfg_llm: Any) -> Any:
    # @spec RP-ENRICH-001, RP-ENRICH-005, RP-ENRICH-006, RP-ENRICH-007, RP-ENRICH-008
    from modok.registry.proposal import EnrichSectionResult

    timeout = getattr(cfg_llm, "timeout_propose_registry", None)
    if timeout is None:
        timeout = getattr(cfg_llm, "timeout_seconds", 60)
    timeout = float(timeout)

    backend = getattr(cfg_llm, "backend", "local")

    messages = [
        {"role": "system", "content": prompts.ENRICH_SECTION_SYSTEM},
        {"role": "user", "content": f"Section heading: {section.heading}\n\n{section.body}"},
    ]

    if backend == "remote":
        endpoint = getattr(cfg_llm, "remote_endpoint", "") or getattr(cfg_llm, "base_url", "")
        model = getattr(cfg_llm, "remote_model", "") or getattr(cfg_llm, "model", "")
        api_key = getattr(cfg_llm, "remote_api_key", "")
        data = _openai_enrich_call(
            messages=messages,
            endpoint=endpoint,
            model=model,
            timeout=timeout,
            api_key=api_key,
        )
    else:
        endpoint = getattr(cfg_llm, "local_endpoint", "http://localhost:11434")
        model = getattr(cfg_llm, "local_model", "llama3.2")
        data = _ollama_enrich_call(
            messages=messages,
            endpoint=endpoint,
            model=model,
            timeout=timeout,
        )

    return EnrichSectionResult(
        features=list(data.get("features", [])),
        modules=list(data.get("modules", [])),
        error_signatures=list(data.get("error_signatures", [])),
        known_issues=list(data.get("known_issues", [])),
        failure_modes=list(data.get("failure_modes", [])),
        decisions=list(data.get("decisions", [])),
        observation_events=list(data.get("observation_events", [])),
    )


def normalise_candidates(
    candidates: list,
    field_type: str,
    cfg_llm: Any,
    counterexamples: list | None = None,
) -> list:
    # @spec RN-NORM-001, RN-NORM-002
    import yaml as _yaml

    timeout = getattr(cfg_llm, "timeout_propose_registry", None)
    if timeout is None:
        timeout = getattr(cfg_llm, "timeout_seconds", 60)
    timeout = float(timeout)

    backend = getattr(cfg_llm, "backend", "local")

    # Send only names to cut output tokens — no descriptions needed for normalisation
    name_key = "normalized_error" if field_type == "errors" else "name"
    name_list = [c.get(name_key, "") for c in candidates if c.get(name_key)]

    user_parts = [
        f"field: {field_type}",
        "",
        _yaml.dump({"candidates": name_list}, default_flow_style=False, allow_unicode=True),
    ]
    if counterexamples:
        ce_names = [
            v.get(name_key, "")
            for v in counterexamples
            if not v.get("_empty_output") and v.get(name_key, "")
        ]
        if ce_names:
            user_parts += [
                "COUNTEREXAMPLES (these violate constraints — do not re-introduce them):",
                _yaml.dump(ce_names, default_flow_style=False, allow_unicode=True),
            ]
    user_content = "\n".join(user_parts)

    messages = [
        {"role": "system", "content": prompts.NORMALISE_REGISTRY_SYSTEM},
        {"role": "user", "content": user_content},
    ]

    if backend == "remote":
        endpoint = getattr(cfg_llm, "remote_endpoint", "") or getattr(cfg_llm, "base_url", "")
        model = getattr(cfg_llm, "remote_model", "") or getattr(cfg_llm, "model", "")
        api_key = getattr(cfg_llm, "remote_api_key", "")
        raw = _openai_enrich_call(
            messages=messages,
            endpoint=endpoint,
            model=model,
            timeout=timeout,
            api_key=api_key,
        )
    else:
        endpoint = getattr(cfg_llm, "local_endpoint", "http://localhost:11434")
        model = getattr(cfg_llm, "local_model", "llama3.2")
        raw = _ollama_enrich_call(
            messages=messages,
            endpoint=endpoint,
            model=model,
            timeout=timeout,
            num_ctx=131072,
            temperature=0,
        )

    # Extract list from response
    if isinstance(raw, list):
        raw_list = raw
    elif isinstance(raw, dict):
        raw_list = raw.get("entries", raw.get(field_type, raw.get("candidates", [])))
    else:
        raw_list = []

    # Convert strings to dicts for downstream verification
    result = []
    for item in raw_list:
        if isinstance(item, str):
            if field_type == "errors":
                result.append({"normalized_error": item, "description": ""})
            else:
                result.append({"name": item, "description": ""})
        elif isinstance(item, dict):
            result.append(item)
    return result


async def parse_ticket(
    raw_text: str,
    project_slug: str,
    backend: str = "local",
    valid_slugs: list[str] | None = None,
    feature_slugs: list[str] | None = None,
    module_slugs: list[str] | None = None,
    feature_descriptions: dict[str, str] | None = None,
    module_descriptions: dict[str, str] | None = None,
    module_elements: dict[str, list[str]] | None = None,
    module_source_files: dict[str, list[str]] | None = None,
) -> TicketParseResult:
    _MAX_TICKET_CHARS = 1500

    cfg = _load_config()
    timeout = _get_timeout(cfg, "timeout_parse_ticket")
    max_retries = int(cfg.get("max_retries", 2))
    num_ctx = int(cfg.get("num_ctx", 8192))

    # Truncate long tickets — anchor information is in the summary/header.
    # File listings in ticket bodies are already handled by _pre_match_modules.
    if len(raw_text) > _MAX_TICKET_CHARS:
        raw_text = raw_text[:_MAX_TICKET_CHARS] + "\n[...truncated]"

    def _fmt_feature(slug: str) -> str:
        desc = (feature_descriptions or {}).get(slug, "")
        return f"  - {slug}: {desc}" if desc else f"  - {slug}"

    def _fmt_module(slug: str) -> str:
        desc = (module_descriptions or {}).get(slug, "")
        files = (module_source_files or {}).get(slug, [])
        elems = (module_elements or {}).get(slug, [])
        files_str = f"; files: {', '.join(files[:10])}" if files else ""
        elems_str = f"; code: {', '.join(elems)}" if elems else ""
        return f"  - {slug}: {desc}{files_str}{elems_str}" if (desc or files_str or elems_str) else f"  - {slug}"

    feature_slug_list = "\n".join(_fmt_feature(s) for s in (feature_slugs or [])) or "  (none registered)"
    module_slug_list = "\n".join(_fmt_module(s) for s in (module_slugs or [])) or "  (none registered)"
    messages = [
        {"role": "system", "content": prompts.PARSE_TICKET_SYSTEM.format(
            project_slug=project_slug,
            feature_slug_list=feature_slug_list,
            module_slug_list=module_slug_list,
        )},
        {"role": "user", "content": f"<ticket>\n{raw_text}\n</ticket>"},
    ]
    response_format = {"type": "json_object"}

    if backend == "auto":
        return await _call_auto(messages, response_format, timeout, cfg, _validate_ticket)

    if backend == "remote":
        endpoint, model, api_key = _check_remote_config(cfg)
        raw = await _call_with_retry(
            messages=messages,
            response_format=response_format,
            endpoint=endpoint,
            model=model,
            timeout=timeout,
            api_key=api_key,
            max_retries=max_retries,
            native_ollama=False,
            temperature=0,
        )
    else:
        endpoint = cfg.get("local_endpoint", "http://localhost:11434")
        model = cfg.get("local_model", "llama3.2")
        raw = await _call_with_retry(
            messages=messages,
            response_format=response_format,
            endpoint=endpoint,
            model=model,
            timeout=timeout,
            api_key="",
            max_retries=max_retries,
            native_ollama=True,
            temperature=0,
            num_ctx=num_ctx,
        )
    result = _parse_and_validate(raw, _validate_ticket)
    if valid_slugs:
        result.feature_slugs = [s for s in result.feature_slugs if s in valid_slugs]
    return result


async def propose_metadata(
    doc_path: Path,
    frontmatter: dict,
    missing_fields: list[str],
    repair_context: list[dict] | None = None,
    backend: str = "local",
) -> MetadataProposal:
    # @spec LLM-META-005, LLM-META-006, LLM-META-007
    cfg = _load_config()
    timeout = _get_timeout(cfg, "timeout_propose_metadata")
    max_retries = int(cfg.get("max_retries", 2))
    user_content = (
        f"Doc: {doc_path}\n"
        f"Current frontmatter: {json.dumps(frontmatter)}\n"
        f"Missing fields: {', '.join(missing_fields)}"
    )
    if repair_context:
        import yaml as _yaml
        user_content += f"\n\nCounterexamples from previous attempt:\n{_yaml.dump(repair_context, default_flow_style=False)}"
    system = prompts.PROPOSE_METADATA_REPAIR_SYSTEM if repair_context else prompts.PROPOSE_METADATA_SYSTEM
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]
    response_format = {"type": "json_object"}

    if backend == "auto":
        return await _call_auto(messages, response_format, timeout, cfg, _validate_metadata)

    if backend == "remote":
        endpoint, model, api_key = _check_remote_config(cfg)
        raw = await _call_with_retry(
            messages=messages,
            response_format=response_format,
            endpoint=endpoint,
            model=model,
            timeout=timeout,
            api_key=api_key,
            max_retries=max_retries,
            native_ollama=False,
        )
    else:
        endpoint = cfg.get("local_endpoint", "http://localhost:11434")
        model = cfg.get("local_model", "llama3.2")
        raw = await _call_with_retry(
            messages=messages,
            response_format=response_format,
            endpoint=endpoint,
            model=model,
            timeout=timeout,
            api_key="",
            max_retries=max_retries,
            native_ollama=True,
        )
    return _parse_and_validate(raw, _validate_metadata)


async def propose_similarity(
    issue: CustomerIssue,
    candidates: list[KnownIssueSummary],
    backend: str = "local",
) -> list[SimilarityProposal]:
    if not candidates:
        return []

    cfg = _load_config()
    timeout = _get_timeout(cfg, "timeout_propose_similarity")
    max_retries = int(cfg.get("max_retries", 2))

    candidates_text = "\n".join(
        f"- id: {c.known_issue_id}\n  summary: {c.summary}\n  errors: {c.error_signatures}"
        for c in candidates
    )
    user_content = (
        f"Customer issue: {issue.summary}\n\n"
        f"Known issues:\n{candidates_text}"
    )
    messages = [
        {"role": "system", "content": prompts.PROPOSE_SIMILARITY_SYSTEM},
        {"role": "user", "content": user_content},
    ]
    response_format = {"type": "json_object"}

    if backend == "auto":
        return await _call_auto(messages, response_format, timeout, cfg, _validate_similarity)

    if backend == "remote":
        endpoint, model, api_key = _check_remote_config(cfg)
        raw = await _call_with_retry(
            messages=messages,
            response_format=response_format,
            endpoint=endpoint,
            model=model,
            timeout=timeout,
            api_key=api_key,
            max_retries=max_retries,
            native_ollama=False,
        )
    else:
        endpoint = cfg.get("local_endpoint", "http://localhost:11434")
        model = cfg.get("local_model", "llama3.2")
        raw = await _call_with_retry(
            messages=messages,
            response_format=response_format,
            endpoint=endpoint,
            model=model,
            timeout=timeout,
            api_key="",
            max_retries=max_retries,
            native_ollama=True,
        )
    return _parse_and_validate(raw, _validate_similarity)


# @spec LLM-SUMM-001, LLM-SUMM-002, LLM-SUMM-003, LLM-SUMM-004
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
) -> str:
    cfg = _load_config()
    timeout = _get_timeout(cfg, "timeout_summarise_packet")
    max_retries = int(cfg.get("max_retries", 2))
    num_ctx = int(cfg.get("num_ctx", 8192))

    modules_line = ", ".join(module_slugs) if module_slugs else "unknown"
    files_block = "\n".join(f"  {f}" for f in relevant_files) if relevant_files else "  (none)"
    tests_block = "\n".join(f"  {f}" for f in relevant_tests) if relevant_tests else "  (none)"
    commits_block = "\n".join(
        f"  {c.get('timestamp', '')[:10]}  {c.get('author_name', '')}:  {c.get('message', '')}"
        for c in recent_commits[:5]
    ) if recent_commits else "  (none)"
    errors_line = "; ".join(error_signatures) if error_signatures else "(none)"
    symptoms_line = "; ".join(symptoms) if symptoms else "(none)"
    issues_line = "; ".join(known_issues) if known_issues else "none"
    elements_line = ", ".join(matched_elements) if matched_elements else "(none)"

    user_content = (
        f"<issue>\n{issue_text}\n</issue>\n\n"
        f"Module: {modules_line}\n"
        f"Errors: {errors_line}\n"
        f"Symptoms: {symptoms_line}\n"
        f"Matched elements: {elements_line}\n\n"
        f"Files:\n{files_block}\n\n"
        f"Tests:\n{tests_block}\n\n"
        f"Recent commits:\n{commits_block}\n\n"
        f"Known issues: {issues_line}"
    )
    messages = [
        {"role": "system", "content": prompts.SUMMARISE_PACKET_SYSTEM},
        {"role": "user", "content": user_content},
    ]
    response_format = {"type": "json_object"}

    if backend == "auto":
        return await _call_auto(messages, response_format, timeout, cfg, _validate_summary)

    if backend == "remote":
        endpoint, model, api_key = _check_remote_config(cfg)
        raw = await _call_with_retry(
            messages=messages,
            response_format=response_format,
            endpoint=endpoint,
            model=model,
            timeout=timeout,
            api_key=api_key,
            max_retries=max_retries,
            native_ollama=False,
        )
    else:
        endpoint = cfg.get("local_endpoint", "http://localhost:11434")
        model = cfg.get("local_model", "llama3.2")
        raw = await _call_with_retry(
            messages=messages,
            response_format=response_format,
            endpoint=endpoint,
            model=model,
            timeout=timeout,
            api_key="",
            max_retries=max_retries,
            native_ollama=True,
            num_ctx=num_ctx,
        )
    return _parse_and_validate(raw, _validate_summary)
