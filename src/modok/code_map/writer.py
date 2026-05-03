"""YAML artifact writer for the code map."""
# @spec CM-OUT-001, CM-OUT-002, CM-OUT-003, CM-OUT-005, CM-OUT-006

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml


def write_code_map(
    out: Path,
    project: str,
    repo_root: Path,
    entries: list[dict],
    git_commit: str | None,
) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)

    sorted_entries = sorted(entries, key=lambda e: e["path"])

    artifact = {
        "project": project,
        "repo_root": str(repo_root),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git_commit": git_commit,
        "files": sorted_entries,
    }

    out.write_text(
        yaml.dump(artifact, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
