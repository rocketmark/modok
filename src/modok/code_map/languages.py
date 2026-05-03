"""Language detection from file extension — pure function, no content sniffing."""
# @spec CM-LANG-001, CM-LANG-002, CM-LANG-003

from __future__ import annotations

from pathlib import Path

_EXT_MAP: dict[str, str] = {
    ".py": "python",
    ".cs": "csharp",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".c": "cpp",
    ".h": "cpp",
    ".hpp": "cpp",
    ".md": "markdown",
    ".mdx": "markdown",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".json": "json",
    ".sh": "shell",
    ".bash": "shell",
    ".uasset": "unreal_asset",
    ".umap": "unreal_asset",
    ".uplugin": "unreal_project",
    ".uproject": "unreal_project",
}


def detect_language(path: Path) -> str:
    return _EXT_MAP.get(path.suffix.lower(), "unknown")
