"""Python symbol and import extraction via stdlib ast."""
# @spec CM-SYM-001, CM-SYM-002, CM-SYM-003, CM-SYM-004, CM-SYM-005, CM-SYM-006

from __future__ import annotations

import ast
from pathlib import Path


def extract_symbols(path: Path) -> dict:
    """Return {symbols, imports} or {parse_error} for a Python source file."""
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {"parse_error": "encoding"}

    try:
        tree = ast.parse(content)
    except SyntaxError:
        return {"parse_error": "syntax"}

    symbols: list[dict] = []
    imports: list[dict] = []

    # Collect all imports anywhere in the module
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append({"module": alias.name, "names": []})
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names = [alias.name for alias in node.names]
                imports.append({"module": node.module, "names": names})

    # Collect top-level classes and functions only
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            symbols.append(
                {
                    "name": node.name,
                    "kind": "class",
                    "line_start": node.lineno,
                    "line_end": node.end_lineno,
                }
            )
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(
                        {
                            "name": child.name,
                            "kind": "method",
                            "parent": node.name,
                            "line_start": child.lineno,
                            "line_end": child.end_lineno,
                        }
                    )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(
                {
                    "name": node.name,
                    "kind": "function",
                    "line_start": node.lineno,
                    "line_end": node.end_lineno,
                }
            )

    symbols.sort(key=lambda s: s["line_start"])
    imports.sort(key=lambda i: i["module"])

    return {"symbols": symbols, "imports": imports}
