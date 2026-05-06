from __future__ import annotations
import re
import stat
from pathlib import Path

MODOK_HOOK_START = "# >>> MODOK BEGIN <<<"
MODOK_HOOK_END = "# >>> MODOK END <<<"


def hook_content(project_slug: str, ingestion_paths: list[str]) -> str:
    paths_pattern = "|".join(re.escape(p) for p in ingestion_paths)
    guards = "\n".join(f'  "{p}"' for p in ingestion_paths)
    return f"""\
{MODOK_HOOK_START}
# Auto-installed by modok init — do not edit this section manually.

# ingest-docs: only when doc/registry files changed
_modok_changed=$(git diff --name-only HEAD~1 HEAD 2>/dev/null | grep -E '^({paths_pattern})')
if [ -n "$_modok_changed" ]; then
  modok ingest --project {project_slug} \\
{guards}
fi

# ingest-git: unconditional — registered-file filter is inside the command
modok ingest-git --project {project_slug}
{MODOK_HOOK_END}
"""


def install_post_commit_hook(
    repo_root: Path, project_slug: str, ingestion_paths: list[str]
) -> None:
    hook_path = repo_root / ".git" / "hooks" / "post-commit"
    new_section = hook_content(project_slug, ingestion_paths)

    if hook_path.exists():
        existing = hook_path.read_text(encoding="utf-8")
        if MODOK_HOOK_START in existing:
            # Replace existing MODOK section
            before = existing[: existing.index(MODOK_HOOK_START)]
            after_marker = existing[existing.index(MODOK_HOOK_END) + len(MODOK_HOOK_END) :]
            content = before + new_section + after_marker
        else:
            content = existing.rstrip("\n") + "\n" + new_section
    else:
        content = "#!/bin/sh\n" + new_section

    hook_path.write_text(content, encoding="utf-8")
    # Ensure executable
    current = hook_path.stat().st_mode
    hook_path.chmod(current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
