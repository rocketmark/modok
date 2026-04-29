from __future__ import annotations
from pathlib import Path

MODOK_HOOK_START = "# >>> MODOK BEGIN <<<"
MODOK_HOOK_END = "# >>> MODOK END <<<"


def install_post_commit_hook(repo_root: Path, project_slug: str, ingestion_paths: list[str]) -> None:
    raise NotImplementedError


def hook_content(project_slug: str, ingestion_paths: list[str]) -> str:
    raise NotImplementedError
