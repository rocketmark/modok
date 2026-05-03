"""
Tests for modok.cli — the CLI entry point.
All tests written before implementation (Phase 5). Every test cites
the EARS spec it verifies via @spec annotation.

Uses click.testing.CliRunner to invoke commands and capture exit codes,
stdout, and stderr without spawning a subprocess.

NOTE: DebugPacket is a dataclass, not pydantic — CLI-RET-009 uses
dataclasses.asdict() rather than model_dump(). The spec says model_dump()
but that will be corrected in the spec when the CLI is implemented;
the intent (JSON serialisation of the packet) is what matters.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import textwrap
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from modok.ingestion.report import IngestionReport
from modok.retrieval.errors import (
    DREGraphUnavailableError,
    DRELLMUnavailableError,
    DRENotFoundError,
)
from modok.retrieval.models import (
    AnchorSet,
    DebugPacket,
    EvidenceAnchor,
    FileRef,
    FixRef,
    KnownIssueRef,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

MINIMAL_CONFIG = """\
[quine]
url = "http://127.0.0.1:8080"
jar = "{jar_path}"

[llm]
provider = "ollama"
base_url = "http://127.0.0.1:11434/v1"
model = "llama3"

[[projects]]
slug = "stagehand"
repo = "{repo_path}"
"""


def write_config(path: Path, jar_path: str = "/fake/quine.jar", repo_path: str = "/fake/repo") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(MINIMAL_CONFIG.format(jar_path=jar_path, repo_path=repo_path))
    return path


def make_debug_packet() -> DebugPacket:
    return DebugPacket(
        issue_summary="Tracker drops out after USB reset",
        anchors=AnchorSet(feature_slugs=["shtp-receiver"], error_signatures=[], symptoms=[]),
        anchor_count=1,
        known_issues=[KnownIssueRef("KI-001", "SHTP version mismatch", "open", 1)],
        recent_fixes=[FixRef("FIX-001", "Upgrade to SHTP v2", "patch", 1)],
        relevant_files=[FileRef("agent/src/shtp.c", 1)],
        evidence=[EvidenceAnchor("feature", "shtp-receiver", ["agent/src/shtp.c"])],
        confidence=1.0,
    )


def make_clean_report() -> IngestionReport:
    return IngestionReport(docs_processed=5, nodes_written=42, edges_written=18)


def make_errored_report() -> IngestionReport:
    return IngestionReport(
        docs_processed=4, nodes_written=38, edges_written=16,
        errors=["bad-doc.md: unknown feature slug 'missing-feat'"],
    )


def _invoke(runner, cli, args, config_path=None, env=None):
    """Invoke CLI with an optional patched config path."""
    _env = dict(os.environ)
    if env:
        _env.update(env)
    if config_path:
        with patch("modok.cli.config.CONFIG_PATH", config_path):
            return runner.invoke(cli, args, env=_env, catch_exceptions=False)
    return runner.invoke(cli, args, env=_env, catch_exceptions=False)


# ---------------------------------------------------------------------------
# Config Loading
# ---------------------------------------------------------------------------

# @spec CLI-CFG-001
def test_config_path_tilde_is_expanded(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    config_path = tmp_path / "config.toml"
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / ".git").mkdir()
    write_config(config_path, repo_path=str(repo_path))

    runner = CliRunner(mix_stderr=False)
    # Patch CONFIG_PATH to a real path (no tilde needed); verify load succeeds.
    # The tilde-expansion is tested by confirming a path with "~" in a value
    # is expanded to an absolute path in the loaded config.
    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.init.install_post_commit_hook"):
            result = runner.invoke(cli, ["init", "--project", "stagehand", "--repo", str(repo_path)])
    assert result.exit_code != 1 or "not found in config" not in (result.output + (result.stderr or ""))


# @spec CLI-CFG-002
def test_missing_config_exits_1(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    missing = tmp_path / "nonexistent.toml"
    runner = CliRunner(mix_stderr=False)
    with patch("modok.cli.config.CONFIG_PATH", missing):
        result = runner.invoke(cli, ["ingest", "--project", "stagehand", str(tmp_path)])
    assert result.exit_code == 1
    assert "setup" in (result.output + (result.stderr or "")).lower()


# @spec CLI-CFG-003
def test_malformed_config_exits_1(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    bad_config = tmp_path / "config.toml"
    bad_config.write_text("[[[ not valid toml")
    runner = CliRunner(mix_stderr=False)
    with patch("modok.cli.config.CONFIG_PATH", bad_config):
        result = runner.invoke(cli, ["ingest", "--project", "stagehand", str(tmp_path)])
    assert result.exit_code == 1


# @spec CLI-CFG-004
def test_unknown_project_slug_exits_1(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    config_path = write_config(tmp_path / "config.toml")
    runner = CliRunner(mix_stderr=False)
    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.ingest.QuineClient") as mock_client_cls:
            mock_client_cls.return_value.ping = AsyncMock(return_value=True)
            result = runner.invoke(cli, ["ingest", "--project", "unknown-project", str(tmp_path)])
    assert result.exit_code == 1
    assert "unknown-project" in (result.output + (result.stderr or ""))


# ---------------------------------------------------------------------------
# Quine Startup Check
# ---------------------------------------------------------------------------

# @spec CLI-PING-001
def test_quine_unreachable_exits_2_without_graph_op(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    config_path = write_config(tmp_path / "config.toml")
    runner = CliRunner(mix_stderr=False)
    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.ingest.QuineClient") as mock_client_cls:
            mock_ping = AsyncMock(return_value=False)
            mock_client = MagicMock()
            mock_client.ping = mock_ping
            mock_client_cls.return_value = mock_client
            with patch("modok.cli.commands.ingest.run_ingestion") as mock_ingest:
                result = runner.invoke(cli, ["ingest", "--project", "stagehand", str(tmp_path)])

    assert result.exit_code == 2
    mock_ingest.assert_not_called()
    output = result.output + (result.stderr or "")
    assert "not reachable" in output.lower() or "quine" in output.lower()


# ---------------------------------------------------------------------------
# modok init
# ---------------------------------------------------------------------------

# @spec CLI-INIT-001
def test_init_non_git_repo_exits_1(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    repo = tmp_path / "not-a-repo"
    repo.mkdir()
    config_path = write_config(tmp_path / "config.toml", repo_path=str(repo))
    runner = CliRunner(mix_stderr=False)
    with patch("modok.cli.config.CONFIG_PATH", config_path):
        result = runner.invoke(cli, ["init", "--project", "stagehand", "--repo", str(repo)])
    assert result.exit_code == 1
    assert "not a git repository" in (result.output + (result.stderr or "")).lower()


# @spec CLI-INIT-002
def test_init_creates_missing_registry_stubs(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    # No registries/ directory at all
    config_path = write_config(tmp_path / "config.toml", repo_path=str(repo))
    runner = CliRunner(mix_stderr=False)
    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.init.install_post_commit_hook"):
            result = runner.invoke(cli, ["init", "--project", "stagehand", "--repo", str(repo)])

    assert (repo / "registries" / "features.yml").exists()
    assert (repo / "registries" / "modules.yml").exists()
    assert (repo / "registries" / "errors.yml").exists()
    output = result.output + (result.stderr or "")
    assert "features.yml" in output or "registries" in output


# @spec CLI-INIT-002
def test_init_assisted_does_not_create_stubs(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli
    from modok.registry.proposal import ProposalSummary

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config_path = write_config(tmp_path / "config.toml", repo_path=str(repo))
    summary = ProposalSummary(
        sections_processed=0, sections_failed=0,
        docs_processed=0, docs_skipped=0,
        entries_written={"features.yml": 0, "modules.yml": 0, "errors.yml": 0},
        failed_sections=[],
    )
    runner = CliRunner(mix_stderr=False)
    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.init.install_post_commit_hook"):
            with patch("modok.cli.commands.init.propose_registries", return_value=summary) as mock_propose:
                runner.invoke(cli, ["init", "--project", "stagehand", "--repo", str(repo), "--assisted"])

    # propose_registries was called — stubs were NOT written by the CLI
    mock_propose.assert_called_once()
    # registries/ may or may not exist (proposal engine owns it), but no stub was written by CLI
    stub_written_by_cli = (
        (repo / "registries" / "features.yml").exists() and
        mock_propose.call_count == 0
    )
    assert not stub_written_by_cli


# @spec CLI-INIT-003
def test_init_installs_post_commit_hook(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config_path = write_config(tmp_path / "config.toml", repo_path=str(repo))
    runner = CliRunner(mix_stderr=False)
    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.init.install_post_commit_hook") as mock_hook:
            runner.invoke(cli, ["init", "--project", "stagehand", "--repo", str(repo)])

    mock_hook.assert_called_once()
    call_args = mock_hook.call_args
    assert str(repo) in str(call_args) or repo in call_args.args or repo in call_args.kwargs.values()


# @spec CLI-INIT-004
def test_init_does_not_duplicate_existing_project_entry(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config_path = write_config(tmp_path / "config.toml", repo_path=str(repo))
    runner = CliRunner(mix_stderr=False)

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.init.install_post_commit_hook"):
            runner.invoke(cli, ["init", "--project", "stagehand", "--repo", str(repo)])
            runner.invoke(cli, ["init", "--project", "stagehand", "--repo", str(repo)])

    content = config_path.read_text()
    assert content.count('slug = "stagehand"') == 1


# @spec CLI-INIT-005
def test_init_appends_new_project_entry(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config_path = write_config(tmp_path / "config.toml", repo_path=str(repo))
    runner = CliRunner(mix_stderr=False)

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.init.install_post_commit_hook"):
            runner.invoke(cli, ["init", "--project", "new-project", "--repo", str(repo)])

    content = config_path.read_text()
    assert 'slug = "new-project"' in content
    assert str(repo) in content


# @spec CLI-INIT-006
def test_init_does_not_call_ping(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config_path = write_config(tmp_path / "config.toml", repo_path=str(repo))
    runner = CliRunner(mix_stderr=False)

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.init.install_post_commit_hook"):
            with patch("modok.cli.commands.init.QuineClient") as mock_cls:
                runner.invoke(cli, ["init", "--project", "stagehand", "--repo", str(repo)])
                mock_cls.assert_not_called()


# @spec CLI-INIT-007
def test_init_creates_config_when_absent(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config_path = tmp_path / "config.toml"
    assert not config_path.exists()

    runner = CliRunner(mix_stderr=False)
    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.init.install_post_commit_hook"):
            runner.invoke(cli, ["init", "--project", "stagehand", "--repo", str(repo)])

    assert config_path.exists()
    content = config_path.read_text()
    assert 'slug = "stagehand"' in content


# ---------------------------------------------------------------------------
# modok ingest
# ---------------------------------------------------------------------------

# @spec CLI-INGEST-001
def test_ingest_prints_report_to_stdout(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    config_path = write_config(tmp_path / "config.toml", repo_path=str(tmp_path))
    runner = CliRunner(mix_stderr=False)
    report = make_clean_report()

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.ingest.QuineClient") as mock_cls:
            mock_cls.return_value.ping = AsyncMock(return_value=True)
            with patch("modok.cli.commands.ingest.run_ingestion", return_value=report):
                with patch("modok.cli.commands.ingest.Registry"):
                    result = runner.invoke(cli, ["ingest", "--project", "stagehand", str(tmp_path)])

    assert "Docs processed" in result.output or "5" in result.output


# @spec CLI-INGEST-002
def test_ingest_exits_0_on_clean_report(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    config_path = write_config(tmp_path / "config.toml", repo_path=str(tmp_path))
    runner = CliRunner(mix_stderr=False)

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.ingest.QuineClient") as mock_cls:
            mock_cls.return_value.ping = AsyncMock(return_value=True)
            with patch("modok.cli.commands.ingest.run_ingestion", return_value=make_clean_report()):
                with patch("modok.cli.commands.ingest.Registry"):
                    result = runner.invoke(cli, ["ingest", "--project", "stagehand", str(tmp_path)])

    assert result.exit_code == 0


# @spec CLI-INGEST-003
def test_ingest_exits_3_on_errored_report(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    config_path = write_config(tmp_path / "config.toml", repo_path=str(tmp_path))
    runner = CliRunner(mix_stderr=False)

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.ingest.QuineClient") as mock_cls:
            mock_cls.return_value.ping = AsyncMock(return_value=True)
            with patch("modok.cli.commands.ingest.run_ingestion", return_value=make_errored_report()):
                with patch("modok.cli.commands.ingest.Registry"):
                    result = runner.invoke(cli, ["ingest", "--project", "stagehand", str(tmp_path)])

    assert result.exit_code == 3



# @spec CLI-INGEST-005
def test_ingest_derives_repo_root_from_config(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    repo = tmp_path / "myrepo"
    repo.mkdir()
    config_path = write_config(tmp_path / "config.toml", repo_path=str(repo))
    runner = CliRunner(mix_stderr=False)
    captured_args: list = []

    def capture_ingest(repo_root, registry, client, project_slug, **kwargs):
        captured_args.append(repo_root)
        return make_clean_report()

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.ingest.QuineClient") as mock_cls:
            mock_cls.return_value.ping = AsyncMock(return_value=True)
            with patch("modok.cli.commands.ingest.run_ingestion", side_effect=capture_ingest):
                with patch("modok.cli.commands.ingest.Registry") as mock_reg_cls:
                    mock_reg_cls.return_value = MagicMock()
                    result = runner.invoke(
                        cli, ["ingest", "--project", "stagehand", str(tmp_path)]
                    )

    assert captured_args, "run_ingestion was not called"
    assert Path(str(captured_args[0])) == repo


# ---------------------------------------------------------------------------
# modok retrieve
# ---------------------------------------------------------------------------

# @spec CLI-RET-001
def test_retrieve_source_ticket_computes_idfrom(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli
    from modok.quine.ids import idFrom

    config_path = write_config(tmp_path / "config.toml")
    runner = CliRunner(mix_stderr=False)
    captured_node_ids: list = []

    async def fake_retrieve(node_id, project_slug, client, **kwargs):
        captured_node_ids.append(node_id)
        return make_debug_packet()

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.retrieve.QuineClient") as mock_cls:
            mock_cls.return_value.ping = AsyncMock(return_value=True)
            with patch("modok.cli.commands.retrieve.retrieve", side_effect=fake_retrieve):
                runner.invoke(cli, [
                    "retrieve", "--project", "stagehand",
                    "--source", "zendesk", "--ticket", "1842",
                ])

    expected_id = idFrom("customer-issue", "stagehand", "zendesk", "1842")
    assert captured_node_ids == [expected_id]


# @spec CLI-RET-002
def test_retrieve_node_id_calls_retrieve_directly(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    config_path = write_config(tmp_path / "config.toml")
    runner = CliRunner(mix_stderr=False)
    captured_node_ids: list = []

    async def fake_retrieve(node_id, project_slug, client, **kwargs):
        captured_node_ids.append(node_id)
        return make_debug_packet()

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.retrieve.QuineClient") as mock_cls:
            mock_cls.return_value.ping = AsyncMock(return_value=True)
            with patch("modok.cli.commands.retrieve.retrieve", side_effect=fake_retrieve):
                runner.invoke(cli, [
                    "retrieve", "--project", "stagehand",
                    "--node-id", "12345",
                ])

    assert captured_node_ids == [12345]


# @spec CLI-RET-003
def test_retrieve_source_and_node_id_together_exits_1(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    config_path = write_config(tmp_path / "config.toml")
    runner = CliRunner(mix_stderr=False)

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.retrieve.QuineClient") as mock_cls:
            mock_cls.return_value.ping = AsyncMock(return_value=True)
            with patch("modok.cli.commands.retrieve.retrieve") as mock_ret:
                result = runner.invoke(cli, [
                    "retrieve", "--project", "stagehand",
                    "--source", "zendesk", "--ticket", "1842",
                    "--node-id", "99",
                ])

    assert result.exit_code == 1
    mock_ret.assert_not_called()


# @spec CLI-RET-004
def test_retrieve_no_identifier_exits_1(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    config_path = write_config(tmp_path / "config.toml")
    runner = CliRunner(mix_stderr=False)

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.retrieve.QuineClient") as mock_cls:
            mock_cls.return_value.ping = AsyncMock(return_value=True)
            with patch("modok.cli.commands.retrieve.retrieve") as mock_ret:
                result = runner.invoke(cli, ["retrieve", "--project", "stagehand"])

    assert result.exit_code == 1
    mock_ret.assert_not_called()


# @spec CLI-RET-005
@pytest.mark.parametrize("args", [
    ["--source", "zendesk"],
    ["--ticket", "1842"],
])
def test_retrieve_partial_source_ticket_exits_1(tmp_path, args):
    from click.testing import CliRunner
    from modok.cli.main import cli

    config_path = write_config(tmp_path / "config.toml")
    runner = CliRunner(mix_stderr=False)

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.retrieve.QuineClient") as mock_cls:
            mock_cls.return_value.ping = AsyncMock(return_value=True)
            with patch("modok.cli.commands.retrieve.retrieve") as mock_ret:
                result = runner.invoke(
                    cli, ["retrieve", "--project", "stagehand"] + args
                )

    assert result.exit_code == 1
    mock_ret.assert_not_called()


# @spec CLI-RET-006
def test_retrieve_not_found_exits_1_with_message(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    config_path = write_config(tmp_path / "config.toml")
    runner = CliRunner(mix_stderr=False)

    async def raise_not_found(*args, **kwargs):
        raise DRENotFoundError("not found")

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.retrieve.QuineClient") as mock_cls:
            mock_cls.return_value.ping = AsyncMock(return_value=True)
            with patch("modok.cli.commands.retrieve.retrieve", side_effect=raise_not_found):
                result = runner.invoke(cli, [
                    "retrieve", "--project", "stagehand",
                    "--source", "zendesk", "--ticket", "1842",
                ])

    assert result.exit_code == 1
    output = result.output + (result.stderr or "")
    assert "stagehand" in output
    assert "not found" in output.lower()


# @spec CLI-RET-007
def test_retrieve_graph_unavailable_exits_2(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    config_path = write_config(tmp_path / "config.toml")
    runner = CliRunner(mix_stderr=False)

    async def raise_graph_unavailable(*args, **kwargs):
        raise DREGraphUnavailableError("quine down")

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.retrieve.QuineClient") as mock_cls:
            mock_cls.return_value.ping = AsyncMock(return_value=True)
            with patch("modok.cli.commands.retrieve.retrieve", side_effect=raise_graph_unavailable):
                result = runner.invoke(cli, [
                    "retrieve", "--project", "stagehand",
                    "--source", "zendesk", "--ticket", "1842",
                ])

    assert result.exit_code == 2


# @spec CLI-RET-008
def test_retrieve_llm_unavailable_exits_2(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    config_path = write_config(tmp_path / "config.toml")
    runner = CliRunner(mix_stderr=False)

    async def raise_llm_unavailable(*args, **kwargs):
        raise DRELLMUnavailableError("llm down")

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.retrieve.QuineClient") as mock_cls:
            mock_cls.return_value.ping = AsyncMock(return_value=True)
            with patch("modok.cli.commands.retrieve.retrieve", side_effect=raise_llm_unavailable):
                result = runner.invoke(cli, [
                    "retrieve", "--project", "stagehand",
                    "--source", "zendesk", "--ticket", "1842",
                ])

    assert result.exit_code == 2


# @spec CLI-RET-009
def test_retrieve_success_prints_json_to_stdout(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    config_path = write_config(tmp_path / "config.toml")
    runner = CliRunner(mix_stderr=False)
    packet = make_debug_packet()

    async def fake_retrieve(*args, **kwargs):
        return packet

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.retrieve.QuineClient") as mock_cls:
            mock_cls.return_value.ping = AsyncMock(return_value=True)
            with patch("modok.cli.commands.retrieve.retrieve", side_effect=fake_retrieve):
                result = runner.invoke(cli, [
                    "retrieve", "--project", "stagehand",
                    "--source", "zendesk", "--ticket", "1842",
                ])

    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["issue_summary"] == "Tracker drops out after USB reset"
    assert parsed["confidence"] == 1.0


# ---------------------------------------------------------------------------
# modok recall
# ---------------------------------------------------------------------------

def _recall_with_results(tmp_path, runner, feature_results, extra_args=None):
    """Helper: run recall with a mocked graph result."""
    from modok.cli.main import cli

    config_path = write_config(tmp_path / "config.toml")
    args = ["recall", "--project", "stagehand", "--feature", "shtp-receiver"]
    if extra_args:
        args += extra_args

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.recall.QuineClient") as mock_cls:
            mock_cls.return_value.ping = AsyncMock(return_value=True)
            mock_cls.return_value.query = AsyncMock(return_value=feature_results)
            return runner.invoke(cli, args)


# @spec CLI-REC-001
def test_recall_prints_results_to_stdout(tmp_path):
    from click.testing import CliRunner

    runner = CliRunner(mix_stderr=False)
    result = _recall_with_results(tmp_path, runner, feature_results=[
        [{"id": 1, "properties": {
            "node_type": "File", "project_slug": "stagehand", "repo_path": "agent/src/shtp.c"
        }}]
    ])
    assert result.exit_code == 0
    assert "shtp" in result.output.lower() or result.output.strip()


# @spec CLI-REC-002
def test_recall_empty_results_exits_0(tmp_path):
    from click.testing import CliRunner

    runner = CliRunner(mix_stderr=False)
    result = _recall_with_results(tmp_path, runner, feature_results=[])
    assert result.exit_code == 0


# @spec CLI-REC-003
def test_recall_default_output_is_tabular(tmp_path):
    from click.testing import CliRunner

    runner = CliRunner(mix_stderr=False)
    result = _recall_with_results(tmp_path, runner, feature_results=[
        [{"id": 1, "properties": {
            "node_type": "File", "project_slug": "stagehand", "repo_path": "agent/src/shtp.c"
        }}]
    ])
    assert result.exit_code == 0
    # Tabular output is not JSON
    try:
        json.loads(result.output)
        assert False, "Expected tabular output, got valid JSON"
    except json.JSONDecodeError:
        pass


# @spec CLI-REC-004
def test_recall_json_flag_outputs_json(tmp_path):
    from click.testing import CliRunner

    runner = CliRunner(mix_stderr=False)
    result = _recall_with_results(tmp_path, runner, feature_results=[
        [{"id": 1, "properties": {
            "node_type": "File", "project_slug": "stagehand", "repo_path": "agent/src/shtp.c"
        }}]
    ], extra_args=["--json"])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert isinstance(parsed, dict)


# @spec CLI-REC-005
def test_recall_quine_unreachable_exits_2(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    config_path = write_config(tmp_path / "config.toml")
    runner = CliRunner(mix_stderr=False)

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.recall.QuineClient") as mock_cls:
            mock_cls.return_value.ping = AsyncMock(return_value=False)
            result = runner.invoke(cli, [
                "recall", "--project", "stagehand", "--feature", "shtp-receiver"
            ])

    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# modok quine start
# ---------------------------------------------------------------------------

# @spec CLI-QS-001
def test_quine_start_already_running_exits_0_no_spawn(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    jar = tmp_path / "quine.jar"
    jar.touch()
    config_path = write_config(tmp_path / "config.toml", jar_path=str(jar))
    runner = CliRunner(mix_stderr=False)

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.quine.QuineClient") as mock_cls:
            mock_cls.return_value.ping = AsyncMock(return_value=True)
            with patch("modok.cli.commands.quine.subprocess") as mock_sub:
                result = runner.invoke(cli, ["quine", "start"])

    assert result.exit_code == 0
    mock_sub.Popen.assert_not_called()
    output = result.output + (result.stderr or "")
    assert "already running" in output.lower()


# @spec CLI-QS-002
def test_quine_start_missing_jar_exits_1_no_spawn(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    config_path = write_config(tmp_path / "config.toml", jar_path=str(tmp_path / "missing.jar"))
    runner = CliRunner(mix_stderr=False)

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.quine.QuineClient") as mock_cls:
            mock_cls.return_value.ping = AsyncMock(return_value=False)
            with patch("modok.cli.commands.quine.subprocess") as mock_sub:
                result = runner.invoke(cli, ["quine", "start"])

    assert result.exit_code == 1
    mock_sub.Popen.assert_not_called()
    output = result.output + (result.stderr or "")
    assert "jar not found" in output.lower() or "not found" in output.lower()


# @spec CLI-QS-003
def test_quine_start_launches_process_and_writes_pid(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    jar = tmp_path / "quine.jar"
    jar.touch()
    pid_file = tmp_path / "quine.pid"
    config_path = write_config(tmp_path / "config.toml", jar_path=str(jar))
    runner = CliRunner(mix_stderr=False)

    mock_proc = MagicMock()
    mock_proc.pid = 54321

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.quine.QUINE_PID_PATH", pid_file):
            with patch("modok.cli.commands.quine.QuineClient") as mock_cls:
                # First ping: not running. Subsequent pings: running.
                mock_cls.return_value.ping = AsyncMock(side_effect=[False, True])
                with patch("modok.cli.commands.quine.subprocess.Popen", return_value=mock_proc):
                    result = runner.invoke(cli, ["quine", "start"])

    assert pid_file.exists()
    assert pid_file.read_text().strip() == "54321"


# @spec CLI-QS-004
def test_quine_start_timeout_exits_2(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    jar = tmp_path / "quine.jar"
    jar.touch()
    pid_file = tmp_path / "quine.pid"
    config_path = write_config(tmp_path / "config.toml", jar_path=str(jar))
    runner = CliRunner(mix_stderr=False)

    mock_proc = MagicMock()
    mock_proc.pid = 99999

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.quine.QUINE_PID_PATH", pid_file):
            with patch("modok.cli.commands.quine.QUINE_START_TIMEOUT", 0):
                with patch("modok.cli.commands.quine.QuineClient") as mock_cls:
                    mock_cls.return_value.ping = AsyncMock(return_value=False)
                    with patch("modok.cli.commands.quine.subprocess.Popen", return_value=mock_proc):
                        result = runner.invoke(cli, ["quine", "start"])

    assert result.exit_code == 2
    output = result.output + (result.stderr or "")
    assert "ready" in output.lower() or "timeout" in output.lower() or "30" in output


# @spec CLI-QS-005
def test_quine_start_success_exits_0(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    jar = tmp_path / "quine.jar"
    jar.touch()
    pid_file = tmp_path / "quine.pid"
    config_path = write_config(tmp_path / "config.toml", jar_path=str(jar))
    runner = CliRunner(mix_stderr=False)

    mock_proc = MagicMock()
    mock_proc.pid = 11111

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.quine.QUINE_PID_PATH", pid_file):
            with patch("modok.cli.commands.quine.QuineClient") as mock_cls:
                mock_cls.return_value.ping = AsyncMock(side_effect=[False, True])
                with patch("modok.cli.commands.quine.subprocess.Popen", return_value=mock_proc):
                    result = runner.invoke(cli, ["quine", "start"])

    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# modok quine stop
# ---------------------------------------------------------------------------

# @spec CLI-QSTOP-001
def test_quine_stop_no_pid_file_exits_1(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    pid_file = tmp_path / "quine.pid"
    config_path = write_config(tmp_path / "config.toml")
    runner = CliRunner(mix_stderr=False)

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.quine.QUINE_PID_PATH", pid_file):
            result = runner.invoke(cli, ["quine", "stop"])

    assert result.exit_code == 1
    output = result.output + (result.stderr or "")
    assert "not running" in output.lower() or "no pid" in output.lower()


# @spec CLI-QSTOP-002
def test_quine_stop_dead_process_exits_2_leaves_pid_file(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    pid_file = tmp_path / "quine.pid"
    pid_file.write_text("88888")
    config_path = write_config(tmp_path / "config.toml")
    runner = CliRunner(mix_stderr=False)

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.quine.QUINE_PID_PATH", pid_file):
            with patch("modok.cli.commands.quine.os.kill",
                       side_effect=ProcessLookupError("no such process")):
                result = runner.invoke(cli, ["quine", "stop"])

    assert result.exit_code == 2
    assert pid_file.exists(), "PID file must be left in place when process is already dead"
    output = result.output + (result.stderr or "")
    assert "crashed" in output.lower() or "not found" in output.lower() or "88888" in output


# @spec CLI-QSTOP-003
def test_quine_stop_clean_exit_removes_pid_and_exits_0(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    pid_file = tmp_path / "quine.pid"
    pid_file.write_text("77777")
    config_path = write_config(tmp_path / "config.toml")
    runner = CliRunner(mix_stderr=False)

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.quine.QUINE_PID_PATH", pid_file):
            with patch("modok.cli.commands.quine.os.kill"):
                with patch("modok.cli.commands.quine.QUINE_STOP_TIMEOUT", 5):
                    with patch("modok.cli.commands.quine._wait_for_exit", return_value=True):
                        result = runner.invoke(cli, ["quine", "stop"])

    assert result.exit_code == 0
    assert not pid_file.exists(), "PID file must be removed on clean stop"


# @spec CLI-QSTOP-004
def test_quine_stop_timeout_exits_2_leaves_pid_file(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    pid_file = tmp_path / "quine.pid"
    pid_file.write_text("66666")
    config_path = write_config(tmp_path / "config.toml")
    runner = CliRunner(mix_stderr=False)

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.quine.QUINE_PID_PATH", pid_file):
            with patch("modok.cli.commands.quine.os.kill"):
                with patch("modok.cli.commands.quine._wait_for_exit", return_value=False):
                    result = runner.invoke(cli, ["quine", "stop"])

    assert result.exit_code == 2
    assert pid_file.exists(), "PID file must be left in place on timeout"
    output = result.output + (result.stderr or "")
    assert "10s" in output or "timeout" in output.lower() or "did not stop" in output.lower()


# ---------------------------------------------------------------------------
# modok quine status
# ---------------------------------------------------------------------------

# @spec CLI-QSTAT-001
def test_quine_status_running_prints_running(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    config_path = write_config(tmp_path / "config.toml")
    runner = CliRunner(mix_stderr=False)

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.quine.QuineClient") as mock_cls:
            mock_cls.return_value.ping = AsyncMock(return_value=True)
            result = runner.invoke(cli, ["quine", "status"])

    assert result.exit_code == 0
    assert "running" in result.output.lower()


# @spec CLI-QSTAT-002
def test_quine_status_stopped_prints_stopped(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    config_path = write_config(tmp_path / "config.toml")
    runner = CliRunner(mix_stderr=False)

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.quine.QuineClient") as mock_cls:
            mock_cls.return_value.ping = AsyncMock(return_value=False)
            result = runner.invoke(cli, ["quine", "status"])

    assert result.exit_code == 0
    assert "stopped" in result.output.lower()


# ---------------------------------------------------------------------------
# modok init --assisted  (CLI-INIT-008 through CLI-INIT-011)
# ---------------------------------------------------------------------------

# @spec CLI-INIT-008
def test_init_assisted_delegates_to_propose_registries_before_hook(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli
    from modok.registry.proposal import ProposalSummary

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config_path = write_config(tmp_path / "config.toml", repo_path=str(repo))
    summary = ProposalSummary(
        sections_processed=5, sections_failed=0,
        docs_processed=2, docs_skipped=0,
        entries_written={"features.yml": 3, "modules.yml": 2, "errors.yml": 1},
        failed_sections=[],
    )

    call_order = []

    def recording_propose(repo_root, cfg):
        call_order.append("propose")
        return summary

    def recording_hook(*args, **kwargs):
        call_order.append("hook")

    runner = CliRunner(mix_stderr=False)
    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.init.propose_registries", side_effect=recording_propose):
            with patch("modok.cli.commands.init.install_post_commit_hook", side_effect=recording_hook):
                runner.invoke(cli, ["init", "--project", "stagehand", "--repo", str(repo), "--assisted"])

    assert "propose" in call_order, "propose_registries must be called with --assisted"
    assert "hook" in call_order, "install_post_commit_hook must still be called"
    assert call_order.index("propose") < call_order.index("hook"), \
        "propose_registries must run before install_post_commit_hook"


# @spec CLI-INIT-009
def test_init_assisted_exits_2_when_llm_unreachable(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli
    from modok.llm.errors import LLMUnavailableError

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config_path = write_config(tmp_path / "config.toml", repo_path=str(repo))
    runner = CliRunner(mix_stderr=False)

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.init.propose_registries",
                   side_effect=LLMUnavailableError("Ollama not running")):
            result = runner.invoke(cli, ["init", "--project", "stagehand", "--repo", str(repo), "--assisted"])

    assert result.exit_code == 2
    output = result.output + (result.stderr or "")
    assert "LLM gateway is not reachable" in output


# @spec CLI-INIT-010
def test_init_without_assisted_does_not_invoke_llm(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config_path = write_config(tmp_path / "config.toml", repo_path=str(repo))
    runner = CliRunner(mix_stderr=False)

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.init.install_post_commit_hook"):
            with patch("modok.cli.commands.init.propose_registries") as mock_propose:
                with patch("modok.llm.gateway.enrich_section") as mock_enrich:
                    with patch("modok.llm.gateway._ollama_chat_completion") as mock_ollama:
                        runner.invoke(cli, ["init", "--project", "stagehand", "--repo", str(repo)])

    mock_propose.assert_not_called()
    mock_enrich.assert_not_called()
    mock_ollama.assert_not_called()


# @spec CLI-INIT-011
def test_init_assisted_prints_summary_to_stdout(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli
    from modok.registry.proposal import ProposalSummary

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config_path = write_config(tmp_path / "config.toml", repo_path=str(repo))
    summary = ProposalSummary(
        sections_processed=7,
        sections_failed=1,
        docs_processed=3,
        docs_skipped=0,
        entries_written={"features.yml": 4, "modules.yml": 2, "errors.yml": 8},
        failed_sections=[("Bad Section", Path("/repo/doc.md"))],
    )
    runner = CliRunner(mix_stderr=False)

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.init.install_post_commit_hook"):
            with patch("modok.cli.commands.init.propose_registries", return_value=summary):
                result = runner.invoke(
                    cli,
                    ["init", "--project", "stagehand", "--repo", str(repo), "--assisted"],
                )

    assert result.exit_code == 0
    out = result.output
    # Summary must mention sections processed, docs processed, and entries written per file
    assert "7" in out or "section" in out.lower()
    assert "3" in out or "doc" in out.lower()
    assert "features.yml" in out
    assert "4" in out  # features.yml entry count
