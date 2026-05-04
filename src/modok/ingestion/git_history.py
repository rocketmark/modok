"""
Git history ingestion — imports commits touching registered files as Commit nodes.

Specs: SI-GIT-001 through SI-GIT-010.
"""
# @spec SI-GIT-001, SI-GIT-002, SI-GIT-003, SI-GIT-004, SI-GIT-005,
#       SI-GIT-006, SI-GIT-007, SI-GIT-008, SI-GIT-009, SI-GIT-010

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class HunkRecord:
    lines: tuple[int, int]        # [new_start, new_end] in post-patch file (1-indexed)
    function_context: str | None  # text git extracted from the @@ header (heuristic)
    added_defs: list[str]         # function/method names first defined in this hunk


@dataclass
class CommitRecord:
    sha: str
    timestamp: str         # ISO-8601 author date
    author_name: str
    author_email: str
    message: str           # first line only, max 120 chars
    branch: str | None     # branch name at ingest time; None if detached HEAD
    touched_files: list[tuple[str, str]] = field(default_factory=list)  # (path, change_type)
    file_hunks: dict[str, list[HunkRecord]] = field(default_factory=dict)  # path → hunks


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_commit_log(log_output: str) -> list[CommitRecord]:
    """Parse git log output in COMMIT-delimited format.

    Expected format per commit (produced by --format="COMMIT %H%n%aI%n%aN%n%aE%n%s"):
      COMMIT <40-char sha>
      <ISO-8601 timestamp>
      <author name>
      <author email>
      <subject line>
      [M|A|C|R|D]\t<file path>   (zero or more, from --name-status)
      [blank line]
    """
    records: list[CommitRecord] = []
    lines = log_output.splitlines()
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        if not line.startswith("COMMIT "):
            i += 1
            continue

        sha = line[7:].strip()
        if len(sha) != 40:
            i += 1
            continue

        if i + 4 >= n:
            break

        timestamp = lines[i + 1].strip()
        author_name = lines[i + 2].strip()
        author_email = lines[i + 3].strip()
        message = lines[i + 4].strip()[:120]
        i += 5

        touched: list[tuple[str, str]] = []
        # Skip the blank line git inserts between the format block and --name-status lines
        while i < n and lines[i] == "":
            i += 1
        while i < n:
            fl = lines[i]
            if fl.startswith("COMMIT "):
                break
            if fl == "":
                i += 1
                break
            if "\t" in fl:
                parts = fl.split("\t", 1)
                change_type = parts[0].strip()
                file_path = parts[1].strip()
                if change_type in ("A", "C", "M", "R"):
                    touched.append((file_path, change_type))
            i += 1

        records.append(CommitRecord(
            sha=sha,
            timestamp=timestamp,
            author_name=author_name,
            author_email=author_email,
            message=message,
            branch=None,
            touched_files=touched,
        ))

    return records


# ---------------------------------------------------------------------------
# Registered file set
# ---------------------------------------------------------------------------

def build_registered_file_set(
    features: dict,
    arrow_index: dict,
    doc_paths: list[str] | None = None,
) -> frozenset[str]:
    """Build the set of file paths that qualify for commit filter (SI-GIT-004).

    Includes:
    - source_files from every feature entry in features.yml
    - arrow_doc, lld, and specs paths from the arrow index
    - registered doc file paths (passed from discover_docs at ingest-git time)
    """
    paths: set[str] = set()

    for entry in features.values():
        if isinstance(entry, dict):
            for fpath in entry.get("source_files", []):
                if fpath:
                    paths.add(fpath)

    for arrow in (arrow_index or {}).get("arrows", []) or []:
        if not isinstance(arrow, dict):
            continue
        for key in ("arrow_doc", "lld", "specs"):
            p = arrow.get(key)
            if p:
                paths.add(p)

    for p in doc_paths or []:
        if p:
            paths.add(p)

    return frozenset(paths)


# ---------------------------------------------------------------------------
# Git command builder
# ---------------------------------------------------------------------------

def _build_git_log_command(
    registered_files: set[str] | frozenset[str],
    since_sha: str | None = None,
    since_date: str | None = None,
    max_commits: int = 500,
    full: bool = False,
) -> list[str]:
    """Build the git log command for commit discovery."""
    cmd = [
        "git", "log",
        "--format=COMMIT %H%n%aI%n%aN%n%aE%n%s",
        "--name-status",
        "--diff-filter=ACMR",
    ]

    if full:
        pass  # no range limit — full history
    elif since_sha:
        # Incremental: only commits strictly after last_git_sha
        cmd.append(f"{since_sha}..HEAD")
    else:
        date_limit = since_date if since_date else "6 months ago"
        cmd.extend(["--after", date_limit, f"--max-count={max_commits}"])

    if registered_files:
        cmd.extend(["--"] + sorted(registered_files))

    return cmd


# ---------------------------------------------------------------------------
# Diff parsing — hunk extraction
# ---------------------------------------------------------------------------

# Matches the +new_start[,new_count] portion of a @@ hunk header, plus any
# trailing function-context text that git extracts with its own heuristics.
_HUNK_HEADER_RE = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@[ \t]*(.*)')

# Detects function/method definition lines across Python, JS/TS, Go, and Rust.
# Applied to each `+` line (leading whitespace stripped) to find new definitions.
_FUNC_DEF_RE = re.compile(
    r'^(?:'
    r'(?:async\s+)?def\s+(\w+)'                                    # Python
    r'|(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)'  # JS/TS
    r'|func\s+(?:\(\w[^)]*\)\s+)?(\w+)'                           # Go (with optional receiver)
    r'|(?:pub(?:\s+\w+)?\s+)?fn\s+(\w+)'                          # Rust
    r')\s*[(<\[]',
    re.ASCII,
)


def _make_hunk(
    start: int,
    end: int,
    func_ctx: str | None,
    added_lines: list[str],
) -> HunkRecord:
    added_defs: list[str] = []
    for raw in added_lines:
        m = _FUNC_DEF_RE.match(raw.lstrip())
        if m:
            name = next((g for g in m.groups() if g is not None), None)
            if name:
                added_defs.append(name)
    return HunkRecord(lines=(start, end), function_context=func_ctx, added_defs=added_defs)


def _parse_diff(diff_output: str) -> dict[str, list[HunkRecord]]:
    """Parse unified diff output into per-file hunk records."""
    file_hunks: dict[str, list[HunkRecord]] = {}
    current_file: str | None = None
    pending_hunks: list[HunkRecord] = []

    hunk_start = 0
    hunk_end = 0
    func_ctx: str | None = None
    added_lines: list[str] = []
    in_hunk = False

    for line in diff_output.splitlines():
        if line.startswith('diff --git '):
            if in_hunk:
                pending_hunks.append(_make_hunk(hunk_start, hunk_end, func_ctx, added_lines))
                in_hunk = False
            if current_file and pending_hunks:
                file_hunks[current_file] = list(pending_hunks)
            current_file = None
            pending_hunks = []
            added_lines = []
        elif line.startswith('+++ b/'):
            current_file = line[6:]
        elif line.startswith('+++ /dev/null'):
            current_file = None
        elif line.startswith('@@ '):
            if in_hunk:
                pending_hunks.append(_make_hunk(hunk_start, hunk_end, func_ctx, added_lines))
            added_lines = []
            m = _HUNK_HEADER_RE.match(line)
            if m:
                hunk_start = int(m.group(1))
                raw_count = m.group(2)
                new_count = int(raw_count) if raw_count is not None else 1
                hunk_end = max(hunk_start, hunk_start + new_count - 1)
                ctx = m.group(3).strip()
                func_ctx = ctx if ctx else None
                in_hunk = True
            else:
                in_hunk = False
        elif line.startswith('+') and not line.startswith('+++'):
            if in_hunk:
                added_lines.append(line[1:])

    if in_hunk:
        pending_hunks.append(_make_hunk(hunk_start, hunk_end, func_ctx, added_lines))
    if current_file and pending_hunks:
        file_hunks[current_file] = pending_hunks

    return file_hunks


def get_diff_hunks(sha: str, repo_root: Path) -> dict[str, list[HunkRecord]]:
    """Return parsed hunk records for every file touched by a commit."""
    result = subprocess.run(
        ['git', 'diff-tree', '--no-commit-id', '-r', '--unified=0', '-p', sha],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout:
        return {}
    return _parse_diff(result.stdout)


# ---------------------------------------------------------------------------
# Git subprocess helpers
# ---------------------------------------------------------------------------

def _get_git_log(cmd: list[str], repo_root: Path | None = None) -> str:
    result = subprocess.run(
        cmd,
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout


def get_head_sha(repo_root: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _get_current_branch(repo_root: Path) -> str | None:
    result = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------

def load_last_git_sha(config: dict, project_slug: str) -> str | None:
    """Read last_git_sha for a project from an in-memory config dict."""
    for project in config.get("projects", []):
        if isinstance(project, dict) and project.get("slug") == project_slug:
            return project.get("last_git_sha") or None
    return None


def save_last_git_sha(config_path: Path, project_slug: str, sha: str) -> None:
    """Write last_git_sha for a project to the TOML config file.

    Edits the file in-place: finds the [[projects]] block with matching slug
    and updates or inserts last_git_sha within that block.
    """
    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return

    lines = text.splitlines(keepends=True)
    result: list[str] = []
    i = 0
    n = len(lines)
    found = False

    while i < n:
        line = lines[i]
        result.append(line)

        # Detect the start of a [[projects]] block
        if line.strip() == "[[projects]]":
            block_start = len(result) - 1
            i += 1
            # Collect the block's lines until next [[...]] or EOF
            block: list[str] = []
            while i < n and not lines[i].strip().startswith("[["):
                block.append(lines[i])
                i += 1

            # Check if this block has the matching slug
            slug_line = f'slug = "{project_slug}"'
            if any(slug_line in bl for bl in block):
                found = True
                # Update or insert last_git_sha
                sha_line = f'last_git_sha = "{sha}"\n'
                new_block: list[str] = []
                replaced = False
                for bl in block:
                    if bl.strip().startswith("last_git_sha"):
                        new_block.append(sha_line)
                        replaced = True
                    else:
                        new_block.append(bl)
                if not replaced:
                    # Insert before any trailing blank lines
                    insert_at = len(new_block)
                    while insert_at > 0 and new_block[insert_at - 1].strip() == "":
                        insert_at -= 1
                    new_block.insert(insert_at, sha_line)
                result.extend(new_block)
            else:
                result.extend(block)
            continue

        i += 1

    if not found:
        # Project block not found — append a minimal entry
        if result and result[-1].strip():
            result.append("\n")
        result.append(f'[[projects]]\nslug = "{project_slug}"\nlast_git_sha = "{sha}"\n')

    config_path.write_text("".join(result), encoding="utf-8")


# ---------------------------------------------------------------------------
# Quine write
# ---------------------------------------------------------------------------

# @spec SI-GIT-002, SI-GIT-003
async def write_commit_to_quine(
    commit: CommitRecord,
    client: Any,
    project_slug: str,
) -> None:
    """Write one Commit node, upsert File nodes for touched paths, and write TOUCHES edges."""
    from modok.quine.models import Commit as CommitNode, File as FileNode, TestFile as TestFileNode
    from modok.retrieval.engine import _is_test_path

    file_hunks_json = json.dumps({
        file_path: [
            {"lines": list(h.lines), "function": h.function_context, "defs": h.added_defs}
            for h in hunks
        ]
        for file_path, hunks in commit.file_hunks.items()
        if hunks
    }) if commit.file_hunks else ""

    commit_node = CommitNode(
        node_type="Commit",
        project_slug=project_slug,
        sha=commit.sha,
        timestamp=commit.timestamp,
        author_name=commit.author_name,
        author_email=commit.author_email,
        message=commit.message,
        branch=commit.branch,
        file_hunks=file_hunks_json,
    )
    await client.upsert_node(commit_node)

    for file_path, _change_type in commit.touched_files:
        # SI-GIT-003: upsert a minimal node so the TOUCHES edge always resolves,
        # even when ingest-docs hasn't run yet for this file's feature.
        if _is_test_path(file_path):
            node = TestFileNode(node_type="TestFile", project_slug=project_slug, repo_path=file_path)
            await client.upsert_node(node)
            await client.write_edge_by_parts(
                ("commit", project_slug, commit.sha),
                "TOUCHES",
                ("test-file", project_slug, file_path),
            )
        else:
            node = FileNode(node_type="File", project_slug=project_slug, repo_path=file_path)
            await client.upsert_node(node)
            await client.write_edge_by_parts(
                ("commit", project_slug, commit.sha),
                "TOUCHES",
                ("file", project_slug, file_path),
            )


# ---------------------------------------------------------------------------
# Main ingestion entry point
# ---------------------------------------------------------------------------

async def ingest_git(
    project_slug: str,
    repo_root: Path,
    registry: Any,
    client: Any,
    config: dict,
    *,
    full: bool = False,
    since_date: str | None = None,
    max_commits: int = 500,
    doc_paths: list[str] | None = None,
) -> None:
    """Import git commits touching registered files into Quine as Commit nodes."""
    # SI-GIT-010: --full and --since are mutually exclusive
    if full and since_date:
        print("Error: --full and --since are mutually exclusive", file=sys.stderr)
        raise SystemExit(1)

    # Build registered file set (SI-GIT-004)
    features_raw: dict = {}
    if hasattr(registry, "_features"):
        features_raw = registry._features  # type: ignore[attr-defined]
    arrow_index_path = repo_root / "docs" / "arrows" / "index.yaml"
    arrow_index: dict = {}
    if arrow_index_path.exists():
        import yaml
        arrow_index = yaml.safe_load(arrow_index_path.read_text()) or {}

    registered_files = build_registered_file_set(features_raw, arrow_index, doc_paths=doc_paths)

    # Determine incremental SHA (SI-GIT-007)
    since_sha = load_last_git_sha(config, project_slug)
    head_sha = get_head_sha(repo_root)
    current_branch = _get_current_branch(repo_root)

    # Build and run git log command
    cmd = _build_git_log_command(
        registered_files=registered_files,
        since_sha=since_sha,
        since_date=since_date,
        max_commits=max_commits,
        full=full,
    )
    log_output = _get_git_log(cmd, repo_root)
    commits = parse_commit_log(log_output)

    # Filter to commits that actually touch a registered file (secondary guard)
    def _touches_registered(c: CommitRecord) -> bool:
        return any(fp in registered_files for fp, _ in c.touched_files)

    commits = [c for c in commits if _touches_registered(c)]

    # Stamp branch on each commit
    for c in commits:
        c.branch = current_branch

    # Write all commits, then update last_git_sha (SI-GIT-007 atomicity)
    edges_with_hunks = 0
    defs_found = 0
    for commit in commits:
        commit.file_hunks = get_diff_hunks(commit.sha, repo_root)
        for hunks in commit.file_hunks.values():
            if hunks:
                edges_with_hunks += 1
                defs_found += sum(len(h.added_defs) for h in hunks)
        await write_commit_to_quine(commit, client=client, project_slug=project_slug)

    # Only update after all writes succeed
    if head_sha:
        config_path = Path.home() / ".modok" / "config.toml"
        save_last_git_sha(config_path, project_slug, head_sha)

    return len(commits), edges_with_hunks, defs_found
