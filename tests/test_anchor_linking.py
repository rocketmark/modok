"""
Tests for modok.ingestion.anchor_linking — mechanical, LLM-free linking of a
CustomerIssue's raw_text to already-validated ErrorSignature/Feature nodes,
plus the LLM fallback classifier that runs only when both mechanical linkers
find nothing.
All tests are written before implementation (Phase 5). Every test cites
the EARS spec it verifies via @spec annotation.

Note: SQ-ANCH-006 (call-site integration — every place a CustomerIssue node is
written invokes both mechanical linkers) and SQ-LLMANCH-001/002 (the gating
decision of whether to call the LLM fallback at all) are verified in
test_webhook_receiver.py and test_ingestion_github.py, next to the existing
tests for those call sites, not here — this file only tests the behavior of
each function in isolation, given that it was called.

Specs verified: SQ-ANCH-001 through SQ-ANCH-010, SQ-LLMANCH-003 through
SQ-LLMANCH-007.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from modok.ingestion.anchor_linking import (
    classify_customer_issue_anchors,
    link_customer_issue_error_anchors,
    link_customer_issue_feature_anchors,
)
from modok.llm.errors import LLMGatewayError, LLMUnavailableError
from modok.llm.models import TicketParseResult


def make_registries(
    tmp_path: Path,
    errors: dict[str, str] | None = None,
    features: dict[str, str] | None = None,
) -> Path:
    reg_dir = tmp_path / "registries"
    reg_dir.mkdir(parents=True, exist_ok=True)
    if features:
        lines = ["features:"]
        for slug, name in features.items():
            lines.append(f"  {slug}:")
            lines.append(f"    name: {name!r}")
        (reg_dir / "features.yml").write_text("\n".join(lines) + "\n")
    else:
        (reg_dir / "features.yml").write_text(
            "features:\n  shtp-receiver:\n    name: SHTP Receiver\n"
        )
    (reg_dir / "modules.yml").write_text("modules:\n  shtp:\n    name: SHTP\n")
    if errors:
        lines = ["errors:"]
        for slug, normalized in errors.items():
            lines.append(f"  {slug}:")
            lines.append(f"    normalized_error: {normalized!r}")
            lines.append("    description: test error")
        (reg_dir / "errors.yml").write_text("\n".join(lines) + "\n")
    return tmp_path


def make_ticket_parse_result(
    feature_slugs: list[str] | None = None,
    error_signatures: list[str] | None = None,
) -> TicketParseResult:
    return TicketParseResult(
        feature_slugs=feature_slugs or [],
        error_signatures=error_signatures or [],
        environment={},
        symptoms=[],
        confidence=0.8,
        raw_response="{}",
    )


# ---------------------------------------------------------------------------
# SQ-ANCH-001 — word-boundary, case-insensitive match against errors.yml
# ---------------------------------------------------------------------------


# @spec SQ-ANCH-001
@pytest.mark.asyncio
async def test_links_error_mentioned_in_raw_text(tmp_path):
    repo_root = make_registries(tmp_path, errors={"gss-failure": "GSS_FAILURE"})
    client = AsyncMock()
    client.node_exists_by_parts = AsyncMock(return_value=True)
    client.replace_edges_by_parts = AsyncMock()

    linked = await link_customer_issue_error_anchors(
        client, "stagehand", repo_root, "github", "42",
        "The solve failed with GSS_FAILURE during resume.",
    )
    assert linked == ["GSS_FAILURE"]


# @spec SQ-ANCH-001
@pytest.mark.asyncio
async def test_links_error_case_insensitively(tmp_path):
    repo_root = make_registries(tmp_path, errors={"gss-failure": "GSS_FAILURE"})
    client = AsyncMock()
    client.node_exists_by_parts = AsyncMock(return_value=True)
    client.replace_edges_by_parts = AsyncMock()

    linked = await link_customer_issue_error_anchors(
        client, "stagehand", repo_root, "github", "42",
        "solve failed with gss_failure again",
    )
    assert linked == ["GSS_FAILURE"]


# @spec SQ-ANCH-001
@pytest.mark.asyncio
async def test_does_not_match_substring_inside_larger_word(tmp_path):
    repo_root = make_registries(tmp_path, errors={"gss": "GSS"})
    client = AsyncMock()
    client.node_exists_by_parts = AsyncMock(return_value=True)
    client.replace_edges_by_parts = AsyncMock()

    linked = await link_customer_issue_error_anchors(
        client, "stagehand", repo_root, "github", "42",
        "Calling GSSAPI failed unexpectedly.",
    )
    assert linked == []


# ---------------------------------------------------------------------------
# SQ-ANCH-002 — only link to ErrorSignature nodes that already exist
# ---------------------------------------------------------------------------


# @spec SQ-ANCH-002
@pytest.mark.asyncio
async def test_does_not_link_when_error_signature_node_absent(tmp_path):
    repo_root = make_registries(tmp_path, errors={"gss-failure": "GSS_FAILURE"})
    client = AsyncMock()
    client.node_exists_by_parts = AsyncMock(return_value=False)
    client.replace_edges_by_parts = AsyncMock()

    linked = await link_customer_issue_error_anchors(
        client, "stagehand", repo_root, "github", "42",
        "The solve failed with GSS_FAILURE.",
    )
    assert linked == []
    # Reconciliation still runs (SQ-ANCH-003) even when the final matched set
    # is empty — consistent with test_replace_edges_reconciles_stale_anchor_on_edited_text,
    # which expects the same call shape when nothing textually matches at all.
    client.replace_edges_by_parts.assert_awaited_once()
    call = client.replace_edges_by_parts.await_args
    to_ids = call.args[2] if len(call.args) > 2 else call.kwargs.get("to_ids")
    assert to_ids == []


# @spec SQ-ANCH-002
@pytest.mark.asyncio
async def test_never_creates_error_signature_node():
    # link_customer_issue_error_anchors has no upsert_node call at all — it can
    # only ever write edges to nodes that already exist.
    import inspect

    from modok.ingestion import anchor_linking

    source = inspect.getsource(anchor_linking)
    assert "upsert_node" not in source


# ---------------------------------------------------------------------------
# SQ-ANCH-003 — replace_edges reconciles the full set, once
# ---------------------------------------------------------------------------


# @spec SQ-ANCH-003
@pytest.mark.asyncio
async def test_calls_replace_edges_once_with_full_matched_set(tmp_path):
    repo_root = make_registries(
        tmp_path, errors={"gss-failure": "GSS_FAILURE", "no-pose": "NO_POSE"}
    )
    client = AsyncMock()
    client.node_exists_by_parts = AsyncMock(return_value=True)
    client.replace_edges_by_parts = AsyncMock()

    await link_customer_issue_error_anchors(
        client, "stagehand", repo_root, "github", "42",
        "Saw both GSS_FAILURE and NO_POSE in the same session.",
    )

    assert client.replace_edges_by_parts.await_count == 1
    call = client.replace_edges_by_parts.await_args
    to_ids = call.args[2] if len(call.args) > 2 else call.kwargs.get("to_ids")
    assert len(to_ids) == 2


# @spec SQ-ANCH-003
@pytest.mark.asyncio
async def test_replace_edges_reconciles_stale_anchor_on_edited_text(tmp_path):
    repo_root = make_registries(tmp_path, errors={"gss-failure": "GSS_FAILURE"})
    client = AsyncMock()
    client.node_exists_by_parts = AsyncMock(return_value=True)
    client.replace_edges_by_parts = AsyncMock()

    # Second call with text that no longer mentions the error must still call
    # replace_edges (with an empty target list) so the stale edge is cleared,
    # not merely skip writing — reconciliation is explicit, not additive-only.
    await link_customer_issue_error_anchors(
        client, "stagehand", repo_root, "github", "42", "Totally unrelated text now.",
    )
    assert client.replace_edges_by_parts.await_count == 1
    call = client.replace_edges_by_parts.await_args
    to_ids = call.args[2] if len(call.args) > 2 else call.kwargs.get("to_ids")
    assert to_ids == []


# ---------------------------------------------------------------------------
# SQ-ANCH-004 — empty/None raw_text performs no matching
# ---------------------------------------------------------------------------


# @spec SQ-ANCH-001, CIING-MATCH-004
@pytest.mark.asyncio
async def test_links_multiple_errors_mentioned_in_same_raw_text(tmp_path):
    """Characterizes today's behavior before the CIING-MATCH-004/005 extraction
    into ErrorSignatureMatcher: multiple registered errors can match the same
    raw_text simultaneously, and all of them are linked, not just the first."""
    repo_root = make_registries(
        tmp_path, errors={"gss-failure": "GSS_FAILURE", "db-timeout": "DB_TIMEOUT"}
    )
    client = AsyncMock()
    client.node_exists_by_parts = AsyncMock(return_value=True)
    client.replace_edges_by_parts = AsyncMock()

    linked = await link_customer_issue_error_anchors(
        client, "stagehand", repo_root, "github", "42",
        "Saw both GSS_FAILURE and a DB_TIMEOUT in the logs.",
    )
    assert set(linked) == {"GSS_FAILURE", "DB_TIMEOUT"}


# @spec SQ-ANCH-004
@pytest.mark.asyncio
async def test_none_raw_text_performs_no_matching(tmp_path):
    repo_root = make_registries(tmp_path, errors={"gss-failure": "GSS_FAILURE"})
    client = AsyncMock()

    linked = await link_customer_issue_error_anchors(
        client, "stagehand", repo_root, "github", "42", None,
    )
    assert linked == []
    client.replace_edges_by_parts.assert_not_called()
    client.node_exists_by_parts.assert_not_called()


# @spec SQ-ANCH-004
@pytest.mark.asyncio
async def test_empty_raw_text_performs_no_matching(tmp_path):
    repo_root = make_registries(tmp_path, errors={"gss-failure": "GSS_FAILURE"})
    client = AsyncMock()

    linked = await link_customer_issue_error_anchors(
        client, "stagehand", repo_root, "github", "42", "",
    )
    assert linked == []
    client.replace_edges_by_parts.assert_not_called()


# ---------------------------------------------------------------------------
# SQ-ANCH-005 — missing registries degrades gracefully
# ---------------------------------------------------------------------------


# @spec SQ-ANCH-005
@pytest.mark.asyncio
async def test_missing_registries_returns_empty_without_raising(tmp_path):
    empty_root = tmp_path / "no-registries-here"
    empty_root.mkdir()
    client = AsyncMock()

    linked = await link_customer_issue_error_anchors(
        client, "stagehand", empty_root, "github", "42", "GSS_FAILURE happened",
    )
    assert linked == []
    client.replace_edges_by_parts.assert_not_called()


# ---------------------------------------------------------------------------
# SQ-ANCH-008 — token-overlap match against features.yml
# ---------------------------------------------------------------------------


# @spec SQ-ANCH-008
@pytest.mark.asyncio
async def test_links_feature_mentioned_via_token_overlap(tmp_path):
    repo_root = make_registries(tmp_path, features={"wifi-provisioning": "WiFi Provisioning"})
    client = AsyncMock()
    client.node_exists_by_parts = AsyncMock(return_value=True)
    client.replace_edges_by_parts = AsyncMock()

    linked = await link_customer_issue_feature_anchors(
        client, "stagehand", repo_root, "github", "42",
        "My wifi keeps dropping during setup.",
    )
    assert linked == ["wifi-provisioning"]


# @spec SQ-ANCH-008
@pytest.mark.asyncio
async def test_does_not_link_feature_without_token_overlap(tmp_path):
    repo_root = make_registries(tmp_path, features={"wifi-provisioning": "WiFi Provisioning"})
    client = AsyncMock()
    client.node_exists_by_parts = AsyncMock(return_value=True)
    client.replace_edges_by_parts = AsyncMock()

    linked = await link_customer_issue_feature_anchors(
        client, "stagehand", repo_root, "github", "42",
        "The camera calibration is completely wrong.",
    )
    assert linked == []


# ---------------------------------------------------------------------------
# SQ-ANCH-009 — only link to Feature nodes that already exist
# ---------------------------------------------------------------------------


# @spec SQ-ANCH-009
@pytest.mark.asyncio
async def test_does_not_link_feature_when_node_absent(tmp_path):
    repo_root = make_registries(tmp_path, features={"wifi-provisioning": "WiFi Provisioning"})
    client = AsyncMock()
    client.node_exists_by_parts = AsyncMock(return_value=False)
    client.replace_edges_by_parts = AsyncMock()

    linked = await link_customer_issue_feature_anchors(
        client, "stagehand", repo_root, "github", "42",
        "My wifi keeps dropping during setup.",
    )
    assert linked == []
    client.replace_edges_by_parts.assert_awaited_once()
    call = client.replace_edges_by_parts.await_args
    to_ids = call.args[2] if len(call.args) > 2 else call.kwargs.get("to_ids")
    assert to_ids == []


# ---------------------------------------------------------------------------
# SQ-ANCH-010 — replace_edges reconciles the full AFFECTS set, once
# ---------------------------------------------------------------------------


# @spec SQ-ANCH-010
@pytest.mark.asyncio
async def test_calls_replace_edges_once_with_full_matched_feature_set(tmp_path):
    repo_root = make_registries(
        tmp_path,
        features={"wifi-provisioning": "WiFi Provisioning", "camera-calibration": "Camera Calibration"},
    )
    client = AsyncMock()
    client.node_exists_by_parts = AsyncMock(return_value=True)
    client.replace_edges_by_parts = AsyncMock()

    await link_customer_issue_feature_anchors(
        client, "stagehand", repo_root, "github", "42",
        "Both wifi and camera calibration are broken after the update.",
    )

    assert client.replace_edges_by_parts.await_count == 1
    call = client.replace_edges_by_parts.await_args
    to_ids = call.args[2] if len(call.args) > 2 else call.kwargs.get("to_ids")
    assert len(to_ids) == 2


# @spec SQ-ANCH-008
@pytest.mark.asyncio
async def test_none_raw_text_performs_no_feature_matching(tmp_path):
    repo_root = make_registries(tmp_path, features={"wifi-provisioning": "WiFi Provisioning"})
    client = AsyncMock()

    linked = await link_customer_issue_feature_anchors(
        client, "stagehand", repo_root, "github", "42", None,
    )
    assert linked == []
    client.replace_edges_by_parts.assert_not_called()
    client.node_exists_by_parts.assert_not_called()


# @spec SQ-ANCH-008
@pytest.mark.asyncio
async def test_missing_registries_returns_empty_feature_list_without_raising(tmp_path):
    empty_root = tmp_path / "no-registries-here"
    empty_root.mkdir()
    client = AsyncMock()

    linked = await link_customer_issue_feature_anchors(
        client, "stagehand", empty_root, "github", "42", "wifi is broken",
    )
    assert linked == []
    client.replace_edges_by_parts.assert_not_called()


# ---------------------------------------------------------------------------
# SQ-LLMANCH-003 — write AFFECTS only for validated, existing Feature slugs
# ---------------------------------------------------------------------------


# @spec SQ-LLMANCH-003
@pytest.mark.asyncio
async def test_classify_writes_affects_only_for_registered_existing_feature(tmp_path):
    repo_root = make_registries(
        tmp_path,
        features={"wifi-provisioning": "WiFi Provisioning"},
        errors={},
    )
    client = AsyncMock()
    client.node_exists_by_parts = AsyncMock(return_value=True)
    client.replace_edges_by_parts = AsyncMock()

    result = make_ticket_parse_result(feature_slugs=["wifi-provisioning", "bogus-slug", "shtp"])
    with patch("modok.llm.gateway.parse_ticket", new=AsyncMock(return_value=result)):
        await classify_customer_issue_anchors(
            client, "stagehand", repo_root, "github", "42", "some free-form ticket text",
        )

    affects_calls = [c for c in client.replace_edges_by_parts.await_args_list if c.args[1] == "AFFECTS"]
    assert len(affects_calls) == 1
    to_parts = affects_calls[0].args[2]
    assert to_parts == [("feature", "stagehand", "wifi-provisioning")]


# @spec SQ-LLMANCH-003
@pytest.mark.asyncio
async def test_classify_does_not_write_affects_when_feature_node_absent(tmp_path):
    repo_root = make_registries(tmp_path, features={"wifi-provisioning": "WiFi Provisioning"})
    client = AsyncMock()
    client.node_exists_by_parts = AsyncMock(return_value=False)
    client.replace_edges_by_parts = AsyncMock()

    result = make_ticket_parse_result(feature_slugs=["wifi-provisioning"])
    with patch("modok.llm.gateway.parse_ticket", new=AsyncMock(return_value=result)):
        await classify_customer_issue_anchors(
            client, "stagehand", repo_root, "github", "42", "some free-form ticket text",
        )

    affects_calls = [c for c in client.replace_edges_by_parts.await_args_list if c.args[1] == "AFFECTS"]
    assert affects_calls[0].args[2] == []


# ---------------------------------------------------------------------------
# SQ-LLMANCH-004 — write HAS_ERROR only for validated, existing ErrorSignature slugs
# ---------------------------------------------------------------------------


# @spec SQ-LLMANCH-004
@pytest.mark.asyncio
async def test_classify_writes_has_error_only_for_registered_existing_error(tmp_path):
    repo_root = make_registries(tmp_path, errors={"gss-failure": "GSS_FAILURE"})
    client = AsyncMock()
    client.node_exists_by_parts = AsyncMock(return_value=True)
    client.replace_edges_by_parts = AsyncMock()

    result = make_ticket_parse_result(error_signatures=["GSS_FAILURE", "MADE_UP_ERROR"])
    with patch("modok.llm.gateway.parse_ticket", new=AsyncMock(return_value=result)):
        await classify_customer_issue_anchors(
            client, "stagehand", repo_root, "github", "42", "some free-form ticket text",
        )

    error_calls = [c for c in client.replace_edges_by_parts.await_args_list if c.args[1] == "HAS_ERROR"]
    assert len(error_calls) == 1
    to_parts = error_calls[0].args[2]
    assert to_parts == [("error", "stagehand", "GSS_FAILURE")]


# @spec SQ-LLMANCH-004
@pytest.mark.asyncio
async def test_classify_does_not_write_has_error_when_node_absent(tmp_path):
    repo_root = make_registries(tmp_path, errors={"gss-failure": "GSS_FAILURE"})
    client = AsyncMock()
    client.node_exists_by_parts = AsyncMock(return_value=False)
    client.replace_edges_by_parts = AsyncMock()

    result = make_ticket_parse_result(error_signatures=["GSS_FAILURE"])
    with patch("modok.llm.gateway.parse_ticket", new=AsyncMock(return_value=result)):
        await classify_customer_issue_anchors(
            client, "stagehand", repo_root, "github", "42", "some free-form ticket text",
        )

    error_calls = [c for c in client.replace_edges_by_parts.await_args_list if c.args[1] == "HAS_ERROR"]
    assert error_calls[0].args[2] == []


# ---------------------------------------------------------------------------
# SQ-LLMANCH-005 — replace_edges called once per edge type, even when empty
# ---------------------------------------------------------------------------


# @spec SQ-LLMANCH-005
@pytest.mark.asyncio
async def test_classify_calls_replace_edges_once_per_type_even_when_empty(tmp_path):
    repo_root = make_registries(tmp_path)
    client = AsyncMock()
    client.node_exists_by_parts = AsyncMock(return_value=True)
    client.replace_edges_by_parts = AsyncMock()

    result = make_ticket_parse_result()
    with patch("modok.llm.gateway.parse_ticket", new=AsyncMock(return_value=result)):
        await classify_customer_issue_anchors(
            client, "stagehand", repo_root, "github", "42", "some free-form ticket text",
        )

    assert client.replace_edges_by_parts.await_count == 2
    edge_types = {c.args[1] for c in client.replace_edges_by_parts.await_args_list}
    assert edge_types == {"AFFECTS", "HAS_ERROR"}


# ---------------------------------------------------------------------------
# SQ-LLMANCH-006 — LLM unavailable/rejected degrades to no writes, never raises
# ---------------------------------------------------------------------------


# @spec SQ-LLMANCH-006
@pytest.mark.asyncio
async def test_classify_llm_unavailable_writes_nothing(tmp_path):
    repo_root = make_registries(tmp_path)
    client = AsyncMock()
    client.replace_edges_by_parts = AsyncMock()

    with patch(
        "modok.llm.gateway.parse_ticket",
        new=AsyncMock(side_effect=LLMUnavailableError("no route to host")),
    ):
        await classify_customer_issue_anchors(
            client, "stagehand", repo_root, "github", "42", "some free-form ticket text",
        )

    client.replace_edges_by_parts.assert_not_called()


# @spec SQ-LLMANCH-006
@pytest.mark.asyncio
async def test_classify_llm_gateway_error_writes_nothing(tmp_path):
    repo_root = make_registries(tmp_path)
    client = AsyncMock()
    client.replace_edges_by_parts = AsyncMock()

    with patch(
        "modok.llm.gateway.parse_ticket",
        new=AsyncMock(side_effect=LLMGatewayError("400: bad request")),
    ):
        await classify_customer_issue_anchors(
            client, "stagehand", repo_root, "github", "42", "some free-form ticket text",
        )

    client.replace_edges_by_parts.assert_not_called()


# ---------------------------------------------------------------------------
# SQ-LLMANCH-007 / SQ-LLMANCH-001 — missing registries / empty raw_text skip the LLM call entirely
# ---------------------------------------------------------------------------


# @spec SQ-LLMANCH-007
@pytest.mark.asyncio
async def test_classify_missing_registries_skips_llm_call(tmp_path):
    empty_root = tmp_path / "no-registries-here"
    empty_root.mkdir()
    client = AsyncMock()
    client.replace_edges_by_parts = AsyncMock()

    with patch("modok.llm.gateway.parse_ticket", new=AsyncMock()) as mock_parse:
        await classify_customer_issue_anchors(
            client, "stagehand", empty_root, "github", "42", "some free-form ticket text",
        )

    mock_parse.assert_not_called()
    client.replace_edges_by_parts.assert_not_called()


# @spec SQ-LLMANCH-001
@pytest.mark.asyncio
async def test_classify_none_raw_text_skips_llm_call(tmp_path):
    repo_root = make_registries(tmp_path)
    client = AsyncMock()
    client.replace_edges_by_parts = AsyncMock()

    with patch("modok.llm.gateway.parse_ticket", new=AsyncMock()) as mock_parse:
        await classify_customer_issue_anchors(
            client, "stagehand", repo_root, "github", "42", None,
        )

    mock_parse.assert_not_called()
    client.replace_edges_by_parts.assert_not_called()
