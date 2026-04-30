"""
Tests for modok.llm — the LLM gateway.
All tests written before implementation (Phase 5). Every test cites
the EARS spec it verifies via @spec annotation.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import given, settings
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


VALID_TICKET_RESPONSE = """\
{
  "feature_slug": "shtp-receiver",
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
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_chat:
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
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_chat:
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
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_chat:
            # local returns bad JSON, remote returns good JSON
            mock_chat.side_effect = [bad_response, VALID_TICKET_RESPONSE]
            result = await parse_ticket("ticket text", "stagehand", backend="auto")

    assert mock_chat.call_count == 2
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
        async def fake_chat(endpoint="", **kwargs):
            if "anthropic" in endpoint:
                remote_calls.append(1)
            return bad_response

        with patch("modok.llm.gateway._load_config", return_value=cfg):
            with patch("modok.llm.gateway._chat_completion", new=fake_chat):
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
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_chat:
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
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_chat:
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
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_chat:
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
    import asyncio

    cfg = make_config(max_retries=2)

    with patch("modok.llm.gateway._load_config", return_value=cfg):
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_chat:
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
    import asyncio

    cfg = make_config(
        max_retries=1,
        remote_endpoint="https://api.anthropic.com/v1",
        remote_model="claude-sonnet-4-6",
        remote_api_key="sk-test",
    )
    with patch("modok.llm.gateway._load_config", return_value=cfg):
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_chat:
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
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_chat:
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
    import asyncio

    cfg = make_config(max_retries=2)
    with patch("modok.llm.gateway._load_config", return_value=cfg):
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_chat:
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
            with patch("modok.llm.gateway._chat_completion", new=fake_chat):
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
            with patch("modok.llm.gateway._chat_completion", new=fake_chat):
                await parse_ticket("text", "stagehand", backend="local")

    _asyncio.run(run())
    assert captured.get("timeout") == default_timeout


# @spec LLM-RETRY-004 — spot-check propose_metadata uses its own timeout key
@pytest.mark.asyncio
async def test_propose_metadata_uses_metadata_timeout():
    from modok.llm.gateway import propose_metadata

    cfg = make_config(timeout_seconds=30, timeout_propose_metadata=15)
    with patch("modok.llm.gateway._load_config", return_value=cfg):
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_chat:
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
            with patch("modok.llm.gateway._chat_completion", new=fake_chat):
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
    from modok.llm.gateway import parse_ticket

    with patch("modok.llm.gateway._load_config", return_value=make_config()):
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = VALID_TICKET_RESPONSE
            await parse_ticket("text", "stagehand", backend="local")

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
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = VALID_TICKET_RESPONSE
            result = await parse_ticket("text", "stagehand", backend="local")

    assert isinstance(result, TicketParseResult)
    assert result.feature_slug == "shtp-receiver"
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
    wrapped = 'Sure! Here you go:\n' + VALID_TICKET_RESPONSE + '\nHope that helps.'
    with patch("modok.llm.gateway._load_config", return_value=make_config()):
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_chat:
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
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = "Sorry, I cannot help with that."
            with pytest.raises(LLMResponseError):
                await parse_ticket("text", "stagehand", backend="local")


# @spec LLM-VAL-005
@pytest.mark.asyncio
async def test_raises_response_error_when_json_fails_schema_validation():
    from modok.llm.gateway import parse_ticket

    bad_schema = '{"wrong_field": "value"}'
    with patch("modok.llm.gateway._load_config", return_value=make_config(max_retries=0)):
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_chat:
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
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = VALID_TICKET_RESPONSE
            result = await parse_ticket("customer reported pose dropout", "stagehand")

    assert result.feature_slug == "shtp-receiver"
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
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_chat:
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
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = VALID_TICKET_RESPONSE
            await parse_ticket("text", "stagehand")

    messages = mock_chat.call_args.kwargs.get("messages", [])
    system_content = next(
        (m["content"] for m in messages if m.get("role") == "system"), ""
    )
    # Must derive from the frozen template, not an arbitrary string
    assert any(
        word in system_content
        for word in prompts.PARSE_TICKET_SYSTEM.split()[:5]
    )


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
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_chat:
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
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = VALID_METADATA_RESPONSE
            await propose_metadata(Path("doc.md"), {}, ["modules"])

    messages = mock_chat.call_args.kwargs.get("messages", [])
    system_content = next(
        (m["content"] for m in messages if m.get("role") == "system"), ""
    )
    assert any(
        word in system_content
        for word in prompts.PROPOSE_METADATA_SYSTEM.split()[:5]
    )


# ---------------------------------------------------------------------------
# LLM-META-004 — ingestion pipeline catches LLMResponseError and warns
# ---------------------------------------------------------------------------

# @spec LLM-META-004
def test_ingestion_pipeline_catches_llm_response_error_and_warns(tmp_path):
    from modok.ingestion.pipeline import apply_llm_proposals
    from modok.ingestion.registry import Registry

    # apply_llm_proposals must not re-raise LLMResponseError
    client = MagicMock()
    with patch("modok.ingestion.pipeline.invoke_llm_gateway",
               side_effect=LLMResponseError("bad response")):
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
        KnownIssueSummary("ki-001", "SHTP version mismatch causes dropout",
                          ["shtp-version-mismatch"]),
    ]

    with patch("modok.llm.gateway._load_config", return_value=make_config()):
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_chat:
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
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_chat:
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
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = VALID_SIMILARITY_RESPONSE
            await propose_similarity(issue, candidates)

    messages = mock_chat.call_args.kwargs.get("messages", [])
    system_content = next(
        (m["content"] for m in messages if m.get("role") == "system"), ""
    )
    assert any(
        word in system_content
        for word in prompts.PROPOSE_SIMILARITY_SYSTEM.split()[:5]
    )


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
        with patch("modok.llm.gateway._chat_completion", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = VALID_TICKET_RESPONSE
            result = await parse_ticket("text", "stagehand")

    # The raw string comes back in the struct
    assert result.raw_response == VALID_TICKET_RESPONSE

    # The gateway source must not persist raw_response anywhere
    import inspect, modok.llm.gateway as gw_mod
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
