"""Extract key code identifiers from source files for LLM anchor extraction."""
from __future__ import annotations

import ast
import re
from pathlib import Path

_MAX_ELEMENTS = 25
_C_KEYWORDS = {"if", "for", "while", "switch", "return", "sizeof", "typedef",
               "struct", "enum", "union", "static", "extern", "const", "void"}


def extract_module_elements(source_files: list[str], repo_root: Path) -> list[str]:
    """Return up to _MAX_ELEMENTS key identifiers from the given source files.

    Extracts class names, public and private methods, and class-level attribute
    names (signals, constants). Deduplicates and caps to keep prompt size sane.
    """
    names: list[str] = []
    for rel_path in source_files:
        abs_path = repo_root / rel_path
        if not abs_path.exists():
            continue
        suffix = abs_path.suffix.lower()
        if suffix == ".py":
            names.extend(_extract_python(abs_path))
        elif suffix in (".c", ".h"):
            names.extend(_extract_c(abs_path))
    seen: set[str] = set()
    result: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            result.append(n)
    return result[:_MAX_ELEMENTS]


def _extract_python(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return []
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            if not node.name.startswith("__"):
                names.append(node.name)
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not item.name.startswith("__"):
                        names.append(item.name)
                elif isinstance(item, ast.Assign):
                    for target in item.targets:
                        if isinstance(target, ast.Name) and not target.id.startswith("__"):
                            names.append(target.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("__"):
                names.append(node.name)
    return names


def _extract_c(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    pattern = re.compile(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(')
    seen: set[str] = set()
    names: list[str] = []
    for m in pattern.finditer(text):
        name = m.group(1)
        if name not in _C_KEYWORDS and not name.startswith("_") and name not in seen:
            seen.add(name)
            names.append(name)
    return names
