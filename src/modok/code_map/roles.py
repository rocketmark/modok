"""Role classification for repo files. Priority: generated > test > config > docs > source."""
# @spec CM-ROLE-001, CM-ROLE-002, CM-ROLE-003, CM-ROLE-004, CM-ROLE-005, CM-ROLE-006

from __future__ import annotations

from pathlib import Path

_GENERATED_SUFFIXES = (".g.cs", ".designer.cs", ".generated.h")
_GENERATED_DIRS = {"Generated", "Intermediate"}

_TEST_STEM_PREFIXES = ("test_", "Test")
_TEST_STEM_SUFFIXES = ("_test", "Tests")
_TEST_DIRS = {"tests", "test", "Tests", "Test"}

_CONFIG_EXTS = {
    ".toml", ".yaml", ".yml", ".json", ".ini", ".config",
    ".csproj", ".sln", ".uplugin", ".uproject",
}
_DOCS_EXTS = {".md", ".mdx"}


def _is_generated(path: Path) -> bool:
    name = path.name
    for suffix in _GENERATED_SUFFIXES:
        if name.endswith(suffix):
            return True
    return any(part in _GENERATED_DIRS for part in path.parts[:-1])


def _is_test(path: Path) -> bool:
    stem = path.stem
    if any(stem.startswith(p) for p in _TEST_STEM_PREFIXES):
        return True
    if any(stem.endswith(s) for s in _TEST_STEM_SUFFIXES):
        return True
    return any(part in _TEST_DIRS for part in path.parts[:-1])


def _is_config(path: Path) -> bool:
    if path.suffix in _CONFIG_EXTS:
        return True
    name = path.name
    return name.startswith(".env.") or name == ".env"


def classify_role(path: Path) -> str:
    if _is_generated(path):
        return "generated"
    if _is_test(path):
        return "test"
    if _is_config(path):
        return "config"
    if path.suffix in _DOCS_EXTS:
        return "docs"
    return "source"
