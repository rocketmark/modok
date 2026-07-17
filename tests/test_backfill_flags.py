"""
Tests for `modok backfill-flags` — one-time catch-up computing FLAGS edges
(and backfilling created_at where missing) for GitHub tickets whose
one-time investigation fired before FileEscalation/RootCauseEscalation's
FLAGS write-back existed. Also covers write_flags_for_packet, the shared
helper extracted from _maybe_notify_github so both call sites use identical
logic.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner


def _mock_client(query_return=None):
    client = MagicMock()
    client.query = AsyncMock(return_value=query_return if query_return is not None else [])
    client.replace_edges_by_parts = AsyncMock()
    client.ping = AsyncMock(return_value=True)
    return client


def _candidate(path, kind="source", confidence="high"):
    from modok.retrieval.models import ScoredCandidate

    return ScoredCandidate(path=path, kind=kind, score=20.0, confidence=confidence, evidence=[])


def _packet(scored_candidates):
    from modok.retrieval.models import DebugPacket, IssueAnchors, IssueSummary

    return DebugPacket(
        issue=IssueSummary(summary="s", anchors=IssueAnchors(features=[], errors=[], symptoms=[])),
        affected_areas=[],
        relevant_files=[],
        relevant_tests=[],
        known_issues=[],
        prior_fixes=[],
        scored_candidates=scored_candidates,
        summary="s",
    )


# ---------------------------------------------------------------------------
# write_flags_for_packet — shared helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_flags_for_packet_writes_high_confidence_only():
    from modok.retrieval.engine import write_flags_for_packet

    client = _mock_client()
    packet = _packet([
        _candidate("a.py", confidence="high"),
        _candidate("b.py", confidence="medium"),
        _candidate("c.py", kind="test", confidence="high"),
    ])
    flagged = await write_flags_for_packet(client, "stagehand", "github", "42", packet)

    assert flagged == ["a.py"]
    call = client.replace_edges_by_parts.call_args
    assert call.args[0] == ("customer-issue", "stagehand", "github", "42")
    assert call.args[2] == [("file", "stagehand", "a.py")]


@pytest.mark.asyncio
async def test_write_flags_for_packet_empty_set_still_calls_replace():
    from modok.retrieval.engine import write_flags_for_packet

    client = _mock_client()
    packet = _packet([_candidate("a.py", confidence="medium")])
    flagged = await write_flags_for_packet(client, "stagehand", "github", "42", packet)

    assert flagged == []
    client.replace_edges_by_parts.assert_called_once()
    assert client.replace_edges_by_parts.call_args.args[2] == []


# ---------------------------------------------------------------------------
# backfill-flags CLI command
# ---------------------------------------------------------------------------


def _fake_config():
    fake_project = MagicMock()
    fake_project.slug = "stagehand"
    fake_project.repo = "/tmp/stagehand"
    fake_config = MagicMock()
    fake_config.project.return_value = fake_project
    return fake_config


def test_nothing_to_backfill_when_all_open_tickets_already_flagged():
    from modok.cli.commands.backfill_flags import backfill_flags_cmd

    client = _mock_client(query_return=[])
    client.query = AsyncMock(side_effect=[
        [["1"], ["2"]],  # open tickets
        [["1"], ["2"]],  # already flagged — same set
    ])

    with patch("modok.cli.commands.backfill_flags.ModokConfig.load", return_value=_fake_config()), \
         patch("modok.cli.commands.backfill_flags.require_quine", return_value=client), \
         patch("modok.cli.commands.backfill_flags.Registry", side_effect=Exception("no registry")):
        result = CliRunner().invoke(backfill_flags_cmd, ["--project", "stagehand"])

    assert result.exit_code == 0
    assert "Nothing to backfill" in result.output


def test_backfills_pending_ticket_writes_flags_and_created_at():
    from modok.cli.commands.backfill_flags import backfill_flags_cmd

    client = _mock_client()
    client.query = AsyncMock(side_effect=[
        [["31"]],  # open tickets
        [],  # already flagged (none)
        [["node-31"]],  # node id lookup
        [[None]],  # created_at check -> missing
        [["2026-07-16T17:54:20Z"]],  # earliest Investigation.triggered_at
        None,  # SET created_at
    ])
    packet = _packet([_candidate("client/stagehand_client/livelink_bus.py", confidence="high")])

    with patch("modok.cli.commands.backfill_flags.ModokConfig.load", return_value=_fake_config()), \
         patch("modok.cli.commands.backfill_flags.require_quine", return_value=client), \
         patch("modok.cli.commands.backfill_flags.Registry", side_effect=Exception("no registry")), \
         patch("modok.cli.commands.backfill_flags.retrieve", new=AsyncMock(return_value=packet)):
        result = CliRunner().invoke(backfill_flags_cmd, ["--project", "stagehand"])

    assert result.exit_code == 0, result.output
    client.replace_edges_by_parts.assert_called_once()
    set_call = [c for c in client.query.call_args_list if "SET ci.created_at" in c.args[0]]
    assert len(set_call) == 1
    assert set_call[0].args[1]["ts"] == "2026-07-16T17:54:20Z"


def test_created_at_not_touched_when_already_present():
    from modok.cli.commands.backfill_flags import backfill_flags_cmd

    client = _mock_client()
    client.query = AsyncMock(side_effect=[
        [["31"]],
        [],
        [["node-31"]],
        [["2026-07-01T00:00:00Z"]],  # created_at already present
    ])
    packet = _packet([_candidate("a.py", confidence="high")])

    with patch("modok.cli.commands.backfill_flags.ModokConfig.load", return_value=_fake_config()), \
         patch("modok.cli.commands.backfill_flags.require_quine", return_value=client), \
         patch("modok.cli.commands.backfill_flags.Registry", side_effect=Exception("no registry")), \
         patch("modok.cli.commands.backfill_flags.retrieve", new=AsyncMock(return_value=packet)):
        result = CliRunner().invoke(backfill_flags_cmd, ["--project", "stagehand"])

    assert result.exit_code == 0, result.output
    set_calls = [c for c in client.query.call_args_list if "SET ci.created_at" in c.args[0]]
    assert len(set_calls) == 0


def test_retrieve_failure_skips_ticket_without_aborting():
    from modok.cli.commands.backfill_flags import backfill_flags_cmd

    client = _mock_client()
    client.query = AsyncMock(side_effect=[
        [["31"]],
        [],
        [["node-31"]],
    ])

    with patch("modok.cli.commands.backfill_flags.ModokConfig.load", return_value=_fake_config()), \
         patch("modok.cli.commands.backfill_flags.require_quine", return_value=client), \
         patch("modok.cli.commands.backfill_flags.Registry", side_effect=Exception("no registry")), \
         patch("modok.cli.commands.backfill_flags.retrieve", new=AsyncMock(side_effect=Exception("boom"))):
        result = CliRunner().invoke(backfill_flags_cmd, ["--project", "stagehand"])

    assert result.exit_code == 0, result.output
    assert "skipped (retrieve failed" in result.output
    client.replace_edges_by_parts.assert_not_called()


def test_already_flagged_tickets_excluded_from_pending():
    from modok.cli.commands.backfill_flags import backfill_flags_cmd

    client = _mock_client()
    client.query = AsyncMock(side_effect=[
        [["31"], ["35"]],  # open tickets
        [["35"]],  # 35 already flagged
        [["node-31"]],  # only 31 processed
        [["2026-07-01T00:00:00Z"]],
    ])
    packet = _packet([_candidate("a.py", confidence="high")])

    with patch("modok.cli.commands.backfill_flags.ModokConfig.load", return_value=_fake_config()), \
         patch("modok.cli.commands.backfill_flags.require_quine", return_value=client), \
         patch("modok.cli.commands.backfill_flags.Registry", side_effect=Exception("no registry")), \
         patch("modok.cli.commands.backfill_flags.retrieve", new=AsyncMock(return_value=packet)) as mock_retrieve:
        result = CliRunner().invoke(backfill_flags_cmd, ["--project", "stagehand"])

    assert result.exit_code == 0, result.output
    mock_retrieve.assert_called_once()
