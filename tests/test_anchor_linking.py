"""
Tests for modok.ingestion.anchor_linking — mechanical, LLM-free linking of a
CustomerIssue's raw_text to already-validated ErrorSignature nodes.
All tests are written before implementation (Phase 5). Every test cites
the EARS spec it verifies via @spec annotation.

Note: SQ-ANCH-006 (call-site integration — every place a CustomerIssue node is
written invokes this function) is verified in test_webhook_receiver.py and
test_ingestion_github.py, next to the existing tests for those call sites,
not here.

Specs verified: SQ-ANCH-001, SQ-ANCH-002, SQ-ANCH-003, SQ-ANCH-004, SQ-ANCH-005.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from modok.ingestion.anchor_linking import link_customer_issue_error_anchors


def make_registries(tmp_path: Path, errors: dict[str, str] | None = None) -> Path:
    reg_dir = tmp_path / "registries"
    reg_dir.mkdir(parents=True, exist_ok=True)
    (reg_dir / "features.yml").write_text("features:\n  shtp-receiver:\n    name: SHTP Receiver\n")
    (reg_dir / "modules.yml").write_text("modules:\n  shtp:\n    name: SHTP\n")
    if errors:
        lines = ["errors:"]
        for slug, normalized in errors.items():
            lines.append(f"  {slug}:")
            lines.append(f"    normalized_error: {normalized!r}")
            lines.append(f"    description: test error")
        (reg_dir / "errors.yml").write_text("\n".join(lines) + "\n")
    return tmp_path


# ---------------------------------------------------------------------------
# SQ-ANCH-001 — word-boundary, case-insensitive match against errors.yml
# ---------------------------------------------------------------------------


# @spec SQ-ANCH-001
@pytest.mark.asyncio
async def test_links_error_mentioned_in_raw_text(tmp_path):
    repo_root = make_registries(tmp_path, errors={"gss-failure": "GSS_FAILURE"})
    client = AsyncMock()
    client.node_exists = AsyncMock(return_value=True)
    client.replace_edges = AsyncMock()

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
    client.node_exists = AsyncMock(return_value=True)
    client.replace_edges = AsyncMock()

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
    client.node_exists = AsyncMock(return_value=True)
    client.replace_edges = AsyncMock()

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
    client.node_exists = AsyncMock(return_value=False)
    client.replace_edges = AsyncMock()

    linked = await link_customer_issue_error_anchors(
        client, "stagehand", repo_root, "github", "42",
        "The solve failed with GSS_FAILURE.",
    )
    assert linked == []
    # Reconciliation still runs (SQ-ANCH-003) even when the final matched set
    # is empty — consistent with test_replace_edges_reconciles_stale_anchor_on_edited_text,
    # which expects the same call shape when nothing textually matches at all.
    client.replace_edges.assert_awaited_once()
    call = client.replace_edges.await_args
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
    client.node_exists = AsyncMock(return_value=True)
    client.replace_edges = AsyncMock()

    await link_customer_issue_error_anchors(
        client, "stagehand", repo_root, "github", "42",
        "Saw both GSS_FAILURE and NO_POSE in the same session.",
    )

    assert client.replace_edges.await_count == 1
    _, kwargs_or_args = client.replace_edges.await_args, client.replace_edges.await_args
    call = client.replace_edges.await_args
    to_ids = call.args[2] if len(call.args) > 2 else call.kwargs.get("to_ids")
    assert len(to_ids) == 2


# @spec SQ-ANCH-003
@pytest.mark.asyncio
async def test_replace_edges_reconciles_stale_anchor_on_edited_text(tmp_path):
    repo_root = make_registries(tmp_path, errors={"gss-failure": "GSS_FAILURE"})
    client = AsyncMock()
    client.node_exists = AsyncMock(return_value=True)
    client.replace_edges = AsyncMock()

    # Second call with text that no longer mentions the error must still call
    # replace_edges (with an empty target list) so the stale edge is cleared,
    # not merely skip writing — reconciliation is explicit, not additive-only.
    await link_customer_issue_error_anchors(
        client, "stagehand", repo_root, "github", "42", "Totally unrelated text now.",
    )
    assert client.replace_edges.await_count == 1
    call = client.replace_edges.await_args
    to_ids = call.args[2] if len(call.args) > 2 else call.kwargs.get("to_ids")
    assert to_ids == []


# ---------------------------------------------------------------------------
# SQ-ANCH-004 — empty/None raw_text performs no matching
# ---------------------------------------------------------------------------


# @spec SQ-ANCH-004
@pytest.mark.asyncio
async def test_none_raw_text_performs_no_matching(tmp_path):
    repo_root = make_registries(tmp_path, errors={"gss-failure": "GSS_FAILURE"})
    client = AsyncMock()

    linked = await link_customer_issue_error_anchors(
        client, "stagehand", repo_root, "github", "42", None,
    )
    assert linked == []
    client.replace_edges.assert_not_called()
    client.node_exists.assert_not_called()


# @spec SQ-ANCH-004
@pytest.mark.asyncio
async def test_empty_raw_text_performs_no_matching(tmp_path):
    repo_root = make_registries(tmp_path, errors={"gss-failure": "GSS_FAILURE"})
    client = AsyncMock()

    linked = await link_customer_issue_error_anchors(
        client, "stagehand", repo_root, "github", "42", "",
    )
    assert linked == []
    client.replace_edges.assert_not_called()


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
    client.replace_edges.assert_not_called()
