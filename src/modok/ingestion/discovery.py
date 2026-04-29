from __future__ import annotations
from pathlib import Path

IGNORE_PATTERNS = [
    ".git",
    "node_modules",
    "bin",
    "obj",
    "dist",
    "build",
    "coverage",
    ".vs",
]

IGNORE_SUFFIXES = [".key", ".pem", ".pfx"]
IGNORE_NAMES = [".env"]
SUPPORTED_SUFFIXES = {".md", ".mdx", ".yaml", ".yml"}


def discover_files(root: Path) -> tuple[list[Path], int]:
    """Return (ingestible_files, ignored_count)."""
    raise NotImplementedError


def has_modok_frontmatter(path: Path) -> bool:
    raise NotImplementedError
