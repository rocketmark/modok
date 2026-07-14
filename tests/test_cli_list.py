"""
Tests for `modok list` — lists valid feature/module slugs from the project's
registries. All tests are written before implementation (Phase 5). Every
test cites the EARS spec it verifies via @spec annotation.

Specs verified: CLI-LIST-001 through CLI-LIST-013.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import yaml
from click.testing import CliRunner

from modok.cli.main import cli

MINIMAL_CONFIG = """\
[quine]
url = "http://127.0.0.1:8080"
jar = "/fake/quine.jar"

[[projects]]
slug = "stagehand"
repo = "{repo_path}"
"""


def write_config(path: Path, repo_path: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(MINIMAL_CONFIG.format(repo_path=repo_path))
    return path


def write_registries(repo_root: Path, features: dict | None = None, modules: dict | None = None) -> None:
    reg_dir = repo_root / "registries"
    reg_dir.mkdir(parents=True, exist_ok=True)
    (reg_dir / "features.yml").write_text(yaml.dump({"features": features or {}}))
    (reg_dir / "modules.yml").write_text(yaml.dump({"modules": modules or {}}))


def _run(tmp_path, args, features=None, modules=None, write_regs=True):
    config_path = write_config(tmp_path / "config.toml", str(tmp_path))
    if write_regs:
        write_registries(tmp_path, features, modules)
    runner = CliRunner()
    with patch("modok.cli.config.CONFIG_PATH", config_path):
        return runner.invoke(cli, ["list", "--project", "stagehand", *args])


_FEATURES = {
    "wifi-provisioning": {"name": "WiFi Provisioning"},
    "client-ui": {"name": "Client UI"},
}
_MODULES = {
    "device-card": {"name": "Device Card"},
    "app": {"name": "App"},
}


# ---------------------------------------------------------------------------
# CLI-LIST-002/003/004/005 — flag narrowing
# ---------------------------------------------------------------------------


# @spec CLI-LIST-002
def test_list_default_shows_both_sections(tmp_path):
    result = _run(tmp_path, [], features=_FEATURES, modules=_MODULES)
    assert result.exit_code == 0
    assert "Features:" in result.output
    assert "Modules:" in result.output
    assert "wifi-provisioning" in result.output
    assert "device-card" in result.output


# @spec CLI-LIST-003
def test_list_features_only(tmp_path):
    result = _run(tmp_path, ["--features"], features=_FEATURES, modules=_MODULES)
    assert result.exit_code == 0
    assert "Features:" in result.output
    assert "Modules:" not in result.output
    assert "device-card" not in result.output


# @spec CLI-LIST-004
def test_list_modules_only(tmp_path):
    result = _run(tmp_path, ["--modules"], features=_FEATURES, modules=_MODULES)
    assert result.exit_code == 0
    assert "Modules:" in result.output
    assert "Features:" not in result.output
    assert "wifi-provisioning" not in result.output


# @spec CLI-LIST-005
def test_list_both_flags_same_as_neither(tmp_path):
    result = _run(tmp_path, ["--features", "--modules"], features=_FEATURES, modules=_MODULES)
    assert result.exit_code == 0
    assert "Features:" in result.output
    assert "Modules:" in result.output


# ---------------------------------------------------------------------------
# CLI-LIST-006/007 — sorting and tabular format
# ---------------------------------------------------------------------------


# @spec CLI-LIST-006
def test_list_entries_sorted_alphabetically_by_slug(tmp_path):
    result = _run(tmp_path, ["--features"], features=_FEATURES)
    lines = [
        line for line in result.output.splitlines() if line.strip() and "Features:" not in line
    ]
    slugs_in_order = [line.strip().split()[0] for line in lines]
    assert slugs_in_order == ["client-ui", "wifi-provisioning"]


# @spec CLI-LIST-007
def test_list_tabular_format_shows_slug_and_name(tmp_path):
    result = _run(tmp_path, ["--features"], features=_FEATURES)
    assert "wifi-provisioning" in result.output
    assert "WiFi Provisioning" in result.output


# ---------------------------------------------------------------------------
# CLI-LIST-008/009 — empty vs. omitted section
# ---------------------------------------------------------------------------


# @spec CLI-LIST-008
def test_list_requested_empty_section_prints_none(tmp_path):
    result = _run(tmp_path, ["--modules"], features=_FEATURES, modules={})
    assert result.exit_code == 0
    assert "Modules:" in result.output
    assert "(none)" in result.output


# @spec CLI-LIST-009
def test_list_unrequested_section_omitted_entirely(tmp_path):
    result = _run(tmp_path, ["--features"], features=_FEATURES, modules=_MODULES)
    assert "Modules:" not in result.output


# ---------------------------------------------------------------------------
# CLI-LIST-010 — JSON shape
# ---------------------------------------------------------------------------


# @spec CLI-LIST-010
def test_list_json_includes_only_requested_keys(tmp_path):
    result = _run(tmp_path, ["--features", "--json"], features=_FEATURES, modules=_MODULES)
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert "features" in parsed
    assert "modules" not in parsed
    assert {"slug": "client-ui", "name": "Client UI"} in parsed["features"]


# @spec CLI-LIST-010
def test_list_json_empty_requested_section_is_empty_list(tmp_path):
    result = _run(tmp_path, ["--modules", "--json"], features=_FEATURES, modules={})
    parsed = json.loads(result.output)
    assert parsed["modules"] == []
    assert "features" not in parsed


# @spec CLI-LIST-010
def test_list_json_both_sections_when_no_flags(tmp_path):
    result = _run(tmp_path, ["--json"], features=_FEATURES, modules=_MODULES)
    parsed = json.loads(result.output)
    assert "features" in parsed
    assert "modules" in parsed
    assert parsed["project"] == "stagehand"


# ---------------------------------------------------------------------------
# CLI-LIST-001 — no Quine dependency
# ---------------------------------------------------------------------------


# @spec CLI-LIST-001
def test_list_module_does_not_import_quine_client():
    import modok.cli.commands.list as list_module

    assert not hasattr(list_module, "QuineClient")


# @spec CLI-LIST-001
def test_list_succeeds_with_no_quine_mock_configured(tmp_path):
    # No QuineClient patch at all, no reachable Quine — if the command tried
    # to ping Quine for real this would fail rather than succeed cleanly.
    result = _run(tmp_path, [], features=_FEATURES, modules=_MODULES)
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# CLI-LIST-011/012/013 — error paths and success
# ---------------------------------------------------------------------------


# @spec CLI-LIST-011
def test_list_project_not_in_config_exits_1(tmp_path):
    config_path = write_config(tmp_path / "config.toml", str(tmp_path))
    write_registries(tmp_path, _FEATURES, _MODULES)
    runner = CliRunner()
    with patch("modok.cli.config.CONFIG_PATH", config_path):
        result = runner.invoke(cli, ["list", "--project", "no-such-project"])
    assert result.exit_code == 1


# @spec CLI-LIST-012
def test_list_missing_registries_exits_1(tmp_path):
    # write_regs=False — no registries/ directory created at all
    result = _run(tmp_path, [], write_regs=False)
    assert result.exit_code == 1


# @spec CLI-LIST-013
def test_list_zero_entries_exits_0(tmp_path):
    result = _run(tmp_path, [], features={}, modules={})
    assert result.exit_code == 0
