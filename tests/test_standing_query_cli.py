"""
Tests for `modok stream install|status|remove`.
All tests are written before implementation (Phase 5). Every test cites
the EARS spec it verifies via @spec annotation.

Specs verified: SQ-CLI-001, SQ-CLI-002, SQ-CLI-003, SQ-CLI-004, SQ-CLI-005.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from modok.cli.main import cli

MINIMAL_CONFIG = """
[quine]
url = "http://127.0.0.1:8080"
jar = "{jar_path}"

[[projects]]
slug = "stagehand"
repo = "{repo_path}"
"""


def write_config(path: Path, jar_path: str = "/fake/quine.jar", repo_path: str = "/fake/repo") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(MINIMAL_CONFIG.format(jar_path=jar_path, repo_path=repo_path))
    return path


# ---------------------------------------------------------------------------
# SQ-CLI-001 — install reports per-definition install status
# ---------------------------------------------------------------------------


# @spec SQ-CLI-001
def test_stream_install_reports_newly_installed(tmp_path):
    config_path = write_config(tmp_path / "config.toml")
    runner = CliRunner()

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.stream.QuineClient") as mock_cls:
            mock_cls.return_value.install_standing_query = AsyncMock(return_value=True)
            result = runner.invoke(cli, ["stream", "install"])

    assert result.exit_code == 0
    assert "actionable-issue-pattern" in result.output
    assert "installed" in result.output.lower()


# @spec SQ-CLI-001
def test_stream_install_reports_already_present(tmp_path):
    config_path = write_config(tmp_path / "config.toml")
    runner = CliRunner()

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.stream.QuineClient") as mock_cls:
            mock_cls.return_value.install_standing_query = AsyncMock(return_value=False)
            result = runner.invoke(cli, ["stream", "install"])

    assert result.exit_code == 0
    assert "already" in result.output.lower()


# ---------------------------------------------------------------------------
# SQ-CLI-002 — no --project option
# ---------------------------------------------------------------------------


# @spec SQ-CLI-002
def test_stream_install_has_no_project_option(tmp_path):
    config_path = write_config(tmp_path / "config.toml")
    runner = CliRunner()

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.stream.QuineClient") as mock_cls:
            mock_cls.return_value.install_standing_query = AsyncMock(return_value=True)
            result = runner.invoke(cli, ["stream", "install", "--project", "stagehand"])

    assert result.exit_code != 0  # unrecognized option


# @spec SQ-CLI-002
def test_stream_status_has_no_project_option(tmp_path):
    config_path = write_config(tmp_path / "config.toml")
    runner = CliRunner()

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.stream.QuineClient") as mock_cls:
            mock_cls.return_value.list_standing_queries = AsyncMock(return_value=[])
            result = runner.invoke(cli, ["stream", "status", "--project", "stagehand"])

    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# SQ-CLI-003 — status prints installed names
# ---------------------------------------------------------------------------


# @spec SQ-CLI-003
def test_stream_status_prints_installed_names(tmp_path):
    config_path = write_config(tmp_path / "config.toml")
    runner = CliRunner()

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.stream.QuineClient") as mock_cls:
            mock_cls.return_value.list_standing_queries = AsyncMock(
                return_value=["actionable-issue-pattern"]
            )
            result = runner.invoke(cli, ["stream", "status"])

    assert result.exit_code == 0
    assert "actionable-issue-pattern" in result.output


# @spec SQ-CLI-003
def test_stream_status_empty_when_none_installed(tmp_path):
    config_path = write_config(tmp_path / "config.toml")
    runner = CliRunner()

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.stream.QuineClient") as mock_cls:
            mock_cls.return_value.list_standing_queries = AsyncMock(return_value=[])
            result = runner.invoke(cli, ["stream", "status"])

    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# SQ-CLI-004 — remove reports per-definition removal status
# ---------------------------------------------------------------------------


# @spec SQ-CLI-004
def test_stream_remove_reports_removed(tmp_path):
    config_path = write_config(tmp_path / "config.toml")
    runner = CliRunner()

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.stream.QuineClient") as mock_cls:
            mock_cls.return_value.remove_standing_query = AsyncMock(return_value=True)
            result = runner.invoke(cli, ["stream", "remove"])

    assert result.exit_code == 0
    assert "removed" in result.output.lower()


# @spec SQ-CLI-004
def test_stream_remove_reports_not_present(tmp_path):
    config_path = write_config(tmp_path / "config.toml")
    runner = CliRunner()

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.stream.QuineClient") as mock_cls:
            mock_cls.return_value.remove_standing_query = AsyncMock(return_value=False)
            result = runner.invoke(cli, ["stream", "remove"])

    assert result.exit_code == 0
    assert "not" in result.output.lower()


# ---------------------------------------------------------------------------
# SQ-CLI-005 — Quine unreachable surfaces a clear, non-zero-exit error
# ---------------------------------------------------------------------------


# @spec SQ-CLI-005
def test_stream_install_quine_unreachable_exits_nonzero(tmp_path):
    config_path = write_config(tmp_path / "config.toml")
    runner = CliRunner()

    with patch("modok.cli.config.CONFIG_PATH", config_path):
        with patch("modok.cli.commands.stream.QuineClient") as mock_cls:
            mock_cls.return_value.install_standing_query = AsyncMock(
                side_effect=ConnectionError("refused")
            )
            result = runner.invoke(cli, ["stream", "install"])

    assert result.exit_code != 0
    assert "http://127.0.0.1:8080" in result.output
