"""
Tests for git history ingestion.

Specs verified: SI-GIT-001 through SI-GIT-010.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from modok.ingestion.git_history import (
    CommitRecord,
    HunkRecord,
    build_registered_file_set,
    load_last_git_sha,
    parse_commit_log,
    save_last_git_sha,
    _parse_diff,
    _update_project_config_field,
)
from modok.ingestion.registry import Registry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_file(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content))
    return path


def make_registry(tmp_path: Path, features: dict | None = None) -> Registry:
    features = features or {}
    reg_dir = tmp_path / "registries"
    reg_dir.mkdir(exist_ok=True)
    (reg_dir / "features.yml").write_text(yaml.dump({"features": features}))
    (reg_dir / "modules.yml").write_text("modules: {}\n")
    (reg_dir / "errors.yml").write_text("errors: {}\n")
    (reg_dir / "doc-types.yml").write_text("doc_types: {}\n")
    return Registry(repo_root=tmp_path)


# Raw git log output format produced by:
#   git log --format="COMMIT %H%n%aI%n%aN%n%aE%n%s" --name-status
_SAMPLE_LOG = """\
COMMIT aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
2024-01-15T10:30:00+00:00
Alice Dev
alice@example.com
fix: handle version mismatch in SHTP handshake
M\tagent/src/shtp.c
A\tagent/tests/test_shtp.c

COMMIT bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
2024-01-14T08:00:00+00:00
Bob Dev
bob@example.com
refactor: extract shtp version check into helper
M\tagent/src/shtp.c
"""

_SAMPLE_LOG_LONG_MSG = """\
COMMIT cccccccccccccccccccccccccccccccccccccccc
2024-01-13T12:00:00+00:00
Carol Dev
carol@example.com
{long_message}
M\tagent/src/shtp.c
""".format(long_message="x" * 200)


# ---------------------------------------------------------------------------
# SI-GIT-001 — modok ingest-git command exists
# ---------------------------------------------------------------------------


# @spec SI-GIT-001
def test_ingest_git_command_exists():
    from click.testing import CliRunner
    from modok.cli.main import cli

    runner = CliRunner()
    result = runner.invoke(cli, ["ingest-git", "--help"])
    assert result.exit_code == 0
    assert "project" in result.output.lower()


# ---------------------------------------------------------------------------
# SI-GIT-002 — Commit node schema
# ---------------------------------------------------------------------------


# @spec SI-GIT-002
def test_parse_commit_log_returns_commit_records():
    records = parse_commit_log(_SAMPLE_LOG)
    assert len(records) == 2


# @spec SI-GIT-002
def test_commit_record_has_full_40_char_sha():
    records = parse_commit_log(_SAMPLE_LOG)
    for r in records:
        assert len(r.sha) == 40
        assert all(c in "0123456789abcdef" for c in r.sha)


# @spec SI-GIT-002
def test_commit_record_has_iso8601_timestamp():
    records = parse_commit_log(_SAMPLE_LOG)
    assert records[0].timestamp == "2024-01-15T10:30:00+00:00"
    assert records[1].timestamp == "2024-01-14T08:00:00+00:00"


# @spec SI-GIT-002
def test_commit_record_has_author_name_and_email():
    records = parse_commit_log(_SAMPLE_LOG)
    assert records[0].author_name == "Alice Dev"
    assert records[0].author_email == "alice@example.com"


# @spec SI-GIT-002
def test_commit_record_message_is_first_line_only():
    records = parse_commit_log(_SAMPLE_LOG)
    assert "\n" not in records[0].message
    assert records[0].message == "fix: handle version mismatch in SHTP handshake"


# @spec SI-GIT-002
def test_commit_record_message_truncated_to_120_chars():
    records = parse_commit_log(_SAMPLE_LOG_LONG_MSG)
    assert len(records[0].message) <= 120


# @spec SI-GIT-002
def test_commit_record_branch_field_present():
    records = parse_commit_log(_SAMPLE_LOG)
    # branch is determined at ingest time (not from log); CommitRecord carries it
    for r in records:
        assert hasattr(r, "branch")  # may be None if not set


# ---------------------------------------------------------------------------
# SI-GIT-003 — TOUCHES edge per changed file (A, C, M, R — not D)
# ---------------------------------------------------------------------------


# @spec SI-GIT-003
def test_parse_commit_log_includes_touched_files():
    records = parse_commit_log(_SAMPLE_LOG)
    first = records[0]
    file_paths = [f for f, _ in first.touched_files]
    assert "agent/src/shtp.c" in file_paths
    assert "agent/tests/test_shtp.c" in file_paths


# @spec SI-GIT-003
def test_parse_commit_log_touched_files_carry_change_type():
    records = parse_commit_log(_SAMPLE_LOG)
    changes = {path: ct for path, ct in records[0].touched_files}
    assert changes["agent/src/shtp.c"] == "M"
    assert changes["agent/tests/test_shtp.c"] == "A"


# @spec SI-GIT-003
def test_deleted_files_excluded_from_touches():
    log_with_delete = """\
COMMIT dddddddddddddddddddddddddddddddddddddddd
2024-01-16T09:00:00+00:00
Dev User
dev@example.com
remove old file
D\tagent/src/old_shtp.c
M\tagent/src/shtp.c
"""
    records = parse_commit_log(log_with_delete)
    assert len(records) == 1
    file_paths = [f for f, _ in records[0].touched_files]
    assert "agent/src/old_shtp.c" not in file_paths
    assert "agent/src/shtp.c" in file_paths


# @spec SI-GIT-003
@pytest.mark.asyncio
async def test_write_commit_upserts_file_node_for_each_touched_path(tmp_path):
    from modok.ingestion.git_history import write_commit_to_quine
    from modok.quine.models import File

    commit = CommitRecord(
        sha="a" * 40,
        timestamp="2024-01-15T10:30:00+00:00",
        author_name="Alice",
        author_email="alice@example.com",
        message="fix something",
        branch="main",
        touched_files=[("agent/src/shtp.c", "M"), ("agent/src/other.c", "A")],
    )
    client = AsyncMock()

    await write_commit_to_quine(commit, client=client, project_slug="stagehand")

    upserted = [c.args[0] for c in client.upsert_node.call_args_list]
    file_nodes = [n for n in upserted if isinstance(n, File)]
    file_paths = {n.repo_path for n in file_nodes}
    assert "agent/src/shtp.c" in file_paths
    assert "agent/src/other.c" in file_paths


# @spec SI-GIT-003
@pytest.mark.asyncio
async def test_touches_edge_written_for_each_touched_file(tmp_path):
    from modok.ingestion.git_history import write_commit_to_quine

    commit = CommitRecord(
        sha="b" * 40,
        timestamp="2024-01-15T10:30:00+00:00",
        author_name="Alice",
        author_email="alice@example.com",
        message="add feature",
        branch="main",
        touched_files=[("agent/src/shtp.c", "A")],
    )
    client = AsyncMock()

    await write_commit_to_quine(commit, client=client, project_slug="stagehand")

    assert client.write_edge_by_parts.call_count == 1
    assert client.write_edge_by_parts.call_args_list[0].args[1] == "TOUCHES"


# @spec SI-GIT-003
@pytest.mark.asyncio
async def test_touches_edge_written_without_properties(tmp_path):
    from modok.ingestion.git_history import write_commit_to_quine

    commit = CommitRecord(
        sha="c" * 40,
        timestamp="2024-01-15T10:30:00+00:00",
        author_name="Alice",
        author_email="alice@example.com",
        message="add feature",
        branch="main",
        touched_files=[("agent/src/new.c", "A")],
    )
    client = AsyncMock()

    await write_commit_to_quine(commit, client=client, project_slug="stagehand")

    # Edge is written without properties (Quine does not persist relationship properties)
    assert client.write_edge_by_parts.call_count == 1
    call_args = client.write_edge_by_parts.call_args
    assert call_args.args[1] == "TOUCHES"
    props = call_args.kwargs.get("properties") or (
        call_args.args[3] if len(call_args.args) > 3 else None
    )
    assert props is None


# ---------------------------------------------------------------------------
# SI-GIT-004 — only registered files pass the commit filter
# ---------------------------------------------------------------------------


# @spec SI-GIT-004
def test_build_registered_file_set_includes_source_files():
    features = {
        "pi-agent": {
            "name": "Pi Agent",
            "product_area": "tracking",
            "source_files": ["agent/src/main.c", "agent/src/shtp.c"],
        }
    }
    arrow_index = {"arrows": []}
    file_set = build_registered_file_set(features, arrow_index)
    assert "agent/src/main.c" in file_set
    assert "agent/src/shtp.c" in file_set


# @spec SI-GIT-004
def test_build_registered_file_set_includes_arrow_doc_paths():
    features = {}
    arrow_index = {
        "arrows": [
            {
                "id": "pi-agent",
                "arrow_doc": "docs/arrows/pi-agent.md",
                "lld": "docs/llds/pi-agent.md",
                "specs": "docs/specs/pi-agent-specs.md",
            }
        ]
    }
    file_set = build_registered_file_set(features, arrow_index)
    assert "docs/arrows/pi-agent.md" in file_set
    assert "docs/llds/pi-agent.md" in file_set
    assert "docs/specs/pi-agent-specs.md" in file_set


# @spec SI-GIT-011
def test_build_registered_file_set_accepts_list_valued_lld():
    # A feature can legitimately have more than one LLD (e.g. client-side
    # and Pi-side halves of one arrow) — arrow_doc/lld/specs may then be a
    # YAML list rather than a single string.
    features = {}
    arrow_index = {
        "arrows": [
            {
                "id": "wifi-provisioning",
                "arrow_doc": "docs/arrows/wifi-provisioning.md",
                "lld": [
                    "docs/llds/wifi-provisioning.md",
                    "docs/llds/wifi-provisioning-client.md",
                ],
                "specs": "docs/specs/wifi-provisioning-specs.md",
            }
        ]
    }
    file_set = build_registered_file_set(features, arrow_index)  # must not raise
    assert "docs/llds/wifi-provisioning.md" in file_set
    assert "docs/llds/wifi-provisioning-client.md" in file_set


# @spec SI-GIT-004
def test_build_registered_file_set_includes_doc_paths():
    features = {}
    arrow_index = {"arrows": []}
    doc_paths = ["docs/developer-guide.md", "docs/setup.md"]
    file_set = build_registered_file_set(features, arrow_index, doc_paths=doc_paths)
    assert "docs/developer-guide.md" in file_set
    assert "docs/setup.md" in file_set


# @spec SI-GIT-004
def test_build_registered_file_set_excludes_unregistered_files():
    features = {
        "pi-agent": {
            "name": "Pi Agent",
            "product_area": "tracking",
            "source_files": ["agent/src/main.c"],
        }
    }
    arrow_index = {"arrows": []}
    file_set = build_registered_file_set(features, arrow_index)
    assert "ci/build.yml" not in file_set
    assert "package-lock.json" not in file_set


# @spec SI-GIT-004
@pytest.mark.asyncio
async def test_ingest_git_skips_commits_touching_only_unregistered_files(tmp_path):
    from modok.ingestion.git_history import ingest_git

    registered_files = {"agent/src/shtp.c"}

    log_for_unreg_only = """\
COMMIT eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee
2024-01-17T09:00:00+00:00
Dev
dev@example.com
update lock file
M\tpackage-lock.json
"""
    client = AsyncMock()
    with patch("modok.ingestion.git_history._get_git_log", return_value=log_for_unreg_only):
        with patch(
            "modok.ingestion.git_history.build_registered_file_set", return_value=registered_files
        ):
            with patch("modok.ingestion.git_history.get_head_sha", return_value="e" * 40):
                with patch("modok.ingestion.git_history.load_last_git_sha", return_value=None):
                    with patch("modok.ingestion.git_history.save_last_git_sha"):
                        await ingest_git(
                            project_slug="stagehand",
                            repo_root=tmp_path,
                            registry=MagicMock(),
                            client=client,
                            config={},
                        )

    assert client.upsert_node.call_count == 0


# ---------------------------------------------------------------------------
# SI-GIT-005 — default: last 6 months or last 500 commits
# ---------------------------------------------------------------------------


# @spec SI-GIT-005
def test_default_ingest_uses_6_month_window(tmp_path):
    from modok.ingestion.git_history import _build_git_log_command

    cmd = _build_git_log_command(
        registered_files={"agent/src/shtp.c"},
        since_sha=None,
        since_date=None,
        max_commits=500,
        full=False,
    )
    # Should include --after or --since for 6 months
    cmd_str = " ".join(cmd)
    assert "--after" in cmd_str or "--since" in cmd_str or "6 months" in cmd_str
    # Should include -n 500 (max commits)
    assert "-n" in cmd_str or "--max-count" in cmd_str


# @spec SI-GIT-005
def test_default_max_commits_is_500(tmp_path):
    from modok.ingestion.git_history import _build_git_log_command

    cmd = _build_git_log_command(
        registered_files={"agent/src/shtp.c"},
        since_sha=None,
        since_date=None,
        max_commits=500,
        full=False,
    )
    cmd_str = " ".join(cmd)
    assert "500" in cmd_str


# ---------------------------------------------------------------------------
# SI-GIT-006 — --full: no lookback limit
# ---------------------------------------------------------------------------


# @spec SI-GIT-006
def test_full_flag_removes_lookback_limit(tmp_path):
    from modok.ingestion.git_history import _build_git_log_command

    cmd = _build_git_log_command(
        registered_files={"agent/src/shtp.c"},
        since_sha=None,
        since_date=None,
        max_commits=500,
        full=True,
    )
    cmd_str = " ".join(cmd)
    assert "--after" not in cmd_str
    assert "--since" not in cmd_str
    assert "-n 500" not in cmd_str and "--max-count=500" not in cmd_str


# ---------------------------------------------------------------------------
# SI-GIT-007 — incremental: last_git_sha stored; updated after successful write
# ---------------------------------------------------------------------------


# @spec SI-GIT-007
def test_load_last_git_sha_returns_none_when_not_set(tmp_path):
    config = {"projects": [{"slug": "stagehand"}]}
    sha = load_last_git_sha(config, "stagehand")
    assert sha is None


# @spec SI-GIT-007
def test_load_last_git_sha_returns_stored_value(tmp_path):
    config = {"projects": [{"slug": "stagehand", "last_git_sha": "a" * 40}]}
    sha = load_last_git_sha(config, "stagehand")
    assert sha == "a" * 40


# @spec SI-GIT-007
def test_save_last_git_sha_writes_to_config(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('[projects]\n[[projects]]\nslug = "stagehand"\n')
    save_last_git_sha(config_path, "stagehand", "b" * 40)
    content = config_path.read_text()
    assert "b" * 40 in content


def test_update_project_config_field_stops_at_following_single_bracket_section(tmp_path):
    """A brand-new key (no existing line to replace) inserted into a
    [[projects]] block that is directly followed by a single-bracket
    [section] (e.g. [webhook]) must land inside the [[projects]] block, not
    get silently absorbed past the section boundary and appended under
    [webhook] instead — found live: last_workflow_sync ended up under
    [webhook] because the block-collection loop only recognized "[[" as a
    terminator, not a plain "[" table header."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[[projects]]\n'
        'slug = "stagehand"\n'
        'repo = "~/github/stagehand"\n'
        'last_github_sync = "2026-07-16T12:00:00Z"\n'
        '\n'
        '[webhook]\n'
        'github_poll_enabled = true\n'
    )

    _update_project_config_field(config_path, "stagehand", "last_workflow_sync", "2026-07-16T12:30:00Z")

    content = config_path.read_text()
    projects_block, _, webhook_block = content.partition("[webhook]")
    assert "last_workflow_sync" in projects_block
    assert "last_workflow_sync" not in webhook_block


# @spec SI-GIT-007
@pytest.mark.asyncio
async def test_last_git_sha_updated_only_after_successful_writes(tmp_path):
    from modok.ingestion.git_history import ingest_git

    save_calls = []
    write_calls = []

    async def fake_write_commit(commit, **kwargs):
        write_calls.append(commit.sha)

    def fake_save(config_path, slug, sha):
        # save must be called after all writes
        assert len(write_calls) > 0, "save_last_git_sha called before any commits written"
        save_calls.append(sha)

    commit_log = _SAMPLE_LOG
    registered = {"agent/src/shtp.c", "agent/tests/test_shtp.c"}

    with patch("modok.ingestion.git_history._get_git_log", return_value=commit_log):
        with patch(
            "modok.ingestion.git_history.build_registered_file_set", return_value=registered
        ):
            with patch("modok.ingestion.git_history.get_head_sha", return_value="a" * 40):
                with patch("modok.ingestion.git_history.load_last_git_sha", return_value=None):
                    with patch(
                        "modok.ingestion.git_history.save_last_git_sha", side_effect=fake_save
                    ):
                        with patch(
                            "modok.ingestion.git_history.write_commit_to_quine",
                            side_effect=fake_write_commit,
                        ):
                            await ingest_git(
                                project_slug="stagehand",
                                repo_root=tmp_path,
                                registry=MagicMock(),
                                client=AsyncMock(),
                                config={},
                            )

    assert len(save_calls) == 1
    assert len(write_calls) >= 1


# @spec SI-GIT-007
@pytest.mark.asyncio
async def test_last_git_sha_not_updated_on_write_failure(tmp_path):
    from modok.ingestion.git_history import ingest_git

    save_calls = []

    async def failing_write(commit, **kwargs):
        raise RuntimeError("Quine connection error")

    with patch("modok.ingestion.git_history._get_git_log", return_value=_SAMPLE_LOG):
        with patch(
            "modok.ingestion.git_history.build_registered_file_set",
            return_value={"agent/src/shtp.c"},
        ):
            with patch("modok.ingestion.git_history.get_head_sha", return_value="a" * 40):
                with patch("modok.ingestion.git_history.load_last_git_sha", return_value=None):
                    with patch(
                        "modok.ingestion.git_history.save_last_git_sha",
                        side_effect=lambda *a: save_calls.append(a),
                    ):
                        with patch(
                            "modok.ingestion.git_history.write_commit_to_quine",
                            side_effect=failing_write,
                        ):
                            with pytest.raises(RuntimeError):
                                await ingest_git(
                                    project_slug="stagehand",
                                    repo_root=tmp_path,
                                    registry=MagicMock(),
                                    client=AsyncMock(),
                                    config={},
                                )

    assert save_calls == [], "last_git_sha must not be updated after a failed run"


# ---------------------------------------------------------------------------
# SI-GIT-008 — post-commit hook invokes ingest-git unconditionally
# ---------------------------------------------------------------------------


# @spec SI-GIT-008
def test_hook_invokes_ingest_git_unconditionally():
    from modok.ingestion.hook import hook_content

    content = hook_content("stagehand", ["docs/", "registries/"])
    assert "ingest-git" in content


# @spec SI-GIT-008
def test_hook_ingest_git_not_inside_path_guard():
    from modok.ingestion.hook import hook_content

    content = hook_content("stagehand", ["docs/", "registries/"])
    lines = content.splitlines()

    # Find the closing fi of the path guard block
    # ingest-git must appear after fi (outside the conditional)
    git_idx = next((i for i, ln in enumerate(lines) if "ingest-git" in ln), None)
    assert git_idx is not None

    # Count 'if' and 'fi' lines before git_idx
    if_count = sum(1 for ln in lines[:git_idx] if ln.strip().startswith("if "))
    fi_count = sum(1 for ln in lines[:git_idx] if ln.strip() == "fi")
    # All opened ifs must be closed before ingest-git (i.e., we're not inside an if block)
    assert if_count <= fi_count, (
        "modok ingest-git is called inside a conditional block — "
        "it must be unconditional (SI-GIT-008)"
    )


# ---------------------------------------------------------------------------
# SI-GIT-009 — running ingest-git twice produces no duplicates
# ---------------------------------------------------------------------------


# @spec SI-GIT-009
@pytest.mark.asyncio
async def test_double_ingest_git_produces_no_duplicate_commit_nodes(tmp_path):
    from modok.ingestion.git_history import ingest_git

    registered = {"agent/src/shtp.c"}

    writes_first: list[str] = []

    async def fake_write(commit, client, project_slug):
        writes_first.append(commit.sha)

    with patch("modok.ingestion.git_history._get_git_log", return_value=_SAMPLE_LOG):
        with patch(
            "modok.ingestion.git_history.build_registered_file_set", return_value=registered
        ):
            with patch("modok.ingestion.git_history.get_head_sha", return_value="a" * 40):
                with patch("modok.ingestion.git_history.load_last_git_sha", return_value=None):
                    with patch("modok.ingestion.git_history.save_last_git_sha"):
                        with patch(
                            "modok.ingestion.git_history.write_commit_to_quine",
                            side_effect=fake_write,
                        ):
                            client = AsyncMock()
                            await ingest_git(
                                project_slug="stagehand",
                                repo_root=tmp_path,
                                registry=MagicMock(),
                                client=client,
                                config={},
                            )
    # Second run: last_git_sha == HEAD → git log range is empty
    with patch("modok.ingestion.git_history._get_git_log", return_value=""):
        with patch(
            "modok.ingestion.git_history.build_registered_file_set", return_value=registered
        ):
            with patch("modok.ingestion.git_history.get_head_sha", return_value="a" * 40):
                with patch("modok.ingestion.git_history.load_last_git_sha", return_value="a" * 40):
                    with patch("modok.ingestion.git_history.save_last_git_sha"):
                        client2 = AsyncMock()
                        await ingest_git(
                            project_slug="stagehand",
                            repo_root=tmp_path,
                            registry=MagicMock(),
                            client=client2,
                            config={},
                        )
                        second_count = client2.upsert_node.call_count

    assert second_count == 0, "second run on same state must import no new commits"


# @spec SI-GIT-009
@given(
    sha=st.text(alphabet="0123456789abcdef", min_size=40, max_size=40),
    timestamp=st.just("2024-01-15T10:30:00+00:00"),
)
@settings(max_examples=20)
def test_parse_commit_log_is_idempotent(sha, timestamp):
    log = f"""\
COMMIT {sha}
{timestamp}
Test Author
test@example.com
Some commit message
M\tagent/src/shtp.c
"""
    records1 = parse_commit_log(log)
    records2 = parse_commit_log(log)
    assert len(records1) == len(records2)
    if records1:
        assert records1[0].sha == records2[0].sha
        assert records1[0].timestamp == records2[0].timestamp


# ---------------------------------------------------------------------------
# SI-GIT-010 — --since flag behavior
# ---------------------------------------------------------------------------


# @spec SI-GIT-010
def test_since_flag_overrides_6_month_default():
    from modok.ingestion.git_history import _build_git_log_command

    cmd = _build_git_log_command(
        registered_files={"agent/src/shtp.c"},
        since_sha=None,
        since_date="2023-06-01",
        max_commits=500,
        full=False,
    )
    cmd_str = " ".join(cmd)
    assert "2023-06-01" in cmd_str


# @spec SI-GIT-010
def test_since_and_max_commits_both_apply():
    from modok.ingestion.git_history import _build_git_log_command

    cmd = _build_git_log_command(
        registered_files={"agent/src/shtp.c"},
        since_sha=None,
        since_date="2023-06-01",
        max_commits=100,
        full=False,
    )
    cmd_str = " ".join(cmd)
    assert "2023-06-01" in cmd_str
    assert "100" in cmd_str


# @spec SI-GIT-010
@pytest.mark.asyncio
async def test_since_and_full_mutually_exclusive(tmp_path):
    from modok.ingestion.git_history import ingest_git

    with pytest.raises(SystemExit) as exc_info:
        await ingest_git(
            project_slug="stagehand",
            repo_root=tmp_path,
            registry=MagicMock(),
            client=AsyncMock(),
            config={},
            full=True,
            since_date="2023-06-01",
        )

    assert exc_info.value.code == 1


# @spec SI-GIT-010
def test_since_replaces_6_month_window_not_max_commits():
    from modok.ingestion.git_history import _build_git_log_command

    since_cmd = _build_git_log_command(
        registered_files={"agent/src/shtp.c"},
        since_sha=None,
        since_date="2023-01-01",
        max_commits=500,
        full=False,
    )

    since_str = " ".join(since_cmd)

    # Both should limit by count
    assert "500" in since_str
    # since_cmd should use the explicit date, not the 6-month relative window
    assert "2023-01-01" in since_str
    # The 6-month relative window keyword should NOT appear when --since is given
    assert "6 months" not in since_str or "month" not in since_str.lower()


# ---------------------------------------------------------------------------
# Diff parsing — _parse_diff / HunkRecord
# ---------------------------------------------------------------------------

_PYTHON_DIFF = """\
diff --git a/src/shtp.py b/src/shtp.py
index abc1234..def5678 100644
--- a/src/shtp.py
+++ b/src/shtp.py
@@ -10,3 +10,5 @@ class ShtpHandler:
+    def handle_packet(self, pkt):
+        return pkt
 existing_line = True
-old_line = True
+new_line = True
@@ -50,1 +52,2 @@ class ShtpHandler:
+def parse_header(buf):
+    pass
"""

_MULTI_FILE_DIFF = """\
diff --git a/agent/src/shtp.c b/agent/src/shtp.c
index aaa..bbb 100644
--- a/agent/src/shtp.c
+++ b/agent/src/shtp.c
@@ -5,2 +5,3 @@ static void init(void) {
+    int x = 1;
+    return x;
 }
diff --git a/agent/src/util.c b/agent/src/util.c
index ccc..ddd 100644
--- a/agent/src/util.c
+++ b/agent/src/util.c
@@ -1,1 +1,3 @@
+int helper(int a) {
+    return a;
+}
"""

_GO_DIFF = """\
diff --git a/pkg/server.go b/pkg/server.go
index 111..222 100644
--- a/pkg/server.go
+++ b/pkg/server.go
@@ -20,2 +20,4 @@ func (s *Server) Start() {
+func HandleRequest(w http.ResponseWriter, r *http.Request) {
+    w.WriteHeader(200)
+}
+
"""

_RUST_DIFF = """\
diff --git a/src/lib.rs b/src/lib.rs
index aaa..bbb 100644
--- a/src/lib.rs
+++ b/src/lib.rs
@@ -1,1 +1,3 @@
+pub fn compute(x: i32) -> i32 {
+    x * 2
+}
"""


def test_parse_diff_returns_file_keys():
    result = _parse_diff(_PYTHON_DIFF)
    assert "src/shtp.py" in result


def test_parse_diff_extracts_hunk_line_range():
    result = _parse_diff(_PYTHON_DIFF)
    hunks = result["src/shtp.py"]
    assert hunks[0].lines == (10, 14)  # +10,5 → lines 10..14
    assert hunks[1].lines == (52, 53)  # +52,2 → lines 52..53


def test_parse_diff_extracts_function_context_from_header():
    result = _parse_diff(_PYTHON_DIFF)
    # git puts "class ShtpHandler:" in the @@ context
    assert result["src/shtp.py"][0].function_context == "class ShtpHandler:"


def test_parse_diff_detects_python_function_defs():
    result = _parse_diff(_PYTHON_DIFF)
    all_defs = [d for h in result["src/shtp.py"] for d in h.added_defs]
    assert "handle_packet" in all_defs
    assert "parse_header" in all_defs


def test_parse_diff_does_not_flag_plain_added_lines_as_defs():
    result = _parse_diff(_PYTHON_DIFF)
    # "new_line = True" and "return pkt" should not be detected as defs
    all_defs = [d for h in result["src/shtp.py"] for d in h.added_defs]
    assert "new_line" not in all_defs
    assert "return" not in all_defs


def test_parse_diff_multiple_files():
    result = _parse_diff(_MULTI_FILE_DIFF)
    assert "agent/src/shtp.c" in result
    assert "agent/src/util.c" in result


def test_parse_diff_detects_go_function():
    result = _parse_diff(_GO_DIFF)
    defs = [d for h in result.get("pkg/server.go", []) for d in h.added_defs]
    assert "HandleRequest" in defs


def test_parse_diff_detects_rust_function():
    result = _parse_diff(_RUST_DIFF)
    defs = [d for h in result.get("src/lib.rs", []) for d in h.added_defs]
    assert "compute" in defs


def test_parse_diff_empty_string_returns_empty():
    assert _parse_diff("") == {}


def test_parse_diff_no_added_lines_has_empty_defs():
    delete_only_diff = """\
diff --git a/src/old.py b/src/old.py
index aaa..bbb 100644
--- a/src/old.py
+++ b/src/old.py
@@ -1,3 +1,0 @@
-def removed():
-    pass
"""
    result = _parse_diff(delete_only_diff)
    if "src/old.py" in result:
        for h in result["src/old.py"]:
            assert h.added_defs == []


def test_parse_diff_hunk_count_of_one_when_omitted():
    # @@ -5 +5 @@ means count=1
    single_line_diff = """\
diff --git a/foo.py b/foo.py
index aaa..bbb 100644
--- a/foo.py
+++ b/foo.py
@@ -5 +5 @@ some_context
+x = 1
"""
    result = _parse_diff(single_line_diff)
    assert result["foo.py"][0].lines == (5, 5)


@pytest.mark.asyncio
async def test_commit_node_carries_file_hunks_when_populated():
    import json
    from modok.ingestion.git_history import write_commit_to_quine
    from modok.quine.models import Commit as CommitNode

    commit = CommitRecord(
        sha="d" * 40,
        timestamp="2024-01-15T10:30:00+00:00",
        author_name="Alice",
        author_email="alice@example.com",
        message="add handler",
        branch="main",
        touched_files=[("src/shtp.py", "M")],
        file_hunks={
            "src/shtp.py": [
                HunkRecord(
                    lines=(10, 12), function_context="class Shtp:", added_defs=["handle_packet"]
                )
            ]
        },
    )
    client = AsyncMock()

    await write_commit_to_quine(commit, client=client, project_slug="stagehand")

    upserted = [c.args[0] for c in client.upsert_node.call_args_list]
    commit_nodes = [n for n in upserted if isinstance(n, CommitNode)]
    assert len(commit_nodes) == 1
    parsed = json.loads(commit_nodes[0].file_hunks)
    assert "src/shtp.py" in parsed
    hunks = parsed["src/shtp.py"]
    assert hunks[0]["lines"] == [10, 12]
    assert hunks[0]["function"] == "class Shtp:"
    assert hunks[0]["defs"] == ["handle_packet"]


@pytest.mark.asyncio
async def test_commit_node_file_hunks_empty_when_no_hunk_data():
    from modok.ingestion.git_history import write_commit_to_quine
    from modok.quine.models import Commit as CommitNode

    commit = CommitRecord(
        sha="e" * 40,
        timestamp="2024-01-15T10:30:00+00:00",
        author_name="Alice",
        author_email="alice@example.com",
        message="tweak config",
        branch="main",
        touched_files=[("src/shtp.py", "M")],
        # file_hunks defaults to {}
    )
    client = AsyncMock()

    await write_commit_to_quine(commit, client=client, project_slug="stagehand")

    upserted = [c.args[0] for c in client.upsert_node.call_args_list]
    commit_nodes = [n for n in upserted if isinstance(n, CommitNode)]
    assert commit_nodes[0].file_hunks == ""
