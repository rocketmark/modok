"""Role classification for repo files. Priority: generated > test > config > docs > source."""
# @spec CM-ROLE-001, CM-ROLE-002, CM-ROLE-003, CM-ROLE-004, CM-ROLE-005, CM-ROLE-006

from __future__ import annotations

from pathlib import Path

_GENERATED_SUFFIXES = (".g.cs", ".designer.cs", ".generated.h")
_GENERATED_DIRS = {"Generated", "Intermediate"}

_TEST_STEM_PREFIXES = ("test_", "Test")
_TEST_STEM_SUFFIXES = ("_test", "_tests", "Tests", "_props")
_TEST_DIRS = {"tests", "test", "Tests", "Test"}

_CONFIG_EXTS = {
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".ini",
    ".config",
    ".cfg",
    ".csproj",
    ".sln",
    ".uplugin",
    ".uproject",
    ".rules",
    ".service",
    ".timer",
    ".template",  # systemd / udev
    ".patch",  # diff/patch files
    ".plist",  # Apple property lists
    ".spec",  # PyInstaller / RPM spec files
}
_CONFIG_NAMES = {
    ".coveragerc",
    ".gitignore",
    ".gitattributes",
    ".editorconfig",
    "CMakeLists.txt",
    "Makefile",
}
_CONFIG_STEM_PREFIXES = ("requirements",)  # requirements.txt, requirements-dev.txt

_DOCS_EXTS = {".md", ".mdx", ".tla"}  # .tla = TLA+ formal specs
_DOCS_NAMES = {"LICENSE", "LICENCE", "NOTICE", "AUTHORS", "CHANGELOG"}
_DOCS_DIRS = {"spec"}  # TLA+ formal spec trees — all files within are docs/tooling


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
    if name in _CONFIG_NAMES:
        return True
    if name.startswith(".env.") or name == ".env":
        return True
    if path.suffix == ".txt" and any(name.startswith(p) for p in _CONFIG_STEM_PREFIXES):
        return True
    if name.endswith("VERSION") or name == "VERSION":
        return True
    return False


def _is_docs(path: Path) -> bool:
    if path.suffix in _DOCS_EXTS:
        return True
    if path.name in _DOCS_NAMES:
        return True
    return any(part in _DOCS_DIRS for part in path.parts[:-1])


def classify_role(path: Path) -> str:
    if _is_generated(path):
        return "generated"
    if _is_test(path):
        return "test"
    if _is_config(path):
        return "config"
    if _is_docs(path):
        return "docs"
    return "source"
