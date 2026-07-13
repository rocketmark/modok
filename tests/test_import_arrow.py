"""
Tests for modok.import_arrow — registry bootstrap from arrow docs.
All tests written before implementation (Phase 5). Tests cite specs via @spec annotation.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from hypothesis import assume, given, settings
import hypothesis.strategies as st


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
    return path


def make_code_map(files: list[dict]) -> dict:
    return {"project": "test-project", "files": files}


def make_code_map_entry(
    path: str,
    language: str = "python",
    role: str = "source",
    symbols: list[dict] | None = None,
) -> dict:
    entry = {"path": path, "language": language, "role": role}
    if symbols is not None:
        entry["symbols"] = symbols
    return entry


def make_repo(tmp_path: Path) -> Path:
    """
    Repo layout:
      docs/arrows/index.yaml
      docs/arrows/widget-core.md
      docs/arrows/util-helper.md
      docs/specs/widget-core-specs.md
      client/widget/renderer.py
      client/widget/state.py
      client/util/helper.py
      agent/src/agent.c
      agent/src/shtp.c
      .modok/code-map.yml
    """
    # index.yaml
    write(
        tmp_path / "docs/arrows/index.yaml",
        """\
        arrows:
          - id: widget-core
            description: "Core widget rendering — layout, state, events"
            specs: docs/specs/widget-core-specs.md
            arrow_doc: docs/arrows/widget-core.md
            tests:
              - client/tests/test_widget.py
              - client/tests/test_widget_state.py (not yet written)
              - spec/widget.cfg
          - id: util-helper
            description: "Utility helper functions"
            specs: docs/specs/util-helper-specs.md
            arrow_doc: docs/arrows/util-helper.md
            tests:
              - client/tests/test_helper.py
    """,
    )

    # widget-core arrow doc
    write(
        tmp_path / "docs/arrows/widget-core.md",
        """\
        # Arrow: widget-core

        ## References

        ### Code
        - `client/widget/renderer.py` — `WidgetRenderer`, `render()`
        - `client/widget/state.py` — `WidgetState`

        ### Key Components
        1. `WidgetRenderer` — renders the widget to the screen
        2. `WidgetState` — tracks widget state changes
    """,
    )

    # util-helper arrow doc
    write(
        tmp_path / "docs/arrows/util-helper.md",
        """\
        # Arrow: util-helper

        ## References

        ### Code
        - `client/util/helper.py` — `HelperUtils`

        ### Key Components
        1. `HelperUtils` — shared utility functions
    """,
    )

    # code map
    code_map = make_code_map(
        [
            make_code_map_entry(
                "client/widget/renderer.py",
                symbols=[
                    {"name": "WidgetRenderer", "kind": "class", "line_start": 1},
                    {"name": "render", "kind": "function", "line_start": 10},
                ],
            ),
            make_code_map_entry(
                "client/widget/state.py",
                symbols=[{"name": "WidgetState", "kind": "class", "line_start": 1}],
            ),
            make_code_map_entry(
                "client/util/helper.py",
                symbols=[{"name": "HelperUtils", "kind": "class", "line_start": 1}],
            ),
            make_code_map_entry("agent/src/agent.c", language="cpp"),
            make_code_map_entry("agent/src/shtp.c", language="cpp"),
        ]
    )
    write(
        tmp_path / ".modok/code-map.yml",
        yaml.dump(code_map, default_flow_style=False),
    )

    # placeholder source files (content doesn't matter for extractor logic)
    write(tmp_path / "client/widget/renderer.py", "class WidgetRenderer: pass\n")
    write(tmp_path / "client/widget/state.py", "class WidgetState: pass\n")
    write(tmp_path / "client/util/helper.py", "class HelperUtils: pass\n")
    write(tmp_path / "agent/src/agent.c", "")
    write(tmp_path / "agent/src/shtp.c", "")

    return tmp_path


# ---------------------------------------------------------------------------
# IA-CMD — Command interface
# ---------------------------------------------------------------------------


# @spec IA-CMD-001
def test_import_arrow_command_exists():
    from modok.cli.main import cli

    assert "import-arrow" in [cmd.name for cmd in cli.commands.values()]


# @spec IA-CMD-001
def test_import_arrow_command_accepts_flags(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    make_repo(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["import-arrow", "--project", "x", "--repo", str(tmp_path), "--dry-run", "--no-llm"],
    )
    assert result.exit_code == 0


# @spec IA-CMD-002
def test_import_arrow_missing_project_in_config_exits_nonzero(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    runner = CliRunner()
    with patch("modok.cli.config.ModokConfig.load") as mock_load:
        mock_cfg = MagicMock()
        mock_cfg.project.side_effect = KeyError("no-such-project")
        mock_load.return_value = mock_cfg
        result = runner.invoke(cli, ["import-arrow", "--project", "no-such-project"])
    assert result.exit_code != 0


# @spec IA-CMD-003
def test_import_arrow_missing_index_yaml_exits_nonzero(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    runner = CliRunner()
    result = runner.invoke(cli, ["import-arrow", "--project", "x", "--repo", str(tmp_path)])
    assert result.exit_code != 0
    assert "index.yaml not found" in result.output.lower()


# @spec IA-FEAT-009
def test_import_arrow_null_valued_required_field_exits_nonzero(tmp_path):
    # A required field can be present as a YAML key with a null value (e.g.
    # an arrow not yet audited: "arrow_doc: null") — `field not in entry` is
    # not sufficient; the value itself must be checked.
    from click.testing import CliRunner
    from modok.cli.main import cli

    write(
        tmp_path / "docs/arrows/index.yaml",
        """\
        arrows:
          - id: unaudited-feature
            description: "Not yet audited"
            specs: docs/specs/unaudited-specs.md
            arrow_doc: null
            tests: []
    """,
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["import-arrow", "--project", "x", "--repo", str(tmp_path)])
    assert result.exit_code != 0
    assert "arrow_doc" in result.output
    assert "unaudited-feature" in result.output


# @spec IA-CMD-004
def test_import_arrow_empty_index_exits_zero_no_files(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    write(tmp_path / "docs/arrows/index.yaml", "arrows: []\n")
    write(tmp_path / ".modok/code-map.yml", yaml.dump(make_code_map([])))
    runner = CliRunner()
    result = runner.invoke(cli, ["import-arrow", "--project", "x", "--repo", str(tmp_path)])
    assert result.exit_code == 0
    assert "nothing to import" in result.output.lower()
    assert not (tmp_path / "registries" / "features.yml").exists()
    assert not (tmp_path / "registries" / "modules.yml").exists()


# @spec IA-CMD-004
def test_import_arrow_empty_index_does_not_clobber_existing_registry(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    write(tmp_path / "docs/arrows/index.yaml", "arrows: []\n")
    write(tmp_path / ".modok/code-map.yml", yaml.dump(make_code_map([])))
    existing_content = "features:\n  old-feature:\n    name: Old Feature\n"
    write(tmp_path / "registries/features.yml", existing_content)
    runner = CliRunner()
    runner.invoke(cli, ["import-arrow", "--project", "x", "--repo", str(tmp_path)])
    assert (tmp_path / "registries/features.yml").read_text() == existing_content


# @spec IA-CMD-005
def test_import_arrow_success_prints_summary(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    make_repo(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["import-arrow", "--project", "x", "--repo", str(tmp_path), "--no-llm"]
    )
    assert result.exit_code == 0
    assert "imported" in result.output.lower()
    assert "features" in result.output.lower()
    assert "modules" in result.output.lower()


# @spec IA-CMD-006
def test_import_arrow_dry_run_prints_yaml_no_files(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    make_repo(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["import-arrow", "--project", "x", "--repo", str(tmp_path), "--dry-run"]
    )
    assert result.exit_code == 0
    assert not (tmp_path / "registries" / "features.yml").exists()
    assert not (tmp_path / "registries" / "modules.yml").exists()
    assert "features:" in result.output
    assert "modules:" in result.output


# @spec IA-CMD-007
def test_import_arrow_no_llm_skips_llm_passes(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    make_repo(tmp_path)
    runner = CliRunner()
    with patch("modok.import_arrow.llm.run_name_description_pass") as mock_llm:
        result = runner.invoke(
            cli, ["import-arrow", "--project", "x", "--repo", str(tmp_path), "--no-llm"]
        )
    mock_llm.assert_not_called()
    assert result.exit_code == 0


# @spec IA-CMD-008
def test_import_arrow_conflict_exits_nonzero_no_output(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    make_repo(tmp_path)
    runner = CliRunner()

    # Inject a conflict at the dedup boundary: find_duplicate_pairs returns a pair,
    # run_dedup_pass judges them as different concepts → CONFLICT.
    # This tests CLI behavior (exit non-zero, no files written), not dedup logic itself.
    def conflict_dedup(pairs, modules, llm_call=None):
        msg = f"{pairs[0][0]} vs {pairs[0][1]} — same files, different concepts. Human review required."
        return modules, [msg]

    with patch(
        "modok.import_arrow.dedup.find_duplicate_pairs", return_value=[("renderer", "renderer-b")]
    ):
        with patch("modok.import_arrow.llm.run_dedup_pass", side_effect=conflict_dedup):
            result = runner.invoke(cli, ["import-arrow", "--project", "x", "--repo", str(tmp_path)])

    assert result.exit_code != 0
    assert "conflict" in result.output.lower()
    assert not (tmp_path / "registries").exists()


# ---------------------------------------------------------------------------
# IA-FEAT — Feature extraction
# ---------------------------------------------------------------------------


# @spec IA-FEAT-001
def test_feature_extraction_produces_one_entry_per_arrow(tmp_path):
    from modok.import_arrow.extractor import extract_features

    index = [
        {"id": "feat-a", "description": "A", "specs": "s.md", "arrow_doc": "a.md", "tests": []},
        {"id": "feat-b", "description": "B", "specs": "s.md", "arrow_doc": "b.md", "tests": []},
    ]
    write(tmp_path / "a.md", "### Code\n")
    write(tmp_path / "b.md", "### Code\n")
    features = extract_features(index, repo_root=tmp_path)
    assert "feat-a" in features
    assert "feat-b" in features
    assert len(features) == 2


# @spec IA-FEAT-002
@given(
    st.lists(
        st.from_regex(r"[a-z][a-z0-9]*", fullmatch=True),
        min_size=1,
        max_size=6,
    )
)
def test_property_slug_to_name_title_cases_all_words(words):
    from modok.import_arrow.extractor import slug_to_name

    slug = "-".join(words)
    result = slug_to_name(slug)
    assert result == " ".join(w.capitalize() for w in words)


# @spec IA-FEAT-003
def test_feature_description_verbatim(tmp_path):
    from modok.import_arrow.extractor import extract_features

    desc = "Core widget rendering — layout, state, events"
    index = [
        {
            "id": "widget-core",
            "description": desc,
            "specs": "s.md",
            "arrow_doc": "a.md",
            "tests": [],
        }
    ]
    write(tmp_path / "a.md", "### Code\n")
    features = extract_features(index, repo_root=tmp_path)
    assert features["widget-core"]["description"] == desc


# @spec IA-FEAT-004
def test_feature_specs_verbatim(tmp_path):
    from modok.import_arrow.extractor import extract_features

    specs_path = "docs/specs/widget-core-specs.md"
    index = [
        {
            "id": "widget-core",
            "description": "X",
            "specs": specs_path,
            "arrow_doc": "a.md",
            "tests": [],
        }
    ]
    write(tmp_path / "a.md", "### Code\n")
    features = extract_features(index, repo_root=tmp_path)
    assert features["widget-core"]["specs"] == specs_path


# @spec IA-FEAT-005
def test_feature_test_files_filtering(tmp_path):
    from modok.import_arrow.extractor import extract_features

    index = [
        {
            "id": "feat-a",
            "description": "X",
            "specs": "s.md",
            "arrow_doc": "a.md",
            "tests": [
                "client/tests/test_widget.py",
                "client/tests/test_widget_state.py (not yet written)",
                "spec/widget.cfg",
                "just-a-label",
            ],
        }
    ]
    write(tmp_path / "a.md", "### Code\n")
    features = extract_features(index, repo_root=tmp_path)
    test_files = features["feat-a"]["test_files"]
    assert "client/tests/test_widget.py" in test_files
    assert "client/tests/test_widget_state.py" in test_files
    assert not any(f.startswith("spec/") for f in test_files)
    assert not any("(" in f for f in test_files)
    assert "just-a-label" not in test_files


# @spec IA-FEAT-010
def test_feature_test_files_skips_non_string_entry(tmp_path, capsys):
    from modok.import_arrow.extractor import extract_features

    index = [
        {
            "id": "feat-a",
            "description": "X",
            "specs": "s.md",
            "arrow_doc": "a.md",
            "tests": [
                "client/tests/test_widget.py",
                # An unquoted "tests:" list item containing a colon, e.g.
                # "foo.c (label: detail)", parses as a single-key mapping
                # rather than a scalar string — must not crash extraction.
                {"agent/src/foo.c (C property tests": "match, normalize)"},
            ],
        }
    ]
    write(tmp_path / "a.md", "### Code\n")
    features = extract_features(index, repo_root=tmp_path)
    test_files = features["feat-a"]["test_files"]
    assert test_files == ["client/tests/test_widget.py"]
    assert "skipping" in capsys.readouterr().err.lower()


# @spec IA-FEAT-006
def test_feature_source_files_from_code_section(tmp_path):
    from modok.import_arrow.extractor import extract_source_files_from_doc

    doc_content = textwrap.dedent("""\
        # Arrow: widget-core

        ### Code
        - `client/widget/renderer.py` — `WidgetRenderer`, `render()`
        - `client/widget/state.py` — `WidgetState`
    """)
    paths = extract_source_files_from_doc(doc_content)
    assert "client/widget/renderer.py" in paths
    assert "client/widget/state.py" in paths
    assert "WidgetRenderer" not in paths
    assert "render()" not in paths


# @spec IA-FEAT-006
@pytest.mark.parametrize(
    "sep,path",
    [
        (" — ", "src/em.py"),  # U+2014 em-dash (used in stagehand arrow docs)
        (" – ", "src/en.py"),  # U+2013 en-dash
        (" - ", "src/hyph.py"),  # spaced hyphen
    ],
)
def test_feature_source_files_em_dash_variants(sep, path):
    from modok.import_arrow.extractor import extract_source_files_from_doc

    doc = f"### Code\n- `{path}`{sep}`Symbol`\n"
    assert path in extract_source_files_from_doc(doc)


# @spec IA-FEAT-006
def test_feature_source_files_multiple_per_line():
    from modok.import_arrow.extractor import extract_source_files_from_doc

    doc = "### Code\n- `src/a.py`, `src/b.py` — description\n"
    paths = extract_source_files_from_doc(doc)
    assert "src/a.py" in paths
    assert "src/b.py" in paths


# @spec IA-FEAT-007
def test_feature_no_code_section_empty_source_files_warns(tmp_path, capsys):
    from modok.import_arrow.extractor import extract_features

    index = [
        {"id": "feat-a", "description": "X", "specs": "s.md", "arrow_doc": "a.md", "tests": []}
    ]
    write(tmp_path / "a.md", "# Arrow: feat-a\n\nNo code section here.\n")
    features = extract_features(index, repo_root=tmp_path)
    assert features["feat-a"]["source_files"] == []
    captured = capsys.readouterr()
    assert "warn" in captured.err.lower()


# @spec IA-FEAT-008
def test_feature_source_file_not_in_code_map_warns(tmp_path, capsys):
    from modok.import_arrow.extractor import validate_source_files

    code_map = make_code_map(
        [
            make_code_map_entry("client/widget/renderer.py"),
        ]
    )
    warnings = []
    validate_source_files(
        slug="feat-a",
        source_files=["client/widget/renderer.py", "client/widget/missing.py"],
        code_map=code_map,
        warn=warnings.append,
    )
    assert any("missing.py" in w for w in warnings)


# @spec IA-FEAT-008
def test_feature_source_file_ignored_in_code_map_warns(tmp_path, capsys):
    from modok.import_arrow.extractor import validate_source_files

    code_map = make_code_map(
        [
            make_code_map_entry("client/widget/renderer.py", role="ignored"),
        ]
    )
    warnings = []
    validate_source_files(
        slug="feat-a",
        source_files=["client/widget/renderer.py"],
        code_map=code_map,
        warn=warnings.append,
    )
    assert any("ignored" in w for w in warnings)


# @spec IA-FEAT-009
def test_feature_missing_required_field_exits_nonzero(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    # index entry missing `description`
    write(
        tmp_path / "docs/arrows/index.yaml",
        textwrap.dedent("""\
        arrows:
          - id: feat-a
            specs: docs/specs/s.md
            arrow_doc: docs/arrows/a.md
            tests: []
    """),
    )
    write(tmp_path / ".modok/code-map.yml", yaml.dump(make_code_map([])))
    runner = CliRunner()
    result = runner.invoke(
        cli, ["import-arrow", "--project", "x", "--repo", str(tmp_path), "--no-llm"]
    )
    assert result.exit_code != 0
    assert "description" in result.output.lower()


# ---------------------------------------------------------------------------
# IA-MOD — Module extraction Pass 1 (mechanical)
# ---------------------------------------------------------------------------


# @spec IA-MOD-001
def test_module_pass1_python_file_per_module():
    from modok.import_arrow.modules import build_modules_pass1

    features = {
        "feat-a": {
            "source_files": ["client/widget/renderer.py", "client/widget/state.py"],
        }
    }
    code_map = make_code_map(
        [
            make_code_map_entry(
                "client/widget/renderer.py",
                symbols=[{"name": "WidgetRenderer", "kind": "class", "line_start": 1}],
            ),
            make_code_map_entry(
                "client/widget/state.py",
                symbols=[{"name": "WidgetState", "kind": "class", "line_start": 1}],
            ),
        ]
    )
    modules = build_modules_pass1(features, code_map)
    assert "renderer" in modules
    assert "state" in modules


# @spec IA-MOD-001
def test_module_pass1_slug_from_stem_underscores_to_hyphens():
    from modok.import_arrow.modules import build_modules_pass1

    features = {"feat-a": {"source_files": ["client/shtp_receiver.py"]}}
    code_map = make_code_map([make_code_map_entry("client/shtp_receiver.py")])
    modules = build_modules_pass1(features, code_map)
    assert "shtp-receiver" in modules


# @spec IA-MOD-002
def test_module_pass1_cpp_files_same_parent_become_directory_module():
    from modok.import_arrow.modules import build_modules_pass1

    features = {
        "pi-agent": {
            "source_files": ["agent/src/agent.c", "agent/src/shtp.c", "agent/src/recovery.c"],
        }
    }
    code_map = make_code_map(
        [
            make_code_map_entry("agent/src/agent.c", language="cpp"),
            make_code_map_entry("agent/src/shtp.c", language="cpp"),
            make_code_map_entry("agent/src/recovery.c", language="cpp"),
        ]
    )
    modules = build_modules_pass1(features, code_map)
    # Should produce one directory-level module, not three file-level modules
    dir_modules = [s for s, e in modules.items() if e.get("source_roots")]
    file_modules = [
        s for s, e in modules.items() if "agent/src/agent.c" in e.get("source_files", [])
    ]
    assert len(dir_modules) == 1
    assert len(file_modules) == 0


# @spec IA-MOD-002
def test_module_pass1_cpp_dir_module_has_source_roots():
    from modok.import_arrow.modules import build_modules_pass1

    features = {"pi-agent": {"source_files": ["agent/src/agent.c", "agent/src/shtp.c"]}}
    code_map = make_code_map(
        [
            make_code_map_entry("agent/src/agent.c", language="cpp"),
            make_code_map_entry("agent/src/shtp.c", language="cpp"),
        ]
    )
    modules = build_modules_pass1(features, code_map)
    dir_mod = next((e for e in modules.values() if e.get("source_roots")), None)
    assert dir_mod is not None
    assert "agent/src" in dir_mod["source_roots"]
    assert dir_mod.get("source_files") == []


# @spec IA-MOD-003
def test_module_pass1_entry_fields():
    from modok.import_arrow.modules import build_modules_pass1

    features = {"feat-a": {"source_files": ["client/widget/renderer.py"]}}
    code_map = make_code_map(
        [
            make_code_map_entry(
                "client/widget/renderer.py",
                symbols=[{"name": "WidgetRenderer", "kind": "class", "line_start": 1}],
            )
        ]
    )
    modules = build_modules_pass1(features, code_map)
    entry = modules.get("renderer")
    assert entry is not None
    assert entry["language"] == "python"
    assert entry["primary_class"] == "WidgetRenderer"
    assert entry["name"] == "Renderer"
    assert entry["description"] == ""


# @spec IA-MOD-003b
def test_module_pass1_file_absent_from_code_map_still_produces_module(capsys):
    from modok.import_arrow.modules import build_modules_pass1

    features = {"feat-a": {"source_files": ["client/missing_file.py"]}}
    code_map = make_code_map([])
    modules = build_modules_pass1(features, code_map)
    entry = modules.get("missing-file")
    assert entry is not None
    assert entry["language"] == "unknown"
    assert entry["primary_class"] == ""
    captured = capsys.readouterr()
    assert "warn" in captured.err.lower() or "missing" in captured.err.lower()


# @spec IA-MOD-004
def test_module_pass1_skips_non_source_roles():
    from modok.import_arrow.modules import build_modules_pass1

    features = {
        "feat-a": {
            "source_files": [
                "config.toml",
                "docs/overview.md",
                "generated/auto.py",
                "vendor/ignored.py",
            ]
        }
    }
    code_map = make_code_map(
        [
            make_code_map_entry("config.toml", language="unknown", role="config"),
            make_code_map_entry("docs/overview.md", language="unknown", role="docs"),
            make_code_map_entry("generated/auto.py", language="python", role="generated"),
            make_code_map_entry("vendor/ignored.py", language="python", role="ignored"),
        ]
    )
    modules = build_modules_pass1(features, code_map)
    assert len(modules) == 0


# @spec IA-MOD-005
def test_module_pass1_slug_disambiguation_different_dirs():
    from modok.import_arrow.modules import build_modules_pass1

    features = {
        "feat-a": {
            "source_files": ["src/engine.py", "lib/engine.py"],
        }
    }
    code_map = make_code_map(
        [
            make_code_map_entry("src/engine.py"),
            make_code_map_entry("lib/engine.py"),
        ]
    )
    modules = build_modules_pass1(features, code_map)
    slugs = list(modules.keys())
    assert "src-engine" in slugs
    assert "lib-engine" in slugs
    assert "engine" not in slugs


# ---------------------------------------------------------------------------
# IA-OVER — Key Components overlay (Pass 2)
# ---------------------------------------------------------------------------


# @spec IA-OVER-001
def test_overlay_extracts_class_and_description():
    from modok.import_arrow.modules import parse_key_components

    section = textwrap.dedent("""\
        1. `WidgetRenderer` — renders the widget to the screen
        2. `WidgetState` — tracks widget state changes
    """)
    components = parse_key_components(section)
    assert components[0]["class_name"] == "WidgetRenderer"
    assert components[0]["description"] == "renders the widget to the screen"
    assert components[1]["class_name"] == "WidgetState"
    assert components[1]["description"] == "tracks widget state changes"


# @spec IA-OVER-002
def test_overlay_updates_module_description_when_symbol_found():
    from modok.import_arrow.modules import apply_key_components_overlay

    modules = {
        "renderer": {
            "source_files": ["client/widget/renderer.py"],
            "description": "",
            "primary_class": "WidgetRenderer",
            "language": "python",
            "name": "Renderer",
            "source_roots": [],
        }
    }
    code_map = make_code_map(
        [
            make_code_map_entry(
                "client/widget/renderer.py",
                symbols=[{"name": "WidgetRenderer", "kind": "class", "line_start": 1}],
            )
        ]
    )
    arrow_key_components = {
        "widget-core": [
            {"class_name": "WidgetRenderer", "description": "renders the widget to the screen"}
        ]
    }
    updated = apply_key_components_overlay(modules, arrow_key_components, code_map)
    assert updated["renderer"]["description"] == "renders the widget to the screen"


# @spec IA-OVER-003
def test_overlay_warns_and_skips_when_symbol_in_multiple_files(capsys):
    from modok.import_arrow.modules import apply_key_components_overlay

    modules = {
        "renderer-a": {
            "source_files": ["client/a/renderer.py"],
            "description": "",
            "primary_class": "Renderer",
            "language": "python",
            "name": "Renderer A",
            "source_roots": [],
        },
        "renderer-b": {
            "source_files": ["client/b/renderer.py"],
            "description": "",
            "primary_class": "Renderer",
            "language": "python",
            "name": "Renderer B",
            "source_roots": [],
        },
    }
    code_map = make_code_map(
        [
            make_code_map_entry(
                "client/a/renderer.py",
                symbols=[{"name": "Renderer", "kind": "class", "line_start": 1}],
            ),
            make_code_map_entry(
                "client/b/renderer.py",
                symbols=[{"name": "Renderer", "kind": "class", "line_start": 1}],
            ),
        ]
    )
    arrow_key_components = {"feat-a": [{"class_name": "Renderer", "description": "the renderer"}]}
    updated = apply_key_components_overlay(modules, arrow_key_components, code_map)
    # Descriptions should remain empty (overlay skipped)
    assert updated["renderer-a"]["description"] == ""
    assert updated["renderer-b"]["description"] == ""
    captured = capsys.readouterr()
    assert "ambiguous" in captured.err.lower() or "warn" in captured.err.lower()


# @spec IA-OVER-004
def test_overlay_warns_and_skips_when_symbol_in_multiple_arrow_docs(capsys):
    from modok.import_arrow.modules import apply_key_components_overlay

    modules = {
        "renderer": {
            "source_files": ["client/renderer.py"],
            "description": "",
            "primary_class": "Renderer",
            "language": "python",
            "name": "Renderer",
            "source_roots": [],
        },
    }
    code_map = make_code_map(
        [
            make_code_map_entry(
                "client/renderer.py",
                symbols=[{"name": "Renderer", "kind": "class", "line_start": 1}],
            ),
        ]
    )
    # Same class in two arrow docs
    arrow_key_components = {
        "feat-a": [{"class_name": "Renderer", "description": "description A"}],
        "feat-b": [{"class_name": "Renderer", "description": "description B"}],
    }
    updated = apply_key_components_overlay(modules, arrow_key_components, code_map)
    assert updated["renderer"]["description"] == ""
    captured = capsys.readouterr()
    assert "ambiguous" in captured.err.lower() or "warn" in captured.err.lower()


# @spec IA-OVER-005
def test_overlay_warns_and_skips_when_symbol_not_in_code_map(capsys):
    from modok.import_arrow.modules import apply_key_components_overlay

    modules = {
        "renderer": {
            "source_files": ["client/renderer.py"],
            "description": "",
            "primary_class": "",
            "language": "python",
            "name": "Renderer",
            "source_roots": [],
        },
    }
    code_map = make_code_map(
        [
            make_code_map_entry("client/renderer.py", symbols=[]),
        ]
    )
    arrow_key_components = {
        "feat-a": [{"class_name": "NonExistentClass", "description": "some description"}]
    }
    updated = apply_key_components_overlay(modules, arrow_key_components, code_map)
    assert updated["renderer"]["description"] == ""
    captured = capsys.readouterr()
    assert "warn" in captured.err.lower() or "not in code map" in captured.err.lower()


# @spec IA-OVER-006
def test_overlay_no_key_components_section_no_warning(tmp_path, capsys):
    from modok.import_arrow.extractor import extract_features
    from modok.import_arrow.modules import build_modules_pass1, apply_key_components_overlay

    doc = "# Arrow\n\n### Code\n- `client/widget.py` — `Widget`\n"
    write(tmp_path / "a.md", doc)
    index = [
        {"id": "feat-a", "description": "X", "specs": "s.md", "arrow_doc": "a.md", "tests": []}
    ]
    features = extract_features(index, repo_root=tmp_path)
    code_map = make_code_map([make_code_map_entry("client/widget.py")])
    modules = build_modules_pass1(features, code_map)
    _ = capsys.readouterr()  # clear
    apply_key_components_overlay(modules, {}, code_map)
    captured = capsys.readouterr()
    # No warning about missing Key Components
    assert "key components" not in captured.err.lower()


# ---------------------------------------------------------------------------
# IA-DEDUP — Deduplication
# ---------------------------------------------------------------------------


# @spec IA-DEDUP-001
def test_dedup_builds_inverted_index():
    from modok.import_arrow.dedup import build_inverted_index

    modules = {
        "renderer": {"source_files": ["client/renderer.py"], "source_roots": []},
        "widget": {"source_files": ["client/renderer.py", "client/state.py"], "source_roots": []},
    }
    index = build_inverted_index(modules)
    assert "renderer" in index["client/renderer.py"]
    assert "widget" in index["client/renderer.py"]
    assert "widget" in index["client/state.py"]


# @spec IA-DEDUP-002
@given(
    slug_a=st.from_regex(r"[a-z][a-z-]*[a-z]", fullmatch=True),
    slug_b=st.from_regex(r"[a-z][a-z-]*[a-z]", fullmatch=True),
    files=st.lists(
        st.from_regex(r"[a-z]+/[a-z_]+\.[a-z]+", fullmatch=True),
        min_size=1,
        max_size=4,
        unique=True,
    ),
)
@settings(max_examples=60)
def test_property_dedup_flags_any_identical_frozensets(slug_a, slug_b, files):
    assume(slug_a != slug_b)
    from modok.import_arrow.dedup import find_duplicate_pairs

    modules = {
        slug_a: {"source_files": list(files), "source_roots": []},
        slug_b: {"source_files": list(files), "source_roots": []},
        "unrelated-z": {"source_files": ["other/unrelated.py"], "source_roots": []},
    }
    pairs = find_duplicate_pairs(modules)
    pair_sets = [frozenset(p) for p in pairs]
    assert frozenset({slug_a, slug_b}) in pair_sets


# @spec IA-DEDUP-002
def test_dedup_three_way_duplicate_flags_all_pairs():
    from modok.import_arrow.dedup import find_duplicate_pairs

    modules = {
        "mod-a": {"source_files": ["client/shared.py"], "source_roots": []},
        "mod-b": {"source_files": ["client/shared.py"], "source_roots": []},
        "mod-c": {"source_files": ["client/shared.py"], "source_roots": []},
    }
    pairs = find_duplicate_pairs(modules)
    pair_sets = [frozenset(p) for p in pairs]
    assert frozenset({"mod-a", "mod-b"}) in pair_sets
    assert frozenset({"mod-a", "mod-c"}) in pair_sets
    assert frozenset({"mod-b", "mod-c"}) in pair_sets
    assert len(pairs) == 3


# @spec IA-DEDUP-003
def test_dedup_warns_on_strict_subset(capsys):
    from modok.import_arrow.dedup import check_subset_pairs

    modules = {
        "module-a": {"source_files": ["client/a.py"], "source_roots": []},
        "module-b": {"source_files": ["client/a.py", "client/b.py"], "source_roots": []},
    }
    check_subset_pairs(modules)
    captured = capsys.readouterr()
    assert "warn" in captured.err.lower() or "subset" in captured.err.lower()


# @spec IA-DEDUP-004
def test_dedup_warns_on_shared_file_non_duplicate(capsys):
    from modok.import_arrow.dedup import check_shared_files

    modules = {
        "module-a": {"source_files": ["client/shared.py"], "source_roots": []},
        "module-b": {"source_files": ["client/shared.py", "client/other.py"], "source_roots": []},
    }
    check_shared_files(modules)
    captured = capsys.readouterr()
    assert "warn" in captured.err.lower() or "shared.py" in captured.err.lower()


# ---------------------------------------------------------------------------
# IA-LLM — LLM passes
# ---------------------------------------------------------------------------


# @spec IA-LLM-001
def test_llm_pass1_skips_self_evident_slugs_with_description():
    from modok.import_arrow.llm import filter_modules_for_llm_pass

    modules = {
        "fbx-writer": {
            "slug": "fbx-writer",
            "primary_class": "write_fbx",
            "description": "Writes FBX files",  # description applied via Key Components
            "language": "python",
            "source_files": ["client/fbx_writer.py"],
            "source_roots": [],
            "name": "Fbx Writer",
        },
        "shtp-receiver": {
            "slug": "shtp-receiver",
            "primary_class": "ShtpReceiver",
            "description": "",
            "language": "python",
            "source_files": ["client/shtp_receiver.py"],
            "source_roots": [],
            "name": "Shtp Receiver",
        },
    }
    candidates = filter_modules_for_llm_pass(modules)
    slugs = [m["slug"] for m in candidates]
    assert "fbx-writer" not in slugs
    assert "shtp-receiver" in slugs


# @spec IA-LLM-002
def test_llm_pass1_sends_batched_call():
    from modok.import_arrow.llm import run_name_description_pass

    modules_to_name = [
        {
            "slug": "shtp-receiver",
            "primary_class": "ShtpReceiver",
            "source_file": "client/shtp_receiver.py",
            "language": "python",
        },
        {
            "slug": "ltc-service",
            "primary_class": "LtcTimecodeService",
            "source_file": "client/ltc.py",
            "language": "python",
        },
    ]
    mock_llm = MagicMock(
        return_value=[
            {
                "slug": "shtp-receiver",
                "name": "SHTP Receiver",
                "description": "Receives SHTP packets",
            },
            {
                "slug": "ltc-service",
                "name": "LTC Timecode Service",
                "description": "Decodes LTC audio",
            },
        ]
    )
    results = run_name_description_pass(modules_to_name, llm_call=mock_llm)
    assert mock_llm.call_count == 1
    assert results["shtp-receiver"]["name"] == "SHTP Receiver"
    assert results["ltc-service"]["name"] == "LTC Timecode Service"


# @spec IA-LLM-003
def test_llm_dedup_sends_one_call_per_pair():
    from modok.import_arrow.llm import run_dedup_pass

    pairs = [
        ("renderer-a", "renderer-b"),
        ("writer-a", "writer-b"),
    ]
    modules = {
        "renderer-a": {
            "name": "Renderer A",
            "description": "D",
            "source_files": ["client/r.py"],
            "source_roots": [],
        },
        "renderer-b": {
            "name": "Renderer B",
            "description": "D",
            "source_files": ["client/r.py"],
            "source_roots": [],
        },
        "writer-a": {
            "name": "Writer A",
            "description": "D",
            "source_files": ["client/w.py"],
            "source_roots": [],
        },
        "writer-b": {
            "name": "Writer B",
            "description": "D",
            "source_files": ["client/w.py"],
            "source_roots": [],
        },
    }
    call_args_list = []

    def mock_llm(payload):
        call_args_list.append(payload)
        return {"same_concept": True, "keep": payload["slug_a"], "description": "D"}

    run_dedup_pass(pairs, modules, llm_call=mock_llm)
    assert len(call_args_list) == 2


# @spec IA-LLM-004
def test_llm_dedup_same_concept_retains_winner():
    from modok.import_arrow.llm import run_dedup_pass

    pairs = [("renderer-a", "renderer-b")]
    modules = {
        "renderer-a": {
            "name": "Renderer A",
            "description": "D",
            "source_files": ["client/r.py"],
            "source_roots": [],
        },
        "renderer-b": {
            "name": "Renderer B",
            "description": "D",
            "source_files": ["client/r.py"],
            "source_roots": [],
        },
    }

    def mock_llm(_):
        return {"same_concept": True, "keep": "renderer-b", "description": "Better description"}

    result_modules, conflicts = run_dedup_pass(pairs, modules, llm_call=mock_llm)
    assert "renderer-b" in result_modules
    assert "renderer-a" not in result_modules
    assert conflicts == []


# @spec IA-LLM-005
def test_llm_dedup_different_concepts_produces_conflict():
    from modok.import_arrow.llm import run_dedup_pass

    pairs = [("renderer-a", "renderer-b")]
    modules = {
        "renderer-a": {
            "name": "Renderer A",
            "description": "D",
            "source_files": ["client/r.py"],
            "source_roots": [],
        },
        "renderer-b": {
            "name": "Renderer B",
            "description": "D",
            "source_files": ["client/r.py"],
            "source_roots": [],
        },
    }

    def mock_llm(_):
        return {"same_concept": False}

    result_modules, conflicts = run_dedup_pass(pairs, modules, llm_call=mock_llm)
    assert len(conflicts) == 1
    assert "renderer-a" in conflicts[0] or "renderer-b" in conflicts[0]


# @spec IA-LLM-006
def test_llm_no_llm_retains_both_duplicates_with_warning(capsys):
    from modok.import_arrow.dedup import resolve_duplicates_no_llm

    pairs = [("renderer-a", "renderer-b")]
    modules = {
        "renderer-a": {
            "name": "Renderer A",
            "description": "D",
            "source_files": ["client/r.py"],
            "source_roots": [],
        },
        "renderer-b": {
            "name": "Renderer B",
            "description": "D",
            "source_files": ["client/r.py"],
            "source_roots": [],
        },
    }
    result_modules = resolve_duplicates_no_llm(pairs, modules)
    assert "renderer-a" in result_modules
    assert "renderer-b" in result_modules
    captured = capsys.readouterr()
    assert "warn" in captured.err.lower() or "duplicate" in captured.err.lower()


# @spec IA-LLM-007
def test_llm_pass1_failure_falls_back_to_title_case(capsys):
    from modok.import_arrow.llm import run_name_description_pass

    modules_to_name = [
        {
            "slug": "shtp-receiver",
            "primary_class": "ShtpReceiver",
            "source_file": "client/shtp_receiver.py",
            "language": "python",
        },
    ]

    def failing_llm(_):
        raise RuntimeError("LLM timeout")

    results = run_name_description_pass(modules_to_name, llm_call=failing_llm)
    assert results["shtp-receiver"]["name"] == "Shtp Receiver"
    captured = capsys.readouterr()
    assert "warn" in captured.err.lower() or "fallback" in captured.err.lower()


# @spec IA-LLM-008
def test_llm_pass2_failure_treats_as_conflict():
    from modok.import_arrow.llm import run_dedup_pass

    pairs = [("renderer-a", "renderer-b")]
    modules = {
        "renderer-a": {
            "name": "A",
            "description": "D",
            "source_files": ["client/r.py"],
            "source_roots": [],
        },
        "renderer-b": {
            "name": "B",
            "description": "D",
            "source_files": ["client/r.py"],
            "source_roots": [],
        },
    }

    def failing_llm(_):
        raise RuntimeError("LLM parse error")

    result_modules, conflicts = run_dedup_pass(pairs, modules, llm_call=failing_llm)
    assert len(conflicts) == 1


# ---------------------------------------------------------------------------
# IA-WIRE — Feature → Module wiring
# ---------------------------------------------------------------------------


# @spec IA-WIRE-001
def test_wiring_populates_feature_modules_list():
    from modok.import_arrow.wiring import wire_features_to_modules

    features = {
        "feat-a": {
            "source_files": ["client/renderer.py", "client/state.py"],
            "modules": [],
        }
    }
    modules = {
        "renderer": {"source_files": ["client/renderer.py"], "source_roots": []},
        "state": {"source_files": ["client/state.py"], "source_roots": []},
        "other": {"source_files": ["client/other.py"], "source_roots": []},
    }
    result = wire_features_to_modules(features, modules)
    assert "renderer" in result["feat-a"]["modules"]
    assert "state" in result["feat-a"]["modules"]
    assert "other" not in result["feat-a"]["modules"]


# @spec IA-WIRE-001
def test_wiring_requires_nonempty_subset():
    from modok.import_arrow.wiring import wire_features_to_modules

    features = {"feat-a": {"source_files": ["client/renderer.py"], "modules": []}}
    modules = {
        "empty-module": {"source_files": [], "source_roots": []},
    }
    result = wire_features_to_modules(features, modules)
    assert "empty-module" not in result["feat-a"]["modules"]


# @spec IA-WIRE-002
def test_wiring_shared_module_appears_in_multiple_features():
    from modok.import_arrow.wiring import wire_features_to_modules

    features = {
        "feat-a": {"source_files": ["shared/hub.py", "client/a.py"], "modules": []},
        "feat-b": {"source_files": ["shared/hub.py", "client/b.py"], "modules": []},
    }
    modules = {
        "hub": {"source_files": ["shared/hub.py"], "source_roots": []},
        "module-a": {"source_files": ["client/a.py"], "source_roots": []},
        "module-b": {"source_files": ["client/b.py"], "source_roots": []},
    }
    result = wire_features_to_modules(features, modules)
    assert "hub" in result["feat-a"]["modules"]
    assert "hub" in result["feat-b"]["modules"]


# @spec IA-WIRE-003
def test_wiring_directory_module_wired_by_source_roots_prefix():
    from modok.import_arrow.wiring import wire_features_to_modules

    features = {
        "pi-agent": {
            "source_files": ["agent/src/agent.c", "agent/src/shtp.c"],
            "modules": [],
        }
    }
    modules = {
        "agent-src": {
            "source_files": [],
            "source_roots": ["agent/src"],
        }
    }
    result = wire_features_to_modules(features, modules)
    assert "agent-src" in result["pi-agent"]["modules"]


# ---------------------------------------------------------------------------
# IA-OUT — Output
# ---------------------------------------------------------------------------


# @spec IA-OUT-001
def test_output_writes_both_files_to_registries_dir(tmp_path):
    from modok.import_arrow.writer import write_registries

    features = {
        "widget-core": {
            "name": "Widget Core",
            "description": "Core widget",
            "source_files": ["client/widget/renderer.py"],
            "test_files": ["client/tests/test_widget.py"],
            "specs": "docs/specs/widget-core-specs.md",
            "modules": ["renderer"],
        }
    }
    modules = {
        "renderer": {
            "name": "Renderer",
            "description": "Renders widgets",
            "source_files": ["client/widget/renderer.py"],
            "source_roots": [],
            "primary_class": "WidgetRenderer",
            "language": "python",
        }
    }
    write_registries(features, modules, repo_root=tmp_path)
    assert (tmp_path / "registries" / "features.yml").exists()
    assert (tmp_path / "registries" / "modules.yml").exists()


# @spec IA-OUT-001
def test_output_creates_registries_dir_if_absent(tmp_path):
    from modok.import_arrow.writer import write_registries

    assert not (tmp_path / "registries").exists()
    write_registries({}, {}, repo_root=tmp_path)
    assert (tmp_path / "registries").is_dir()


# @spec IA-OUT-002
def test_output_files_are_valid_yaml(tmp_path):
    from modok.import_arrow.writer import write_registries

    features = {
        "feat-a": {
            "name": "Feat A",
            "description": "X",
            "source_files": [],
            "test_files": [],
            "specs": "s.md",
            "modules": [],
        }
    }
    modules = {
        "mod-a": {
            "name": "Mod A",
            "description": "Y",
            "source_files": [],
            "source_roots": [],
            "primary_class": "",
            "language": "python",
        }
    }
    write_registries(features, modules, repo_root=tmp_path)
    data_f = yaml.safe_load((tmp_path / "registries/features.yml").read_text())
    data_m = yaml.safe_load((tmp_path / "registries/modules.yml").read_text())
    assert isinstance(data_f, dict)
    assert isinstance(data_m, dict)


# @spec IA-OUT-003
def test_output_features_yml_schema(tmp_path):
    from modok.import_arrow.writer import write_registries

    features = {
        "feat-a": {
            "name": "Feat A",
            "description": "A feature",
            "source_files": ["client/a.py"],
            "test_files": ["tests/test_a.py"],
            "specs": "docs/specs/feat-a-specs.md",
            "modules": ["mod-a"],
        }
    }
    write_registries(features, {}, repo_root=tmp_path)
    data = yaml.safe_load((tmp_path / "registries/features.yml").read_text())
    entry = data["features"]["feat-a"]
    assert "name" in entry
    assert "description" in entry
    assert "source_files" in entry
    assert "test_files" in entry
    assert "specs" in entry
    assert "modules" in entry


# @spec IA-OUT-004
def test_output_modules_yml_schema(tmp_path):
    from modok.import_arrow.writer import write_registries

    modules = {
        "mod-a": {
            "name": "Mod A",
            "description": "A module",
            "source_files": ["client/a.py"],
            "source_roots": [],
            "primary_class": "ClassA",
            "language": "python",
        }
    }
    write_registries({}, modules, repo_root=tmp_path)
    data = yaml.safe_load((tmp_path / "registries/modules.yml").read_text())
    entry = data["modules"]["mod-a"]
    assert "name" in entry
    assert "description" in entry
    assert "source_files" in entry
    assert "source_roots" in entry
    assert "primary_class" in entry
    assert "language" in entry


# @spec IA-OUT-005
def test_output_both_files_regenerated_together(tmp_path):
    from modok.import_arrow.writer import write_registries

    write_registries(
        {
            "feat-a": {
                "name": "A",
                "description": "X",
                "source_files": [],
                "test_files": [],
                "specs": "s.md",
                "modules": [],
            }
        },
        {
            "mod-a": {
                "name": "A",
                "description": "Y",
                "source_files": [],
                "source_roots": [],
                "primary_class": "",
                "language": "python",
            }
        },
        repo_root=tmp_path,
    )

    write_registries(
        {
            "feat-b": {
                "name": "B",
                "description": "Y",
                "source_files": [],
                "test_files": [],
                "specs": "s.md",
                "modules": [],
            }
        },
        {
            "mod-b": {
                "name": "B",
                "description": "Z",
                "source_files": [],
                "source_roots": [],
                "primary_class": "",
                "language": "python",
            }
        },
        repo_root=tmp_path,
    )

    features_data = yaml.safe_load((tmp_path / "registries/features.yml").read_text())
    modules_data = yaml.safe_load((tmp_path / "registries/modules.yml").read_text())
    # Both files reflect the second write — feat-a/mod-a replaced by feat-b/mod-b
    assert "feat-b" in features_data["features"]
    assert "feat-a" not in features_data["features"]
    assert "mod-b" in modules_data["modules"]
    assert "mod-a" not in modules_data["modules"]


# @spec IA-OUT-006
def test_output_dry_run_no_files_written(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    make_repo(tmp_path)
    runner = CliRunner()
    runner.invoke(cli, ["import-arrow", "--project", "x", "--repo", str(tmp_path), "--dry-run"])
    assert not (tmp_path / "registries").exists()


# ---------------------------------------------------------------------------
# IA-UNCLAIMED — Non-arrow modules
# ---------------------------------------------------------------------------


# @spec IA-UNCLAIMED-001
def test_unclaimed_files_printed_to_stdout(tmp_path, capsys):
    from modok.import_arrow.extractor import report_unclaimed_files

    features = {
        "feat-a": {"source_files": ["client/renderer.py"]},
    }
    code_map = make_code_map(
        [
            make_code_map_entry("client/renderer.py"),
            make_code_map_entry("client/math_utils.py"),
            make_code_map_entry("client/log_format.py"),
        ]
    )
    report_unclaimed_files(features, code_map)
    captured = capsys.readouterr()
    assert "math_utils.py" in captured.out
    assert "log_format.py" in captured.out
    assert "renderer.py" not in captured.out


# @spec IA-UNCLAIMED-001
def test_unclaimed_files_count_printed(tmp_path, capsys):
    from modok.import_arrow.extractor import report_unclaimed_files

    features = {"feat-a": {"source_files": []}}
    code_map = make_code_map(
        [
            make_code_map_entry("client/a.py"),
            make_code_map_entry("client/b.py"),
        ]
    )
    report_unclaimed_files(features, code_map)
    captured = capsys.readouterr()
    # LLD format: "N source files in code map not claimed by any arrow:"
    assert "2 source files" in captured.out.lower()
