"""
Git history ingestion — imports commits touching registered files as Commit nodes.

Specs: SI-GIT-001 through SI-GIT-010.
"""
# @spec SI-GIT-001, SI-GIT-002, SI-GIT-003, SI-GIT-004, SI-GIT-005,
#       SI-GIT-006, SI-GIT-007, SI-GIT-008, SI-GIT-009, SI-GIT-010

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class CommitRecord:
    sha: str
    timestamp: str         # ISO-8601 author date
    author_name: str
    author_email: str
    message: str           # first line only, max 120 chars
    branch: str | None     # branch name at ingest time; None if detached HEAD
    touched_files: list[tuple[str, str]] = field(default_factory=list)  # (path, change_type)


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

def build_registered_file_set(features: dict, arrow_index: dict) -> frozenset[str]:
    """Build the set of file paths that qualify for commit filter (SI-GIT-004).

    Includes:
    - source_files from every feature entry in features.yml
    - arrow_doc, lld, and specs paths from the arrow index
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

    if since_sha:
        # Incremental: only commits strictly after last_git_sha
        cmd.append(f"{since_sha}..HEAD")
    elif not full:
        date_limit = since_date if since_date else "6 months ago"
        cmd.extend(["--after", date_limit, f"--max-count={max_commits}"])

    if registered_files:
        cmd.extend(["--"] + sorted(registered_files))

    return cmd


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
    """Write last_git_sha for a project to the TOML config file."""
    try:
        import tomllib
        with open(config_path, "rb") as f:
            config = tomllib.load(f)
    except (FileNotFoundError, Exception):
        config = {}

    projects = config.get("projects", [])
    updated = False
    for proj in projects:
        if isinstance(proj, dict) and proj.get("slug") == project_slug:
            proj["last_git_sha"] = sha
            updated = True
            break
    if not updated:
        projects.append({"slug": project_slug, "last_git_sha": sha})
    config["projects"] = projects

    try:
        import tomli_w
        with open(config_path, "wb") as f:
            tomli_w.dump(config, f)
    except ImportError:
        # Fallback: write a minimal representation
        lines = [f'last_git_sha_{project_slug} = "{sha}"\n']
        with open(config_path, "a") as f:
            f.writelines(lines)


# ---------------------------------------------------------------------------
# Quine write
# ---------------------------------------------------------------------------

async def write_commit_to_quine(
    commit: CommitRecord,
    client: Any,
    project_slug: str,
) -> None:
    """Write one Commit node and its TOUCHES edges to Quine (SI-GIT-002, SI-GIT-003)."""
    commit_node = {
        "node_type": "Commit",
        "project_slug": project_slug,
        "sha": commit.sha,
        "timestamp": commit.timestamp,
        "author_name": commit.author_name,
        "author_email": commit.author_email,
        "message": commit.message,
        "branch": commit.branch,
    }
    await client.upsert_node(commit_node)

    for file_path, change_type in commit.touched_files:
        # SI-GIT-003: only write TOUCHES edge when File node already exists
        file_node = await client.get_node_by_path(file_path)
        if file_node is None:
            continue
        await client.write_edge(
            ("commit", project_slug, commit.sha),
            "TOUCHES",
            ("file", project_slug, file_path),
            {"change_type": change_type},
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

    registered_files = build_registered_file_set(features_raw, arrow_index)

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
    for commit in commits:
        await write_commit_to_quine(commit, client=client, project_slug=project_slug)

    # Only update after all writes succeed
    if head_sha:
        config_path = Path.home() / ".modok" / "config.toml"
        save_last_git_sha(config_path, project_slug, head_sha)
