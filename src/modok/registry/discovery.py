# @spec RP-DISC-001, RP-DISC-002, RP-DISC-003
from __future__ import annotations

from pathlib import Path

_IGNORE_DIR_NAMES = frozenset(
    [".git", "node_modules", "bin", "obj", "dist", "build", "coverage", ".vs", "registries"]
)
_IGNORE_SUFFIXES = frozenset([".key", ".pem", ".pfx"])
_IGNORE_NAMES = frozenset([".env"])
_ELIGIBLE_SUFFIXES = frozenset([".md", ".mdx"])


def discover_docs(repo_root: Path) -> tuple[list[Path], int]:
    """Return (eligible_files, ignored_count).

    Eligible files have a .md or .mdx suffix and are not under an ignored directory.
    """
    found: list[Path] = []
    ignored = 0

    for candidate in repo_root.rglob("*"):
        if not candidate.is_file():
            continue
        if _is_ignored(candidate, repo_root):
            if candidate.suffix in _ELIGIBLE_SUFFIXES:
                ignored += 1
            continue
        if candidate.suffix in _ELIGIBLE_SUFFIXES:
            found.append(candidate)

    return found, ignored


def _is_ignored(path: Path, repo_root: Path) -> bool:
    try:
        rel = path.relative_to(repo_root)
    except ValueError:
        return False

    for part in rel.parts:
        if part in _IGNORE_DIR_NAMES:
            return True

    if path.suffix in _IGNORE_SUFFIXES:
        return True
    if path.name in _IGNORE_NAMES:
        return True
    return False
