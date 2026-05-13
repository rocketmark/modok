"""
Tests for modok.llm — the LLM gateway.
All tests written before implementation (Phase 5). Every test cites
the EARS spec it verifies via @spec annotation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(
    local_endpoint="http://localhost:11434/v1",
    local_model="llama3.2",
    remote_endpoint=None,
    remote_model=None,
    remote_api_key=None,
    auto_escalation_threshold=0.60,
    timeout_seconds=30,
    timeout_parse_ticket=30,
    timeout_propose_metadata=15,
    timeout_propose_similarity=15,
    max_retries=2,
):
    """Legacy flat-key config dict — exercises backwards compat shim."""
    cfg = {
        "local_endpoint": local_endpoint,
        "local_model": local_model,
        "auto_escalation_threshold": auto_escalation_threshold,
        "timeout_seconds": timeout_seconds,
        "timeout_parse_ticket": timeout_parse_ticket,
        "timeout_propose_metadata": timeout_propose_metadata,
        "timeout_propose_similarity": timeout_propose_similarity,
        "max_retries": max_retries,
    }
    if remote_endpoint:
        cfg["remote_endpoint"] = remote_endpoint
    if remote_model:
        cfg["remote_model"] = remote_model
    if remote_api_key is not None:
        cfg["remote_api_key"] = remote_api_key
    return cfg


def make_backends_config(
    backends: list[dict],
    timeout_seconds: int = 30,
    timeout_parse_ticket: int = 30,
    timeout_propose_metadata: int = 15,
    timeout_propose_similarity: int = 15,
    max_retries: int = 2,
) -> dict:
    """New-style backends list config dict."""
    return {
        "backends": backends,
        "timeout_seconds": timeout_seconds,
        "timeout_parse_ticket": timeout_parse_ticket,
        "timeout_propose_metadata": timeout_propose_metadata,
        "timeout_propose_similarity": timeout_propose_similarity,
        "max_retries": max_retries,
    }


def _ollama_backend(endpoint: str = "http://localhost:11434", model: str = "llama3.2") -> dict:
    return {"name": "local", "protocol": "ollama", "endpoint": endpoint, "model": model, "api_key": ""}


def _openai_backend(endpoint: str, model: str, api_key: str = "") -> dict:
    return {"name": "remote", "protocol": "openai", "endpoint": endpoint, "model": model, "api_key": api_key}


VALID_TICKET_RESPONSE = """\
{
  "feature_slugs": ["shtp-receiver"],
  "error_signatures": ["shtp-version-mismatch"],
  "environment": {"platform": "windows"},
  "symptoms": ["pose dropout"],
  "confidence": 0.85
}
"""

VALID_METADATA_RESPONSE = """\
{
  "proposed_fields": {"modules": ["shtp"], "source_files": ["agent/src/shtp.c"]},
  "confidence": 0.78,
  "evidence": "Doc body references shtp.c extensively"
}
"""

VALID_SIMILARITY_RESPONSE = """\
{
  "proposals": [
    {
      "known_issue_id": "ki-001",
      "score": 0.92,
      "evidence_anchors": ["shtp-version-mismatch", "pose dropout"]
    }
  ]
}
"""


# ---------------------------------------------------------------------------
# LLM-BACK-001 — local backend uses local_endpoint and local_model
# ---------------------------------------------------------------------------


# @spec LLM-BACK-001
@pytest.mark.asyncio
async def test_local_backend_uses_local_endpoint():
    from modok.llm.gateway import parse_ticket

    with patch("modok.llm.gateway._load_config", return_value=make_config()):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = VALID_TICKET_RESPONSE
            await parse_ticket("ticket text", "stagehand", backend="local")

    call_kwargs = mock_chat.call_args.kwargs
    assert call_kwargs.get("endpoint", "").startswith("http://localhost:11434")
    assert "llama" in call_kwargs.get("model", "")


# @spec LLM-BACK-001
@pytest.mark.asyncio
async def test_local_backend_ignores_remote_when_configured():
    from modok.llm.gateway import parse_ticket

    cfg = make_config(
        remote_endpoint="https://api.anthropic.com/v1",
        remote_model="claude-sonnet-4-6",
        remote_api_key="sk-test",
    )
    with patch("modok.llm.gateway._load_config", return_value=cfg):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = VALID_TICKET_RESPONSE
            await parse_ticket("ticket text", "stagehand", backend="local")

    assert mock_chat.call_count == 1
    call_kwargs = mock_chat.call_args.kwargs
    assert "anthropic" not in call_kwargs.get("endpoint", "")


# ---------------------------------------------------------------------------
# LLM-BACK-002 — remote backend raises LLMConfigError when not configured
# ---------------------------------------------------------------------------


# @spec LLM-BACK-002
@pytest.mark.asyncio
async def test_remote_backend_raises_config_error_when_not_configured():
    from modok.llm.gateway import parse_ticket

    with patch("modok.llm.gateway._load_config", return_value=make_config()):
        with pytest.raises(LLMConfigError):
            await parse_ticket("ticket text", "stagehand", backend="remote")


# @spec LLM-BACK-002
@pytest.mark.asyncio
async def test_remote_backend_makes_no_network_call_before_config_check():
    from modok.llm.gateway import parse_ticket

    with patch("modok.llm.gateway._load_config", return_value=make_config()):
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_chat:
            with pytest.raises(LLMConfigError):
                await parse_ticket("ticket text", "stagehand", backend="remote")
        mock_chat.assert_not_called()


# ---------------------------------------------------------------------------
# LLM-BACK-003 — auto: escalates on validation failure
# ---------------------------------------------------------------------------


# @spec LLM-BACK-003
@pytest.mark.asyncio
async def test_auto_escalates_to_remote_on_local_validation_failure():
    from modok.llm.gateway import parse_ticket

    cfg = make_config(
        remote_endpoint="https://api.anthropic.com/v1",
        remote_model="claude-sonnet-4-6",
        remote_api_key="sk-test",
    )
    bad_response = '{"not_a_valid_ticket": true}'

    with patch("modok.llm.gateway._load_config", return_value=cfg):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_local:
            mock_local.return_value = bad_response  # local returns bad JSON
            with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_remote:
                mock_remote.return_value = VALID_TICKET_RESPONSE  # remote returns good JSON
                result = await parse_ticket("ticket text", "stagehand", backend="auto")

    assert mock_local.call_count == 1
    assert mock_remote.call_count == 1
    assert isinstance(result, TicketParseResult)


# @spec LLM-BACK-003
@given(max_retries=st.integers(min_value=0, max_value=4))
@settings(deadline=None)
def test_auto_escalates_at_most_once(max_retries):
    """Regardless of max_retries, auto mode escalates to remote at most once."""
    import asyncio as _asyncio
    from modok.llm.gateway import parse_ticket

    cfg = make_config(
        max_retries=max_retries,
        remote_endpoint="https://api.anthropic.com/v1",
        remote_model="claude-sonnet-4-6",
        remote_api_key="sk-test",
    )
    bad_response = '{"not_a_valid_ticket": true}'
    remote_calls = []

    async def run():
        async def fake_chat_remote(endpoint="", **kwargs):
            if "anthropic" in endpoint:
                remote_calls.append(1)
            return bad_response

        async def fake_chat_local(messages=None, endpoint="", model="", timeout=30, **kwargs):
            return bad_response

        with patch("modok.llm.gateway._load_config", return_value=cfg):
            with patch("modok.llm.gateway._ollama_chat_completion", new=fake_chat_local):
                with patch("modok.llm.gateway._chat_completion", new=fake_chat_remote):
                    with patch("modok.llm.gateway.asyncio.sleep", new_callable=AsyncMock):
                        try:
                            await parse_ticket("text", "stagehand", backend="auto")
                        except (LLMResponseError, LLMUnavailableError):
                            pass

    _asyncio.run(run())
    assert len(remote_calls) <= 1


# ---------------------------------------------------------------------------
# LLM-BACK-004 — auto: escalates on low confidence
# ---------------------------------------------------------------------------


# @spec LLM-BACK-004
@pytest.mark.asyncio
async def test_auto_does_not_escalate_on_low_confidence():
    """Low confidence alone does not trigger escalation — only validation failure does."""
    from modok.llm.gateway import parse_ticket

    cfg = make_config(
        remote_endpoint="https://api.anthropic.com/v1",
        remote_model="claude-sonnet-4-6",
        remote_api_key="sk-test",
    )
    low_conf_response = """\
{
  "feature_slug": null,
  "error_signatures": [],
  "environment": {},
  "symptoms": [],
  "confidence": 0.10
}
"""

    with patch("modok.llm.gateway._load_config", return_value=cfg):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = low_conf_response
            result = await parse_ticket("ticket text", "stagehand", backend="auto")

    # Local response validated fine — no escalation even though confidence is low
    assert mock_chat.call_count == 1
    assert result.confidence == 0.10


# ---------------------------------------------------------------------------
# LLM-BACK-005 — auto without remote configured behaves as local
# ---------------------------------------------------------------------------


# @spec LLM-BACK-005
@pytest.mark.asyncio
async def test_auto_without_remote_behaves_as_local():
    from modok.llm.gateway import parse_ticket

    with patch("modok.llm.gateway._load_config", return_value=make_config()):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = VALID_TICKET_RESPONSE
            result = await parse_ticket("ticket text", "stagehand", backend="auto")

    assert mock_chat.call_count == 1
    assert isinstance(result, TicketParseResult)


# @spec LLM-BACK-005
@pytest.mark.asyncio
async def test_auto_without_remote_does_not_raise_on_low_confidence():
    from modok.llm.gateway import parse_ticket

    low_conf = """\
{
  "feature_slug": null,
  "error_signatures": [],
  "environment": {},
  "symptoms": [],
  "confidence": 0.10
}
"""
    with patch("modok.llm.gateway._load_config", return_value=make_config()):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = low_conf
            # should not raise — no remote to escalate to
            result = await parse_ticket("ticket text", "stagehand", backend="auto")

    assert isinstance(result, TicketParseResult)
    assert result.confidence == 0.10


# ---------------------------------------------------------------------------
# LLM-BACK-006 — API key: config first, env var fallback, error if neither
# ---------------------------------------------------------------------------


# @spec LLM-BACK-006
@pytest.mark.asyncio
async def test_api_key_read_from_config():
    from modok.llm.gateway import parse_ticket

    cfg = make_config(
        remote_endpoint="https://api.anthropic.com/v1",
        remote_model="claude-sonnet-4-6",
        remote_api_key="sk-from-config",
    )
    with patch("modok.llm.gateway._load_config", return_value=cfg):
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = VALID_TICKET_RESPONSE
            await parse_ticket("text", "stagehand", backend="remote")

    call_kwargs = mock_chat.call_args.kwargs
    assert call_kwargs.get("api_key") == "sk-from-config"


# @spec LLM-BACK-006
@pytest.mark.asyncio
async def test_api_key_falls_back_to_env_var(monkeypatch):
    from modok.llm.gateway import parse_ticket

    monkeypatch.setenv("MODOK_LLM_API_KEY", "sk-from-env")
    cfg = make_config(
        remote_endpoint="https://api.anthropic.com/v1",
        remote_model="claude-sonnet-4-6",
        remote_api_key="",
    )
    with patch("modok.llm.gateway._load_config", return_value=cfg):
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = VALID_TICKET_RESPONSE
            await parse_ticket("text", "stagehand", backend="remote")

    call_kwargs = mock_chat.call_args.kwargs
    assert call_kwargs.get("api_key") == "sk-from-env"


# @spec LLM-BACK-006
@pytest.mark.asyncio
async def test_api_key_raises_config_error_when_missing(monkeypatch):
    from modok.llm.gateway import parse_ticket

    monkeypatch.delenv("MODOK_LLM_API_KEY", raising=False)
    cfg = make_config(
        remote_endpoint="https://api.anthropic.com/v1",
        remote_model="claude-sonnet-4-6",
        remote_api_key="",
    )
    with patch("modok.llm.gateway._load_config", return_value=cfg):
        with pytest.raises(LLMConfigError):
            await parse_ticket("text", "stagehand", backend="remote")


# ---------------------------------------------------------------------------
# LLM-RETRY-001 — retry on timeout/5xx, same backend only
# ---------------------------------------------------------------------------


# @spec LLM-RETRY-001
@pytest.mark.asyncio
async def test_retries_on_timeout_up_to_max():
    from modok.llm.gateway import parse_ticket

    cfg = make_config(max_retries=2)

    with patch("modok.llm.gateway._load_config", return_value=cfg):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.side_effect = [
                asyncio.TimeoutError(),
                asyncio.TimeoutError(),
                VALID_TICKET_RESPONSE,
            ]
            with patch("modok.llm.gateway.asyncio.sleep", new_callable=AsyncMock):
                result = await parse_ticket("text", "stagehand", backend="local")

    assert mock_chat.call_count == 3
    assert isinstance(result, TicketParseResult)


# @spec LLM-RETRY-001
@pytest.mark.asyncio
async def test_retry_stays_on_same_backend():
    from modok.llm.gateway import parse_ticket

    cfg = make_config(
        max_retries=1,
        remote_endpoint="https://api.anthropic.com/v1",
        remote_model="claude-sonnet-4-6",
        remote_api_key="sk-test",
    )
    with patch("modok.llm.gateway._load_config", return_value=cfg):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.side_effect = [asyncio.TimeoutError(), VALID_TICKET_RESPONSE]
            with patch("modok.llm.gateway.asyncio.sleep", new_callable=AsyncMock):
                await parse_ticket("text", "stagehand", backend="local")

    # both calls should go to local endpoint
    for call in mock_chat.call_args_list:
        assert "localhost" in call.kwargs.get("endpoint", "")


# ---------------------------------------------------------------------------
# LLM-RETRY-002 — 4xx raises immediately, no retry
# ---------------------------------------------------------------------------


# @spec LLM-RETRY-002
@pytest.mark.asyncio
async def test_4xx_raises_immediately_without_retry():
    from modok.llm.gateway import parse_ticket

    with patch("modok.llm.gateway._load_config", return_value=make_config(max_retries=2)):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.side_effect = LLMGatewayError("401 Unauthorized")
            with pytest.raises(LLMGatewayError):
                await parse_ticket("text", "stagehand", backend="local")

    assert mock_chat.call_count == 1


# ---------------------------------------------------------------------------
# LLM-RETRY-003 — LLMUnavailableError after retries exhausted
# ---------------------------------------------------------------------------


# @spec LLM-RETRY-003
@pytest.mark.asyncio
async def test_raises_unavailable_after_all_retries():
    from modok.llm.gateway import parse_ticket

    cfg = make_config(max_retries=2)
    with patch("modok.llm.gateway._load_config", return_value=cfg):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.side_effect = asyncio.TimeoutError()
            with patch("modok.llm.gateway.asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(LLMUnavailableError):
                    await parse_ticket("text", "stagehand", backend="local")

    assert mock_chat.call_count == 3  # 1 initial + 2 retries


# ---------------------------------------------------------------------------
# LLM-RETRY-004 — per-call-type timeouts
# ---------------------------------------------------------------------------


# @spec LLM-RETRY-004
@given(
    specific_timeout=st.integers(min_value=1, max_value=120),
    default_timeout=st.integers(min_value=1, max_value=120),
)
@settings(deadline=None)
def test_parse_ticket_uses_per_call_type_timeout(specific_timeout, default_timeout):
    """parse_ticket always uses timeout_parse_ticket when present, else timeout_seconds."""
    import asyncio as _asyncio
    from modok.llm.gateway import parse_ticket

    cfg = make_config(timeout_seconds=default_timeout, timeout_parse_ticket=specific_timeout)
    captured = {}

    async def run():
        async def fake_chat(timeout=None, **kwargs):
            captured["timeout"] = timeout
            return VALID_TICKET_RESPONSE

        with patch("modok.llm.gateway._load_config", return_value=cfg):
            with patch("modok.llm.gateway._ollama_chat_completion", new=fake_chat):
                await parse_ticket("text", "stagehand", backend="local")

    _asyncio.run(run())
    assert captured.get("timeout") == specific_timeout


# @spec LLM-RETRY-004
@given(default_timeout=st.integers(min_value=1, max_value=120))
@settings(deadline=None)
def test_timeout_falls_back_to_timeout_seconds_when_specific_key_absent(default_timeout):
    """When per-call-type key is absent, timeout_seconds is used."""
    import asyncio as _asyncio
    from modok.llm.gateway import parse_ticket

    cfg = make_config(timeout_seconds=default_timeout)
    cfg.pop("timeout_parse_ticket", None)
    captured = {}

    async def run():
        async def fake_chat(timeout=None, **kwargs):
            captured["timeout"] = timeout
            return VALID_TICKET_RESPONSE

        with patch("modok.llm.gateway._load_config", return_value=cfg):
            with patch("modok.llm.gateway._ollama_chat_completion", new=fake_chat):
                await parse_ticket("text", "stagehand", backend="local")

    _asyncio.run(run())
    assert captured.get("timeout") == default_timeout


# @spec LLM-RETRY-004 — spot-check propose_metadata uses its own timeout key
@pytest.mark.asyncio
async def test_propose_metadata_uses_metadata_timeout():
    from modok.llm.gateway import propose_metadata

    cfg = make_config(timeout_seconds=30, timeout_propose_metadata=15)
    with patch("modok.llm.gateway._load_config", return_value=cfg):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = VALID_METADATA_RESPONSE
            await propose_metadata(Path("doc.md"), {}, ["modules"], backend="local")

    assert mock_chat.call_args.kwargs.get("timeout") == 15


# ---------------------------------------------------------------------------
# LLM-RETRY-005 — total attempts never exceeds max_retries + 1
# ---------------------------------------------------------------------------


# @spec LLM-RETRY-005
@given(max_retries=st.integers(min_value=0, max_value=5))
@settings(deadline=None)
def test_total_attempts_never_exceeds_max_retries_plus_one(max_retries):
    import asyncio as _asyncio
    from modok.llm.gateway import parse_ticket

    cfg = make_config(max_retries=max_retries)
    call_count = 0

    async def run():
        nonlocal call_count

        async def fake_chat(**kwargs):
            nonlocal call_count
            call_count += 1
            raise _asyncio.TimeoutError()

        with patch("modok.llm.gateway._load_config", return_value=cfg):
            with patch("modok.llm.gateway._ollama_chat_completion", new=fake_chat):
                with patch("modok.llm.gateway.asyncio.sleep", new_callable=AsyncMock):
                    try:
                        await parse_ticket("text", "stagehand", backend="local")
                    except LLMUnavailableError:
                        pass

    _asyncio.run(run())
    assert call_count <= max_retries + 1


# ---------------------------------------------------------------------------
# LLM-VAL-001 — json_object response_format on all calls
# ---------------------------------------------------------------------------


# @spec LLM-VAL-001
@pytest.mark.asyncio
async def test_chat_completion_sets_json_object_format():
    """Remote (OpenAI-compatible) backend sends response_format=json_object; local uses format:json internally."""
    from modok.llm.gateway import parse_ticket

    cfg = make_config(
        remote_endpoint="https://api.anthropic.com/v1",
        remote_model="claude-sonnet-4-6",
        remote_api_key="sk-test",
    )
    with patch("modok.llm.gateway._load_config", return_value=cfg):
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = VALID_TICKET_RESPONSE
            await parse_ticket("text", "stagehand", backend="remote")

    call_kwargs = mock_chat.call_args.kwargs
    assert call_kwargs.get("response_format") == {"type": "json_object"}


# ---------------------------------------------------------------------------
# LLM-VAL-002 — valid JSON returns typed result
# ---------------------------------------------------------------------------


# @spec LLM-VAL-002
@pytest.mark.asyncio
async def test_valid_json_returns_typed_ticket_parse_result():
    from modok.llm.gateway import parse_ticket

    with patch("modok.llm.gateway._load_config", return_value=make_config()):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = VALID_TICKET_RESPONSE
            result = await parse_ticket("text", "stagehand", backend="local")

    assert isinstance(result, TicketParseResult)
    assert "shtp-receiver" in result.feature_slugs
    assert result.confidence == 0.85


# ---------------------------------------------------------------------------
# LLM-VAL-003 — JSON extraction fallback from raw text
# ---------------------------------------------------------------------------


# @spec LLM-VAL-003
@given(
    prefix=st.text(max_size=40, alphabet=st.characters(whitelist_categories=("L", "Z", "P"))),
    suffix=st.text(max_size=40, alphabet=st.characters(whitelist_categories=("L", "Z", "P"))),
    key=st.text(min_size=1, max_size=10, alphabet=st.characters(whitelist_categories=("L",))),
    value=st.text(min_size=0, max_size=20, alphabet=st.characters(whitelist_categories=("L",))),
)
def test_extract_json_finds_object_in_arbitrary_surrounding_text(prefix, suffix, key, value):
    from modok.llm.gateway import _extract_json
    import json

    assume(_extract_json(prefix) is None)  # skip prefixes that are themselves valid JSON
    obj = {key: value}
    raw = prefix + "\n" + json.dumps(obj) + "\n" + suffix
    result = _extract_json(raw)
    assert result is not None
    assert result[key] == value


# @spec LLM-VAL-003
def test_extract_json_returns_none_when_no_object_found():
    from modok.llm.gateway import _extract_json

    result = _extract_json("This is just plain text with no JSON.")
    assert result is None


# @spec LLM-VAL-003
@pytest.mark.asyncio
async def test_successful_extraction_does_not_trigger_retry():
    from modok.llm.gateway import parse_ticket

    # Model returns text with embedded JSON — not strict json_object
    wrapped = "Sure! Here you go:\n" + VALID_TICKET_RESPONSE + "\nHope that helps."
    with patch("modok.llm.gateway._load_config", return_value=make_config()):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = wrapped
            result = await parse_ticket("text", "stagehand", backend="local")

    assert mock_chat.call_count == 1
    assert isinstance(result, TicketParseResult)


# ---------------------------------------------------------------------------
# LLM-VAL-004 / LLM-VAL-005 — raises LLMResponseError on unrecoverable bad response
# ---------------------------------------------------------------------------


# @spec LLM-VAL-004
@pytest.mark.asyncio
async def test_raises_response_error_when_no_json_extractable():
    from modok.llm.gateway import parse_ticket

    with patch("modok.llm.gateway._load_config", return_value=make_config(max_retries=0)):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = "Sorry, I cannot help with that."
            with pytest.raises(LLMResponseError):
                await parse_ticket("text", "stagehand", backend="local")


# @spec LLM-VAL-005
@pytest.mark.asyncio
async def test_raises_response_error_when_json_fails_schema_validation():
    from modok.llm.gateway import parse_ticket

    bad_schema = '{"wrong_field": "value"}'
    with patch("modok.llm.gateway._load_config", return_value=make_config(max_retries=0)):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = bad_schema
            with pytest.raises(LLMResponseError):
                await parse_ticket("text", "stagehand", backend="local")


# ---------------------------------------------------------------------------
# LLM-TICKET-001 — parse_ticket returns TicketParseResult
# ---------------------------------------------------------------------------


# @spec LLM-TICKET-001
@pytest.mark.asyncio
async def test_parse_ticket_returns_all_required_fields():
    from modok.llm.gateway import parse_ticket

    with patch("modok.llm.gateway._load_config", return_value=make_config()):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = VALID_TICKET_RESPONSE
            result = await parse_ticket("customer reported pose dropout", "stagehand")

    assert "shtp-receiver" in result.feature_slugs
    assert "shtp-version-mismatch" in result.error_signatures
    assert result.environment == {"platform": "windows"}
    assert result.raw_response != ""


# ---------------------------------------------------------------------------
# LLM-TICKET-002 — missing confidence defaults to 0.0
# ---------------------------------------------------------------------------


# @spec LLM-TICKET-002
@pytest.mark.asyncio
async def test_missing_confidence_defaults_to_zero():
    from modok.llm.gateway import parse_ticket

    no_conf = """\
{
  "feature_slug": null,
  "error_signatures": [],
  "environment": {},
  "symptoms": []
}
"""
    with patch("modok.llm.gateway._load_config", return_value=make_config()):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = no_conf
            result = await parse_ticket("text", "stagehand")

    assert result.confidence == 0.0


# ---------------------------------------------------------------------------
# LLM-TICKET-003 — uses frozen prompt template
# ---------------------------------------------------------------------------


# @spec LLM-TICKET-003
@pytest.mark.asyncio
async def test_parse_ticket_uses_prompt_from_prompts_module():
    from modok.llm import prompts
    from modok.llm.gateway import parse_ticket

    with patch("modok.llm.gateway._load_config", return_value=make_config()):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = VALID_TICKET_RESPONSE
            await parse_ticket("text", "stagehand")

    messages = mock_chat.call_args.kwargs.get("messages", [])
    system_content = next((m["content"] for m in messages if m.get("role") == "system"), "")
    # Must derive from the frozen template, not an arbitrary string
    assert any(word in system_content for word in prompts.PARSE_TICKET_SYSTEM.split()[:5])


# ---------------------------------------------------------------------------
# LLM-TICKET-004 — parse_ticket never writes
# ---------------------------------------------------------------------------


# @spec LLM-TICKET-004
def test_parse_ticket_does_not_write_to_quine_or_disk():
    import inspect
    import modok.llm.gateway as gw_mod

    src = inspect.getsource(gw_mod)
    ticket_src = src.split("async def parse_ticket")[1].split("async def ")[0]
    assert "write_text" not in ticket_src
    assert "upsert_node" not in ticket_src
    assert "write_edge" not in ticket_src


# ---------------------------------------------------------------------------
# LLM-META-001 — propose_metadata returns MetadataProposal
# ---------------------------------------------------------------------------


# @spec LLM-META-001
@pytest.mark.asyncio
async def test_propose_metadata_returns_all_required_fields():
    from modok.llm.gateway import propose_metadata

    with patch("modok.llm.gateway._load_config", return_value=make_config()):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = VALID_METADATA_RESPONSE
            result = await propose_metadata(
                Path("docs/lld/shtp.md"),
                {"doc_type": "lld", "feature": "shtp-receiver"},
                ["modules", "source_files"],
            )

    assert isinstance(result, MetadataProposal)
    assert "modules" in result.proposed_fields
    assert result.confidence == 0.78
    assert result.evidence != ""
    assert result.raw_response != ""


# ---------------------------------------------------------------------------
# LLM-META-002 — uses frozen prompt template
# ---------------------------------------------------------------------------


# @spec LLM-META-002
@pytest.mark.asyncio
async def test_propose_metadata_uses_prompt_from_prompts_module():
    from modok.llm import prompts
    from modok.llm.gateway import propose_metadata

    with patch("modok.llm.gateway._load_config", return_value=make_config()):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = VALID_METADATA_RESPONSE
            await propose_metadata(Path("doc.md"), {}, ["modules"])

    messages = mock_chat.call_args.kwargs.get("messages", [])
    system_content = next((m["content"] for m in messages if m.get("role") == "system"), "")
    assert any(word in system_content for word in prompts.PROPOSE_METADATA_SYSTEM.split()[:5])


# ---------------------------------------------------------------------------
# LLM-META-004 — ingestion pipeline catches LLMResponseError and warns
# ---------------------------------------------------------------------------


# @spec LLM-META-004
def test_ingestion_pipeline_catches_llm_response_error_and_warns(tmp_path):
    from modok.ingestion.pipeline import apply_llm_proposals

    # apply_llm_proposals must not re-raise LLMResponseError
    client = MagicMock()
    with patch("modok.ingestion.pipeline.user_approves", return_value=False):
        # Should not raise
        path = tmp_path / "doc.md"
        path.write_text("---\nmodok:\n  doc_type: lld\n---\n# Doc\n")
        try:
            apply_llm_proposals(path, proposals={}, client=client)
        except LLMResponseError:
            pytest.fail("ingestion pipeline must not propagate LLMResponseError")


# ---------------------------------------------------------------------------
# LLM-SIM-001 — propose_similarity accepts candidates list, no Quine calls
# ---------------------------------------------------------------------------


# @spec LLM-SIM-001
@pytest.mark.asyncio
async def test_propose_similarity_returns_proposals():
    from modok.llm.gateway import propose_similarity
    from modok.quine.models import CustomerIssue

    issue = CustomerIssue(
        node_type="CustomerIssue",
        project_slug="stagehand",
        source_system="github",
        ticket_id="gh-200",
        summary="pose dropout on windows",
        status="open",
    )
    candidates = [
        KnownIssueSummary(
            "ki-001", "SHTP version mismatch causes dropout", ["shtp-version-mismatch"]
        ),
    ]

    with patch("modok.llm.gateway._load_config", return_value=make_config()):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = VALID_SIMILARITY_RESPONSE
            result = await propose_similarity(issue, candidates)

    assert isinstance(result, list)
    assert len(result) == 1
    assert isinstance(result[0], SimilarityProposal)
    assert result[0].method == "llm"


# ---------------------------------------------------------------------------
# LLM-SIM-002 — SimilarityProposal carries all required fields
# ---------------------------------------------------------------------------


# @spec LLM-SIM-002
@pytest.mark.asyncio
async def test_similarity_proposal_has_all_required_fields():
    from modok.llm.gateway import propose_similarity
    from modok.quine.models import CustomerIssue

    issue = CustomerIssue(
        node_type="CustomerIssue",
        project_slug="stagehand",
        source_system="github",
        ticket_id="gh-200",
        summary="dropout",
        status="open",
    )
    candidates = [KnownIssueSummary("ki-001", "summary", [])]

    with patch("modok.llm.gateway._load_config", return_value=make_config()):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = VALID_SIMILARITY_RESPONSE
            result = await propose_similarity(issue, candidates)

    p = result[0]
    assert p.known_issue_id == "ki-001"
    assert isinstance(p.score, float)
    assert p.method == "llm"
    assert isinstance(p.evidence_anchors, list)
    assert isinstance(p.raw_response, str)


# ---------------------------------------------------------------------------
# LLM-SIM-003 — empty candidates → empty result, no LLM call
# ---------------------------------------------------------------------------


# @spec LLM-SIM-003
@pytest.mark.asyncio
async def test_empty_candidates_returns_empty_without_llm_call():
    from modok.llm.gateway import propose_similarity
    from modok.quine.models import CustomerIssue

    issue = CustomerIssue(
        node_type="CustomerIssue",
        project_slug="stagehand",
        source_system="github",
        ticket_id="gh-200",
        summary="dropout",
        status="open",
    )

    with patch("modok.llm.gateway._load_config", return_value=make_config()):
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_chat:
            result = await propose_similarity(issue, candidates=[], backend="remote")

    assert result == []
    mock_chat.assert_not_called()


# ---------------------------------------------------------------------------
# LLM-SIM-004 — uses frozen prompt template
# ---------------------------------------------------------------------------


# @spec LLM-SIM-004
@pytest.mark.asyncio
async def test_propose_similarity_uses_prompt_from_prompts_module():
    from modok.llm import prompts
    from modok.llm.gateway import propose_similarity
    from modok.quine.models import CustomerIssue

    issue = CustomerIssue(
        node_type="CustomerIssue",
        project_slug="stagehand",
        source_system="github",
        ticket_id="gh-200",
        summary="dropout",
        status="open",
    )
    candidates = [KnownIssueSummary("ki-001", "summary", [])]

    with patch("modok.llm.gateway._load_config", return_value=make_config()):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = VALID_SIMILARITY_RESPONSE
            await propose_similarity(issue, candidates)

    messages = mock_chat.call_args.kwargs.get("messages", [])
    system_content = next((m["content"] for m in messages if m.get("role") == "system"), "")
    assert any(word in system_content for word in prompts.PROPOSE_SIMILARITY_SYSTEM.split()[:5])


# ---------------------------------------------------------------------------
# LLM-SIM-005 — propose_similarity never writes
# ---------------------------------------------------------------------------


# @spec LLM-SIM-005
def test_propose_similarity_not_in_write_path():
    import inspect
    import modok.llm.gateway as gw_mod

    src = inspect.getsource(gw_mod)
    propose_src = src.split("async def propose_similarity")[1].split("async def ")[0]
    assert "upsert_node" not in propose_src
    assert "write_edge" not in propose_src


# ---------------------------------------------------------------------------
# LLM-WRITE-001/002/003 — gateway write boundary
# ---------------------------------------------------------------------------


# @spec LLM-WRITE-001
def test_gateway_module_does_not_import_quine_client():
    import inspect
    import modok.llm.gateway as gw_mod

    src = inspect.getsource(gw_mod)
    assert "QuineClient" not in src
    assert "from modok.quine.client" not in src


# @spec LLM-WRITE-002
def test_gateway_module_does_not_write_files():
    import inspect
    import modok.llm.gateway as gw_mod

    src = inspect.getsource(gw_mod)
    # None of the public gateway functions should call open/write_text
    for fn in ("parse_ticket", "propose_metadata", "propose_similarity"):
        fn_src = src.split(f"async def {fn}")[1].split("async def ")[0]
        assert "write_text" not in fn_src
        assert "open(" not in fn_src


# @spec LLM-WRITE-003
@pytest.mark.asyncio
async def test_raw_response_is_in_memory_not_persisted():
    """raw_response is returned in the result struct and not written to disk."""
    from modok.llm.gateway import parse_ticket

    with patch("modok.llm.gateway._load_config", return_value=make_config()):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = VALID_TICKET_RESPONSE
            result = await parse_ticket("text", "stagehand")

    # The raw string comes back in the struct
    assert result.raw_response == VALID_TICKET_RESPONSE

    # The gateway source must not persist raw_response anywhere
    import inspect
    import modok.llm.gateway as gw_mod

    src = inspect.getsource(gw_mod)
    assert "raw_response" not in src.split("def _load_config")[0].replace(
        "raw_response=", ""
    ).replace("raw_response:", "")


# ---------------------------------------------------------------------------
# LLM-PROMPT-001/002 — prompt discipline
# ---------------------------------------------------------------------------


# @spec LLM-PROMPT-001
def test_all_prompts_are_module_level_constants():
    import modok.llm.prompts as prompts_mod
    import inspect

    src = inspect.getsource(prompts_mod)
    assert "PARSE_TICKET_SYSTEM" in src
    assert "PROPOSE_METADATA_SYSTEM" in src
    assert "PROPOSE_SIMILARITY_SYSTEM" in src
    # All must be module-level strings, not functions
    assert callable(getattr(prompts_mod, "PARSE_TICKET_SYSTEM", None)) is False


# @spec LLM-PROMPT-002
def test_each_call_type_uses_distinct_prompt():
    import modok.llm.prompts as prompts_mod

    assert prompts_mod.PARSE_TICKET_SYSTEM != prompts_mod.PROPOSE_METADATA_SYSTEM
    assert prompts_mod.PARSE_TICKET_SYSTEM != prompts_mod.PROPOSE_SIMILARITY_SYSTEM
    assert prompts_mod.PROPOSE_METADATA_SYSTEM != prompts_mod.PROPOSE_SIMILARITY_SYSTEM


# ---------------------------------------------------------------------------
# LLM-META-005/006/007 — repair_context in propose_metadata
# ---------------------------------------------------------------------------


# @spec LLM-META-005
@pytest.mark.asyncio
async def test_propose_metadata_includes_counterexamples_in_repair_prompt():
    from modok.llm.gateway import propose_metadata

    repair_context = [{"field": "feature_slug", "reason": "not in registry", "bad_value": "docs"}]
    with patch("modok.llm.gateway._load_config", return_value=make_config()):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = VALID_METADATA_RESPONSE
            await propose_metadata(
                Path("doc.md"),
                {},
                ["feature_slug"],
                repair_context=repair_context,
            )

    messages = mock_chat.call_args.kwargs.get("messages", [])
    user_content = " ".join(m["content"] for m in messages if m.get("role") == "user")
    assert "not in registry" in user_content or "feature_slug" in user_content


# @spec LLM-META-006
@pytest.mark.asyncio
async def test_propose_metadata_no_counterexample_content_without_repair_context():
    from modok.llm.gateway import propose_metadata

    with patch("modok.llm.gateway._load_config", return_value=make_config()):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = VALID_METADATA_RESPONSE
            await propose_metadata(Path("doc.md"), {}, ["modules"], repair_context=None)

    messages = mock_chat.call_args.kwargs.get("messages", [])
    full_content = " ".join(m.get("content", "") for m in messages)
    assert "counterexample" not in full_content.lower()
    assert "bad_value" not in full_content


# @spec LLM-META-007
def test_repair_and_initial_prompts_are_distinct_constants():
    import modok.llm.prompts as prompts_mod

    assert hasattr(prompts_mod, "PROPOSE_METADATA_SYSTEM")
    assert hasattr(prompts_mod, "PROPOSE_METADATA_REPAIR_SYSTEM")
    assert prompts_mod.PROPOSE_METADATA_SYSTEM != prompts_mod.PROPOSE_METADATA_REPAIR_SYSTEM


# ---------------------------------------------------------------------------
# LLM-VER-001 — reject fields not in missing_fields
# ---------------------------------------------------------------------------


# @spec LLM-VER-001
def test_verifier_rejects_field_not_in_missing_fields():
    from modok.ingestion.verifier import verify_proposal
    from modok.llm.models import MetadataProposal

    proposal = MetadataProposal(
        proposed_fields={"feature_slug": "ingestion", "owner": "platform"},
        confidence=0.8,
        evidence="The document describes the ingestion pipeline in detail.",
        raw_response="{}",
    )
    registry = MagicMock()
    registry.has_feature.return_value = True

    result = verify_proposal(proposal, ["feature_slug"], {}, registry)

    rejected_names = [r.field for r in result.rejected_fields]
    assert "owner" in rejected_names
    assert "feature_slug" in result.valid_fields


# ---------------------------------------------------------------------------
# LLM-VER-002 — reject fields that overwrite existing frontmatter
# ---------------------------------------------------------------------------


# @spec LLM-VER-002
def test_verifier_rejects_field_that_overwrites_existing_frontmatter():
    from modok.ingestion.verifier import verify_proposal
    from modok.llm.models import MetadataProposal

    proposal = MetadataProposal(
        proposed_fields={"feature_slug": "ingestion"},
        confidence=0.8,
        evidence="The document describes the ingestion pipeline in detail.",
        raw_response="{}",
    )
    existing = {"feature_slug": "retrieval"}
    registry = MagicMock()

    result = verify_proposal(proposal, ["feature_slug"], existing, registry)

    rejected_names = [r.field for r in result.rejected_fields]
    assert "feature_slug" in rejected_names
    assert any("overwrite" in r.reason.lower() for r in result.rejected_fields)


# ---------------------------------------------------------------------------
# LLM-VER-003 — reject wrong type
# ---------------------------------------------------------------------------


# @spec LLM-VER-003
def test_verifier_rejects_wrong_value_type():
    from modok.ingestion.verifier import verify_proposal
    from modok.llm.models import MetadataProposal

    # modules must be a list, not a string
    proposal = MetadataProposal(
        proposed_fields={"modules": "shtp"},
        confidence=0.8,
        evidence="The document describes the ingestion pipeline in detail.",
        raw_response="{}",
    )
    registry = MagicMock()

    result = verify_proposal(proposal, ["modules"], {}, registry)

    rejected_names = [r.field for r in result.rejected_fields]
    assert "modules" in rejected_names


# ---------------------------------------------------------------------------
# LLM-VER-004 — reject unknown slug
# ---------------------------------------------------------------------------


# @spec LLM-VER-004
def test_verifier_rejects_feature_slug_not_in_registry():
    from modok.ingestion.verifier import verify_proposal
    from modok.llm.models import MetadataProposal

    proposal = MetadataProposal(
        proposed_fields={"feature_slug": "nonexistent-feature"},
        confidence=0.8,
        evidence="The document describes the ingestion pipeline in detail.",
        raw_response="{}",
    )
    registry = MagicMock()
    registry.has_feature.return_value = False
    registry.feature_slugs.return_value = ["ingestion", "retrieval"]

    result = verify_proposal(proposal, ["feature_slug"], {}, registry)

    rejected_names = [r.field for r in result.rejected_fields]
    assert "feature_slug" in rejected_names
    assert any(r.allowed_values for r in result.rejected_fields if r.field == "feature_slug")


# ---------------------------------------------------------------------------
# LLM-VER-005 — reject unknown enum value
# ---------------------------------------------------------------------------


# @spec LLM-VER-005
def test_verifier_rejects_unknown_enum_value():
    from modok.ingestion.verifier import verify_proposal
    from modok.llm.models import MetadataProposal

    # doc_type must be one of the known enum values
    proposal = MetadataProposal(
        proposed_fields={"doc_type": "superdoc"},
        confidence=0.8,
        evidence="The document describes the ingestion pipeline in detail.",
        raw_response="{}",
    )
    registry = MagicMock()

    result = verify_proposal(proposal, ["doc_type"], {}, registry)

    rejected_names = [r.field for r in result.rejected_fields]
    assert "doc_type" in rejected_names
    rejected = next(r for r in result.rejected_fields if r.field == "doc_type")
    assert rejected.allowed_values


# ---------------------------------------------------------------------------
# LLM-VER-006 — reject duplicates in list fields
# ---------------------------------------------------------------------------


# @spec LLM-VER-006
@given(
    items=st.lists(st.text(min_size=1, max_size=15), min_size=2, max_size=10).filter(
        lambda items_: len(items_) != len(set(items_))
    ),
)
@settings(deadline=None)
def test_verifier_rejects_list_field_with_duplicates(items):
    from modok.ingestion.verifier import verify_proposal
    from modok.llm.models import MetadataProposal

    proposal = MetadataProposal(
        proposed_fields={"modules": items},
        confidence=0.8,
        evidence="The document describes the ingestion pipeline in detail.",
        raw_response="{}",
    )
    registry = MagicMock()
    registry.has_module.return_value = True

    result = verify_proposal(proposal, ["modules"], {}, registry)

    rejected_names = [r.field for r in result.rejected_fields]
    assert "modules" in rejected_names
    assert any("duplicate" in r.reason.lower() for r in result.rejected_fields)


# ---------------------------------------------------------------------------
# LLM-VER-007 — reject empty values
# ---------------------------------------------------------------------------


# @spec LLM-VER-007
@given(
    field=st.sampled_from(["feature_slug", "modules", "tags"]),
    empty_val=st.one_of(st.none(), st.just(""), st.just([])),
)
@settings(deadline=None)
def test_verifier_rejects_empty_values(field, empty_val):
    from modok.ingestion.verifier import verify_proposal
    from modok.llm.models import MetadataProposal

    proposal = MetadataProposal(
        proposed_fields={field: empty_val},
        confidence=0.8,
        evidence="The document describes the ingestion pipeline in detail.",
        raw_response="{}",
    )
    registry = MagicMock()
    registry.has_feature.return_value = True
    registry.has_module.return_value = True

    result = verify_proposal(proposal, [field], {}, registry)

    rejected_names = [r.field for r in result.rejected_fields]
    assert field in rejected_names


# ---------------------------------------------------------------------------
# LLM-VER-008 — reject insufficient evidence (two-tier)
# ---------------------------------------------------------------------------


# @spec LLM-VER-008
def test_verifier_rejects_evidence_shorter_than_15_chars():
    from modok.ingestion.verifier import verify_proposal
    from modok.llm.models import MetadataProposal

    proposal = MetadataProposal(
        proposed_fields={"feature_slug": "ingestion"},
        confidence=0.8,
        evidence="See doc.",  # 8 chars — too short
        raw_response="{}",
    )
    registry = MagicMock()
    registry.has_feature.return_value = True

    result = verify_proposal(proposal, ["feature_slug"], {}, registry)

    assert not result.is_valid
    assert all("evidence" in r.reason.lower() for r in result.rejected_fields)


# @spec LLM-VER-008
@pytest.mark.parametrize(
    "filler",
    [
        "The document is about validation.",
        "This document describes the feature.",
        "Based on the document content.",
        "The file mentions the module.",
    ],
)
def test_verifier_rejects_known_filler_patterns(filler):
    from modok.ingestion.verifier import verify_proposal
    from modok.llm.models import MetadataProposal

    proposal = MetadataProposal(
        proposed_fields={"feature_slug": "ingestion"},
        confidence=0.8,
        evidence=filler,
        raw_response="{}",
    )
    registry = MagicMock()
    registry.has_feature.return_value = True

    result = verify_proposal(proposal, ["feature_slug"], {}, registry)

    assert not result.is_valid
    assert all("evidence" in r.reason.lower() for r in result.rejected_fields)


# @spec LLM-VER-008
def test_verifier_accepts_short_but_specific_evidence_fails_hard_check():
    """A 14-char specific string still fails the hard check — must be written as a sentence."""
    from modok.ingestion.verifier import verify_proposal
    from modok.llm.models import MetadataProposal

    proposal = MetadataProposal(
        proposed_fields={"feature_slug": "ingestion"},
        confidence=0.8,
        evidence="shtp.c line 42",  # 14 chars, specific but too short
        raw_response="{}",
    )
    registry = MagicMock()
    registry.has_feature.return_value = True

    result = verify_proposal(proposal, ["feature_slug"], {}, registry)

    assert not result.is_valid


# ---------------------------------------------------------------------------
# LLM-VER-009 — verify_proposal is a pure function
# ---------------------------------------------------------------------------


# @spec LLM-VER-009
@given(
    field=st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L",))),
    value=st.text(min_size=1, max_size=20),
)
@settings(deadline=None)
def test_verify_proposal_does_not_mutate_inputs(field, value):
    from modok.ingestion.verifier import verify_proposal
    from modok.llm.models import MetadataProposal

    original_missing = [field]
    original_frontmatter = {"unrelated_key": "existing_value"}
    proposed = {field: value}

    proposal = MetadataProposal(
        proposed_fields=dict(proposed),
        confidence=0.5,
        # Valid evidence: long enough, no filler prefix — per-field checks run
        evidence="Frontmatter block explicitly sets this field to the given value.",
        raw_response="{}",
    )
    registry = MagicMock()
    registry.has_feature.return_value = True
    registry.has_module.return_value = True
    registry.has_error.return_value = True
    registry.feature_slugs.return_value = []

    verify_proposal(proposal, list(original_missing), dict(original_frontmatter), registry)

    assert original_missing == [field]
    assert original_frontmatter == {"unrelated_key": "existing_value"}


# ---------------------------------------------------------------------------
# LLM-VER-010/011 — is_valid flag and valid_fields / rejected_fields
# ---------------------------------------------------------------------------


# @spec LLM-VER-010
def test_verify_proposal_is_valid_true_when_all_fields_pass():
    from modok.ingestion.verifier import verify_proposal
    from modok.llm.models import MetadataProposal

    proposal = MetadataProposal(
        proposed_fields={"feature_slug": "ingestion"},
        confidence=0.8,
        evidence="The document describes the ingestion pipeline failing due to missing metadata.",
        raw_response="{}",
    )
    registry = MagicMock()
    registry.has_feature.return_value = True

    result = verify_proposal(proposal, ["feature_slug"], {}, registry)

    assert result.is_valid is True
    assert result.rejected_fields == []
    assert "feature_slug" in result.valid_fields


# @spec LLM-VER-011
def test_verify_proposal_is_valid_false_with_mixed_fields():
    from modok.ingestion.verifier import verify_proposal
    from modok.llm.models import MetadataProposal

    proposal = MetadataProposal(
        proposed_fields={"feature_slug": "ingestion", "owner": "platform"},
        confidence=0.8,
        evidence="The document describes the ingestion pipeline failing due to missing metadata.",
        raw_response="{}",
    )
    registry = MagicMock()
    registry.has_feature.return_value = True

    result = verify_proposal(proposal, ["feature_slug"], {}, registry)

    assert result.is_valid is False
    assert "feature_slug" in result.valid_fields
    assert any(r.field == "owner" for r in result.rejected_fields)


# ---------------------------------------------------------------------------
# LLM-CEGIS-001 — repair attempt called when initial verification fails
# ---------------------------------------------------------------------------


# @spec LLM-CEGIS-001
@pytest.mark.asyncio
async def test_cegis_repair_called_on_initial_verification_failure():
    from modok.ingestion.pipeline import run_llm_proposal_pass
    from modok.llm.models import MetadataProposal

    good_proposal = MetadataProposal(
        proposed_fields={"feature_slug": "ingestion"},
        confidence=0.8,
        evidence="The document describes the ingestion pipeline failing due to missing metadata.",
        raw_response="{}",
    )
    bad_proposal = MetadataProposal(
        proposed_fields={"feature_slug": "docs"},  # will fail registry check
        confidence=0.6,
        evidence="The document is about validation.",  # filler evidence
        raw_response="{}",
    )

    registry = MagicMock()
    registry.has_feature.side_effect = lambda slug: slug == "ingestion"
    registry.feature_slugs.return_value = ["ingestion", "retrieval"]

    propose_calls = []

    async def fake_propose(doc_path, frontmatter, missing_fields, repair_context=None, **kwargs):
        propose_calls.append(repair_context)
        return bad_proposal if repair_context is None else good_proposal

    with patch("modok.ingestion.pipeline.propose_metadata", new=fake_propose):
        with patch(
            "modok.ingestion.pipeline._load_llm_config", return_value={"cegis_fix_enabled": True}
        ):
            await run_llm_proposal_pass(
                doc_path=Path("doc.md"),
                frontmatter={"doc_type": "lld"},
                missing_fields=["feature_slug"],
                registry=registry,
                strict=False,
                dry_run=False,
            )

    assert len(propose_calls) == 2
    assert propose_calls[0] is None  # initial call
    assert propose_calls[1] is not None  # repair call has counterexamples


# ---------------------------------------------------------------------------
# LLM-CEGIS-002 — repair only re-proposes rejected fields; accumulates valid_fields
# ---------------------------------------------------------------------------


# @spec LLM-CEGIS-002
@pytest.mark.asyncio
async def test_cegis_repair_only_proposes_rejected_fields():
    from modok.ingestion.pipeline import run_llm_proposal_pass
    from modok.llm.models import MetadataProposal

    # Initial: feature_slug passes, modules fails (wrong type)
    initial = MetadataProposal(
        proposed_fields={"feature_slug": "ingestion", "modules": "shtp"},
        confidence=0.8,
        evidence="The document describes the ingestion pipeline failing due to missing metadata.",
        raw_response="{}",
    )
    repair = MetadataProposal(
        proposed_fields={"modules": ["shtp"]},
        confidence=0.85,
        evidence="The document describes the ingestion pipeline failing due to missing metadata.",
        raw_response="{}",
    )

    registry = MagicMock()
    registry.has_feature.return_value = True
    registry.has_module.return_value = True

    repair_missing: list = []

    async def fake_propose(doc_path, frontmatter, missing_fields, repair_context=None, **kwargs):
        if repair_context is not None:
            repair_missing.extend(missing_fields)
        return initial if repair_context is None else repair

    with patch("modok.ingestion.pipeline.propose_metadata", new=fake_propose):
        with patch(
            "modok.ingestion.pipeline._load_llm_config", return_value={"cegis_fix_enabled": True}
        ):
            result = await run_llm_proposal_pass(
                doc_path=Path("doc.md"),
                frontmatter={},
                missing_fields=["feature_slug", "modules"],
                registry=registry,
                strict=False,
                dry_run=False,
            )

    # Repair should only request the rejected field, not the passing one
    assert "feature_slug" not in repair_missing
    assert "modules" in repair_missing
    # Both passes contribute to valid_fields
    assert "feature_slug" in result.valid_fields
    assert "modules" in result.valid_fields


# ---------------------------------------------------------------------------
# LLM-CEGIS-003 — at most one repair attempt
# ---------------------------------------------------------------------------


# @spec LLM-CEGIS-003
@pytest.mark.asyncio
async def test_cegis_makes_at_most_one_repair_attempt():
    from modok.ingestion.pipeline import run_llm_proposal_pass
    from modok.llm.models import MetadataProposal

    bad = MetadataProposal(
        proposed_fields={"feature_slug": "docs"},
        confidence=0.5,
        evidence="The document is about validation.",
        raw_response="{}",
    )
    registry = MagicMock()
    registry.has_feature.return_value = False
    registry.feature_slugs.return_value = ["ingestion"]

    call_count = 0

    async def fake_propose(doc_path, frontmatter, missing_fields, repair_context=None, **kwargs):
        nonlocal call_count
        call_count += 1
        return bad

    with patch("modok.ingestion.pipeline.propose_metadata", new=fake_propose):
        with patch(
            "modok.ingestion.pipeline._load_llm_config", return_value={"cegis_fix_enabled": True}
        ):
            await run_llm_proposal_pass(
                doc_path=Path("doc.md"),
                frontmatter={},
                missing_fields=["feature_slug"],
                registry=registry,
                strict=False,
                dry_run=False,
            )

    assert call_count == 2  # initial + exactly one repair


# ---------------------------------------------------------------------------
# LLM-CEGIS-004 — no repair when cegis_fix_enabled = false
# ---------------------------------------------------------------------------


# @spec LLM-CEGIS-004
@pytest.mark.asyncio
async def test_cegis_disabled_makes_no_repair_attempt():
    from modok.ingestion.pipeline import run_llm_proposal_pass
    from modok.llm.models import MetadataProposal

    bad = MetadataProposal(
        proposed_fields={"feature_slug": "docs"},
        confidence=0.5,
        evidence="The document is about validation.",
        raw_response="{}",
    )
    registry = MagicMock()
    registry.has_feature.return_value = False
    registry.feature_slugs.return_value = ["ingestion"]

    call_count = 0

    async def fake_propose(doc_path, frontmatter, missing_fields, repair_context=None, **kwargs):
        nonlocal call_count
        call_count += 1
        return bad

    with patch("modok.ingestion.pipeline.propose_metadata", new=fake_propose):
        with patch(
            "modok.ingestion.pipeline._load_llm_config", return_value={"cegis_fix_enabled": False}
        ):
            await run_llm_proposal_pass(
                doc_path=Path("doc.md"),
                frontmatter={},
                missing_fields=["feature_slug"],
                registry=registry,
                strict=False,
                dry_run=False,
            )

    assert call_count == 1  # initial only, no repair


# ---------------------------------------------------------------------------
# LLM-CEGIS-005 — total propose_metadata calls never exceed 2
# ---------------------------------------------------------------------------


# @spec LLM-CEGIS-005
@given(cegis_enabled=st.booleans())
@settings(deadline=None)
def test_total_propose_metadata_calls_never_exceed_two(cegis_enabled):
    import asyncio as _asyncio
    from modok.ingestion.pipeline import run_llm_proposal_pass
    from modok.llm.models import MetadataProposal

    bad = MetadataProposal(
        proposed_fields={"feature_slug": "docs"},
        confidence=0.5,
        evidence="The document is about validation.",
        raw_response="{}",
    )
    registry = MagicMock()
    registry.has_feature.return_value = False
    registry.feature_slugs.return_value = ["ingestion"]

    call_count = 0

    async def fake_propose(doc_path, frontmatter, missing_fields, repair_context=None, **kwargs):
        nonlocal call_count
        call_count += 1
        return bad

    async def run():
        with patch("modok.ingestion.pipeline.propose_metadata", new=fake_propose):
            with patch(
                "modok.ingestion.pipeline._load_llm_config",
                return_value={"cegis_fix_enabled": cegis_enabled},
            ):
                await run_llm_proposal_pass(
                    doc_path=Path("doc.md"),
                    frontmatter={},
                    missing_fields=["feature_slug"],
                    registry=registry,
                    strict=False,
                    dry_run=False,
                )

    _asyncio.run(run())
    assert call_count <= 2


# ---------------------------------------------------------------------------
# LLM-SUMM-001 — summarise_packet sends all required fields including matched_elements
# ---------------------------------------------------------------------------

VALID_SUMMARY_RESPONSE = (
    '{"summary": "The reinit_requested signal in device_card.py fails on USB reconnect."}'
)


# @spec LLM-SUMM-001
@pytest.mark.asyncio
async def test_summarise_packet_sends_all_required_fields():
    from modok.llm.gateway import summarise_packet

    with patch("modok.llm.gateway._load_config", return_value=make_config()):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = VALID_SUMMARY_RESPONSE
            await summarise_packet(
                issue_text="Tracker drops after USB reset",
                module_slugs=["device-card"],
                error_signatures=["usb-reset-error"],
                symptoms=["reinit fails"],
                relevant_files=["ui/device_card.py"],
                relevant_tests=["tests/test_device_card.py"],
                matched_elements=["reinit_requested"],
                recent_commits=[
                    {
                        "timestamp": "2024-01-15T10:00:00Z",
                        "author_name": "Dev",
                        "message": "fix reinit",
                    }
                ],
                known_issues=["USB reset causes pose dropout"],
                backend="local",
            )

    messages = mock_chat.call_args.kwargs.get("messages", [])
    user_content = next(m["content"] for m in messages if m.get("role") == "user")
    assert "Tracker drops after USB reset" in user_content
    assert "device-card" in user_content
    assert "usb-reset-error" in user_content
    assert "reinit fails" in user_content
    assert "reinit_requested" in user_content
    assert "ui/device_card.py" in user_content
    assert "USB reset causes pose dropout" in user_content


# @spec LLM-SUMM-001
@pytest.mark.asyncio
async def test_summarise_packet_includes_matched_elements_in_user_content():
    from modok.llm.gateway import summarise_packet

    with patch("modok.llm.gateway._load_config", return_value=make_config()):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = VALID_SUMMARY_RESPONSE
            await summarise_packet(
                issue_text="issue",
                module_slugs=[],
                error_signatures=[],
                symptoms=[],
                relevant_files=[],
                relevant_tests=[],
                matched_elements=["reinit_requested", "DeviceCard"],
                recent_commits=[],
                known_issues=[],
                backend="local",
            )

    messages = mock_chat.call_args.kwargs.get("messages", [])
    user_content = next(m["content"] for m in messages if m.get("role") == "user")
    assert "reinit_requested" in user_content
    assert "DeviceCard" in user_content


# ---------------------------------------------------------------------------
# LLM-SUMM-002 — system prompt includes element priority ordering
# ---------------------------------------------------------------------------


# @spec LLM-SUMM-002
def test_summarise_packet_system_prompt_names_element_priority():
    from modok.llm import prompts

    prompt = prompts.SUMMARISE_PACKET_SYSTEM
    # Priority 1 must be matched elements
    assert "matched element" in prompt.lower() or "Matched element" in prompt
    # Explicit instruction to name elements when present
    assert "name" in prompt.lower()


# @spec LLM-SUMM-002
@pytest.mark.asyncio
async def test_summarise_packet_sends_system_prompt_with_priority_instruction():
    from modok.llm.gateway import summarise_packet
    from modok.llm import prompts

    with patch("modok.llm.gateway._load_config", return_value=make_config()):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = VALID_SUMMARY_RESPONSE
            await summarise_packet(
                issue_text="issue",
                module_slugs=[],
                error_signatures=[],
                symptoms=[],
                relevant_files=[],
                relevant_tests=[],
                matched_elements=[],
                recent_commits=[],
                known_issues=[],
            )

    messages = mock_chat.call_args.kwargs.get("messages", [])
    system_content = next(m["content"] for m in messages if m.get("role") == "system")
    assert any(word in system_content for word in prompts.SUMMARISE_PACKET_SYSTEM.split()[:5])


# ---------------------------------------------------------------------------
# LLM-SUMM-003 — uses SUMMARISE_PACKET_SYSTEM; validates {"summary": "..."} before returning string
# ---------------------------------------------------------------------------


# @spec LLM-SUMM-003
@pytest.mark.asyncio
async def test_summarise_packet_uses_summarise_packet_system_prompt():
    from modok.llm.gateway import summarise_packet
    from modok.llm import prompts

    with patch("modok.llm.gateway._load_config", return_value=make_config()):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = VALID_SUMMARY_RESPONSE
            await summarise_packet(
                issue_text="issue",
                module_slugs=[],
                error_signatures=[],
                symptoms=[],
                relevant_files=[],
                relevant_tests=[],
                matched_elements=[],
                recent_commits=[],
                known_issues=[],
            )

    messages = mock_chat.call_args.kwargs.get("messages", [])
    system_content = next(m["content"] for m in messages if m.get("role") == "system")
    assert system_content == prompts.SUMMARISE_PACKET_SYSTEM


# @spec LLM-SUMM-003
@pytest.mark.asyncio
async def test_summarise_packet_returns_plain_string_extracted_from_json():
    from modok.llm.gateway import summarise_packet

    with patch("modok.llm.gateway._load_config", return_value=make_config()):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = '{"summary": "The reinit_requested signal fails."}'
            result = await summarise_packet(
                issue_text="issue",
                module_slugs=[],
                error_signatures=[],
                symptoms=[],
                relevant_files=[],
                relevant_tests=[],
                matched_elements=[],
                recent_commits=[],
                known_issues=[],
            )

    assert isinstance(result, str)
    assert result == "The reinit_requested signal fails."


# @spec LLM-SUMM-003
@pytest.mark.asyncio
async def test_summarise_packet_raises_response_error_when_summary_key_absent():
    from modok.llm.gateway import summarise_packet

    with patch("modok.llm.gateway._load_config", return_value=make_config(max_retries=0)):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = '{"not_summary": "wrong"}'
            with pytest.raises(LLMResponseError):
                await summarise_packet(
                    issue_text="issue",
                    module_slugs=[],
                    error_signatures=[],
                    symptoms=[],
                    relevant_files=[],
                    relevant_tests=[],
                    matched_elements=[],
                    recent_commits=[],
                    known_issues=[],
                )


# ---------------------------------------------------------------------------
# LLM-SUMM-004 — summarise_packet never writes; returns plain string only
# ---------------------------------------------------------------------------


# @spec LLM-SUMM-004
def test_summarise_packet_not_in_write_path():
    import inspect
    import modok.llm.gateway as gw_mod

    src = inspect.getsource(gw_mod)
    summ_src = src.split("async def summarise_packet")[1].split("async def ")[0]
    assert "upsert_node" not in summ_src
    assert "write_edge" not in summ_src
    assert "write_text" not in summ_src
    assert "open(" not in summ_src


# @spec LLM-SUMM-004
@pytest.mark.asyncio
async def test_summarise_packet_return_type_is_str():
    from modok.llm.gateway import summarise_packet

    with patch("modok.llm.gateway._load_config", return_value=make_config()):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = VALID_SUMMARY_RESPONSE
            result = await summarise_packet(
                issue_text="issue",
                module_slugs=[],
                error_signatures=[],
                symptoms=[],
                relevant_files=[],
                relevant_tests=[],
                matched_elements=[],
                recent_commits=[],
                known_issues=[],
            )

    assert isinstance(result, str)
    assert len(result) > 0


# ---------------------------------------------------------------------------
# LLM-SUMM-004 — summarise_packet uses timeout_summarise_packet
# ---------------------------------------------------------------------------


# @spec LLM-RETRY-004, LLM-SUMM-001
@pytest.mark.asyncio
async def test_summarise_packet_uses_summarise_timeout():
    from modok.llm.gateway import summarise_packet

    cfg = make_config(timeout_seconds=30)
    cfg["timeout_summarise_packet"] = 45

    with patch("modok.llm.gateway._load_config", return_value=cfg):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = VALID_SUMMARY_RESPONSE
            await summarise_packet(
                issue_text="issue",
                module_slugs=[],
                error_signatures=[],
                symptoms=[],
                relevant_files=[],
                relevant_tests=[],
                matched_elements=[],
                recent_commits=[],
                known_issues=[],
            )

    assert mock_chat.call_args.kwargs.get("timeout") == 45


# ---------------------------------------------------------------------------
# LLM-TICKET-005 — legacy feature_slug (singular) normalized to feature_slugs list
# ---------------------------------------------------------------------------


# @spec LLM-TICKET-005
@pytest.mark.asyncio
async def test_legacy_feature_slug_string_normalized_to_list():
    from modok.llm.gateway import parse_ticket

    legacy_response = """\
{
  "feature_slug": "shtp-receiver",
  "error_signatures": ["shtp-version-mismatch"],
  "environment": {},
  "symptoms": [],
  "confidence": 0.8
}
"""
    with patch("modok.llm.gateway._load_config", return_value=make_config()):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = legacy_response
            result = await parse_ticket("text", "stagehand", backend="local")

    assert isinstance(result.feature_slugs, list)
    assert "shtp-receiver" in result.feature_slugs


# @spec LLM-TICKET-005
@pytest.mark.asyncio
async def test_legacy_feature_slug_does_not_duplicate_when_also_in_feature_slugs():
    from modok.llm.gateway import parse_ticket

    both_response = """\
{
  "feature_slugs": ["shtp-receiver"],
  "feature_slug": "shtp-receiver",
  "error_signatures": [],
  "environment": {},
  "symptoms": [],
  "confidence": 0.8
}
"""
    with patch("modok.llm.gateway._load_config", return_value=make_config()):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = both_response
            result = await parse_ticket("text", "stagehand", backend="local")

    assert result.feature_slugs.count("shtp-receiver") == 1


# @spec LLM-TICKET-005
@pytest.mark.asyncio
async def test_legacy_feature_slug_null_produces_empty_list():
    from modok.llm.gateway import parse_ticket

    null_slug_response = """\
{
  "feature_slug": null,
  "error_signatures": [],
  "environment": {},
  "symptoms": [],
  "confidence": 0.0
}
"""
    with patch("modok.llm.gateway._load_config", return_value=make_config()):
        with patch(
            "modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = null_slug_response
            result = await parse_ticket("text", "stagehand", backend="local")

    assert isinstance(result.feature_slugs, list)
    assert result.feature_slugs == []


# @spec LLM-PROMPT-002 — summarise_packet uses a distinct system prompt
def test_summarise_packet_system_prompt_distinct_from_other_prompts():
    import modok.llm.prompts as prompts_mod

    assert hasattr(prompts_mod, "SUMMARISE_PACKET_SYSTEM")
    assert prompts_mod.SUMMARISE_PACKET_SYSTEM != prompts_mod.PARSE_TICKET_SYSTEM
    assert prompts_mod.SUMMARISE_PACKET_SYSTEM != prompts_mod.PROPOSE_METADATA_SYSTEM
    assert prompts_mod.SUMMARISE_PACKET_SYSTEM != prompts_mod.PROPOSE_SIMILARITY_SYSTEM


# ---------------------------------------------------------------------------
# LLM-PROTO-001 — protocol field controls wire format (ollama vs openai)
# ---------------------------------------------------------------------------


# @spec LLM-PROTO-001
@pytest.mark.asyncio
async def test_ollama_protocol_uses_ollama_chat_completion():
    """A backend with protocol=ollama hits _ollama_chat_completion, not _chat_completion."""
    from modok.llm.gateway import parse_ticket

    cfg = make_backends_config([_ollama_backend()])
    with patch("modok.llm.gateway._load_config", return_value=cfg):
        with patch("modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock) as mock_ollama:
            with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_openai:
                mock_ollama.return_value = VALID_TICKET_RESPONSE
                await parse_ticket("text", "stagehand", backend="auto")

    assert mock_ollama.call_count == 1
    assert mock_openai.call_count == 0


# @spec LLM-PROTO-001
@pytest.mark.asyncio
async def test_openai_protocol_uses_chat_completion():
    """A backend with protocol=openai hits _chat_completion, not _ollama_chat_completion."""
    from modok.llm.gateway import parse_ticket

    cfg = make_backends_config([_openai_backend("http://localhost:10240", "qwen3", api_key="")])
    with patch("modok.llm.gateway._load_config", return_value=cfg):
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_openai:
            with patch("modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock) as mock_ollama:
                mock_openai.return_value = VALID_TICKET_RESPONSE
                await parse_ticket("text", "stagehand", backend="auto")

    assert mock_openai.call_count == 1
    assert mock_ollama.call_count == 0


# @spec LLM-PROTO-001
@pytest.mark.asyncio
async def test_openai_protocol_local_endpoint_still_uses_openai_wire_format():
    """protocol=openai on a localhost endpoint still uses /chat/completions, not /api/chat."""
    from modok.llm.gateway import parse_ticket

    cfg = make_backends_config([_openai_backend("http://localhost:10240", "mlx-model")])
    with patch("modok.llm.gateway._load_config", return_value=cfg):
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_openai:
            with patch("modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock) as mock_ollama:
                mock_openai.return_value = VALID_TICKET_RESPONSE
                await parse_ticket("text", "stagehand", backend="auto")

    assert mock_openai.call_count == 1
    assert mock_ollama.call_count == 0
    call_kwargs = mock_openai.call_args.kwargs
    assert "localhost:10240" in call_kwargs.get("endpoint", "")
    assert call_kwargs.get("model") == "mlx-model"


# ---------------------------------------------------------------------------
# LLM-MULTI-001 — multi-backend: auto walks list on validation failure
# ---------------------------------------------------------------------------


# @spec LLM-MULTI-001
@pytest.mark.asyncio
async def test_multi_backend_auto_escalates_to_second_on_validation_failure():
    """With two backends, auto mode escalates to backend[1] when backend[0] returns bad JSON."""
    from modok.llm.gateway import parse_ticket

    cfg = make_backends_config([
        _ollama_backend(),
        _openai_backend("https://api.anthropic.com/v1", "claude-haiku-4-5-20251001"),
    ])
    bad_response = '{"not_a_valid_ticket": true}'

    with patch("modok.llm.gateway._load_config", return_value=cfg):
        with patch("modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock) as mock_local:
            mock_local.return_value = bad_response
            with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_remote:
                mock_remote.return_value = VALID_TICKET_RESPONSE
                result = await parse_ticket("text", "stagehand", backend="auto")

    assert mock_local.call_count == 1
    assert mock_remote.call_count == 1
    assert isinstance(result, TicketParseResult)


# @spec LLM-MULTI-001
@pytest.mark.asyncio
async def test_multi_backend_first_mode_uses_only_first_backend():
    """mode=first (or backend=local) uses only backend[0] even when backend[1] is configured."""
    from modok.llm.gateway import parse_ticket

    cfg = make_backends_config([
        _ollama_backend(),
        _openai_backend("https://api.anthropic.com/v1", "claude-haiku-4-5-20251001"),
    ])

    with patch("modok.llm.gateway._load_config", return_value=cfg):
        with patch("modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock) as mock_local:
            mock_local.return_value = VALID_TICKET_RESPONSE
            with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_remote:
                await parse_ticket("text", "stagehand", backend="local")

    assert mock_local.call_count == 1
    assert mock_remote.call_count == 0


# @spec LLM-MULTI-001
@pytest.mark.asyncio
async def test_multi_backend_auto_succeeds_on_first_when_valid():
    """When backend[0] returns valid JSON, backend[1] is never called."""
    from modok.llm.gateway import parse_ticket

    cfg = make_backends_config([
        _ollama_backend(),
        _openai_backend("https://api.anthropic.com/v1", "claude-haiku-4-5-20251001"),
    ])

    with patch("modok.llm.gateway._load_config", return_value=cfg):
        with patch("modok.llm.gateway._ollama_chat_completion", new_callable=AsyncMock) as mock_local:
            mock_local.return_value = VALID_TICKET_RESPONSE
            with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_remote:
                result = await parse_ticket("text", "stagehand", backend="auto")

    assert mock_local.call_count == 1
    assert mock_remote.call_count == 0
    assert isinstance(result, TicketParseResult)


# @spec LLM-MULTI-001
@pytest.mark.asyncio
async def test_three_backends_walks_all_on_consecutive_validation_failures():
    """With three backends all returning bad JSON, all three are tried before raising."""
    from modok.llm.gateway import parse_ticket
    from modok.llm.errors import LLMResponseError

    cfg = make_backends_config([
        _ollama_backend(),
        _openai_backend("http://localhost:10240", "mlx-model"),
        _openai_backend("https://api.anthropic.com/v1", "claude-haiku-4-5-20251001"),
    ])
    bad_response = '{"not_a_valid_ticket": true}'
    call_log: list[str] = []

    async def fake_ollama(endpoint="", **kwargs):
        call_log.append("ollama")
        return bad_response

    async def fake_openai(endpoint="", **kwargs):
        call_log.append(f"openai:{endpoint}")
        return bad_response

    with patch("modok.llm.gateway._load_config", return_value=cfg):
        with patch("modok.llm.gateway._ollama_chat_completion", new=fake_ollama):
            with patch("modok.llm.gateway._chat_completion", new=fake_openai):
                with pytest.raises(LLMResponseError):
                    await parse_ticket("text", "stagehand", backend="auto")

    assert call_log[0] == "ollama"
    assert len(call_log) == 3


# ---------------------------------------------------------------------------
# LLM-MULTI-002 — _resolve_backends: new-style and legacy produce equivalent results
# ---------------------------------------------------------------------------


# @spec LLM-MULTI-002
def test_resolve_backends_new_style_single_ollama():
    from modok.llm.gateway import _resolve_backends

    cfg = {"backends": [{"protocol": "ollama", "endpoint": "http://localhost:11434", "model": "llama3.2", "api_key": ""}]}
    result = _resolve_backends(cfg)
    assert len(result) == 1
    assert result[0]["native_ollama"] is True
    assert result[0]["endpoint"] == "http://localhost:11434"


# @spec LLM-MULTI-002
def test_resolve_backends_new_style_single_openai():
    from modok.llm.gateway import _resolve_backends

    cfg = {"backends": [{"protocol": "openai", "endpoint": "http://localhost:10240", "model": "mlx-model", "api_key": ""}]}
    result = _resolve_backends(cfg)
    assert len(result) == 1
    assert result[0]["native_ollama"] is False
    assert result[0]["endpoint"] == "http://localhost:10240"


# @spec LLM-MULTI-002
def test_resolve_backends_legacy_with_remote_matches_new_style():
    from modok.llm.gateway import _resolve_backends

    legacy = {
        "local_endpoint": "http://localhost:11434",
        "local_model": "llama3.2",
        "remote_endpoint": "https://api.anthropic.com/v1",
        "remote_model": "claude-haiku-4-5-20251001",
        "remote_api_key": "sk-test",
    }
    new_style = {
        "backends": [
            {"protocol": "ollama", "endpoint": "http://localhost:11434", "model": "llama3.2", "api_key": ""},
            {"protocol": "openai", "endpoint": "https://api.anthropic.com/v1", "model": "claude-haiku-4-5-20251001", "api_key": "sk-test"},
        ]
    }
    r_legacy = _resolve_backends(legacy)
    r_new = _resolve_backends(new_style)

    assert len(r_legacy) == len(r_new) == 2
    assert r_legacy[0]["native_ollama"] == r_new[0]["native_ollama"]
    assert r_legacy[1]["native_ollama"] == r_new[1]["native_ollama"]
    assert r_legacy[1]["api_key"] == r_new[1]["api_key"] == "sk-test"


# @spec LLM-MULTI-002
def test_resolve_backends_legacy_single_local_no_remote():
    from modok.llm.gateway import _resolve_backends

    legacy = {"local_endpoint": "http://localhost:11434", "local_model": "llama3.2"}
    result = _resolve_backends(legacy)
    assert len(result) == 1
    assert result[0]["native_ollama"] is True
