"""Repo scanner: walks files, applies ignore rules, classifies and hashes each entry."""
# @spec CM-SCAN-001, CM-SCAN-002, CM-SCAN-003, CM-SCAN-004, CM-SCAN-005, CM-SCAN-006
# @spec CM-FILE-001, CM-FILE-002, CM-FILE-003, CM-FILE-004
# @spec CM-TEST-001, CM-TEST-002, CM-TEST-003, CM-TEST-004, CM-TEST-005

from __future__ import annotations

import fnmatch
import hashlib
from pathlib import Path

from modok.code_map.languages import detect_language
from modok.code_map.python_ast import extract_symbols
from modok.code_map.roles import classify_role

_IGNORED_DIRS = {
    ".git", "node_modules", "bin", "obj", "dist", "build",
    "coverage", ".vs", "__pycache__", ".modok",
}
_IGNORED_EXTS = {".pyc", ".pyo", ".key", ".pem", ".pfx"}
_IGNORED_FILES = {".env"}
_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
_BINARY_LANGUAGES = {"unreal_asset", "unreal_project"}


def _load_gitignore_patterns(repo_root: Path) -> list[str]:
    gitignore = repo_root / ".gitignore"
    if not gitignore.is_file():
        return []
    patterns = []
    for line in gitignore.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(line)
    return patterns


def _matches_gitignore(rel_posix: str, patterns: list[str]) -> bool:
    name = rel_posix.rsplit("/", 1)[-1]
    for pattern in patterns:
        p = pattern.lstrip("/")
        if p.endswith("/"):
            # Directory pattern: match any path that starts with it
            if rel_posix.startswith(p) or (rel_posix + "/").startswith(p):
                return True
        else:
            if fnmatch.fnmatch(rel_posix, p) or fnmatch.fnmatch(name, p):
                return True
    return False


def _is_statically_ignored(path: Path, repo_root: Path) -> bool:
    rel = path.relative_to(repo_root)
    parts = rel.parts
    # Any parent directory named in the ignore set
    for part in parts[:-1]:
        if part in _IGNORED_DIRS:
            return True
    name = path.name
    if name in _IGNORED_FILES:
        return True
    if path.suffix in _IGNORED_EXTS:
        return True
    return False


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _count_lines(path: Path) -> int:
    try:
        return len(path.read_bytes().splitlines())
    except OSError:
        return 0


def _mirrored_path_covers(test_path: str, source_paths: set[str]) -> list[dict]:
    filename = test_path.rsplit("/", 1)[-1]
    if "." in filename:
        stem, ext = filename.rsplit(".", 1)
        ext = "." + ext
    else:
        stem, ext = filename, ""

    if stem.startswith("test_"):
        base = stem[5:] + ext
    elif stem.endswith("_test"):
        base = stem[:-5] + ext
    else:
        base = filename

    covers = []
    for sp in sorted(source_paths):
        if sp == base or sp.endswith("/" + base):
            covers.append({"path": sp, "method": "mirrored_path"})
    return covers


def _import_covers(imports: list[dict], source_paths: set[str]) -> list[dict]:
    covers = []
    for imp in imports:
        module = imp["module"]
        names = imp.get("names", [])

        # Try module itself as a file: e.g. "foo.bar" -> "foo/bar.py"
        module_file = module.replace(".", "/") + ".py"
        for sp in sorted(source_paths):
            if sp == module_file or sp.endswith("/" + module_file):
                covers.append({"path": sp, "method": "import_reference"})

        # Try module/name.py for "from X import Y": e.g. "src" + "parser" -> "src/parser.py"
        for name in names:
            sub_file = module.replace(".", "/") + "/" + name + ".py"
            for sp in sorted(source_paths):
                if sp == sub_file or sp.endswith("/" + sub_file):
                    covers.append({"path": sp, "method": "import_reference"})

    return covers


def scan_repo(repo_root: Path) -> list[dict]:
    gitignore_patterns = _load_gitignore_patterns(repo_root)

    entries: list[dict] = []
    # Maps test file path -> parsed imports (used for coverage, not stored in entry)
    test_imports: dict[str, list[dict]] = {}

    all_paths = sorted(
        repo_root.rglob("*"),
        key=lambda p: p.relative_to(repo_root).as_posix(),
    )

    for path in all_paths:
        # Skip regular directories (not symlinks)
        if path.is_dir() and not path.is_symlink():
            continue

        rel_posix = path.relative_to(repo_root).as_posix()

        # Static ignore rules (silent drop)
        if _is_statically_ignored(path, repo_root):
            continue

        # Gitignore rules (silent drop)
        if gitignore_patterns and _matches_gitignore(rel_posix, gitignore_patterns):
            continue

        # Symlinks: record as ignored, do not follow
        if path.is_symlink():
            entries.append({"path": rel_posix, "role": "ignored", "symlink": True})
            continue

        # Large files: record as ignored
        try:
            size = path.stat().st_size
        except OSError:
            continue

        if size > _MAX_FILE_SIZE:
            entries.append({"path": rel_posix, "role": "ignored", "skipped": "file too large"})
            continue

        language = detect_language(path)
        role = classify_role(path)
        sha256 = _hash_file(path)

        entry: dict = {
            "path": rel_posix,
            "sha256": sha256,
            "language": language,
            "role": role,
        }

        if language not in _BINARY_LANGUAGES:
            entry["line_count"] = _count_lines(path)

        if language == "python" and role == "source":
            sym_result = extract_symbols(path)
            if "parse_error" in sym_result:
                entry["parse_error"] = sym_result["parse_error"]
            else:
                entry["symbols"] = sym_result["symbols"]
                entry["imports"] = sym_result["imports"]
        elif language == "python" and role == "test":
            # Parse imports for coverage detection only; not stored in entry
            try:
                sym_result = extract_symbols(path)
                if "parse_error" not in sym_result:
                    test_imports[rel_posix] = sym_result["imports"]
            except Exception:
                pass

        entries.append(entry)

    # Sort by path (already sorted from rglob, but enforce determinism)
    entries.sort(key=lambda e: e["path"])

    # Coverage detection pass
    source_paths = {e["path"] for e in entries if e.get("role") == "source"}

    for entry in entries:
        if entry.get("role") != "test":
            continue

        test_path = entry["path"]
        covers: list[dict] = []

        covers.extend(_mirrored_path_covers(test_path, source_paths))

        if entry.get("language") == "python":
            imports = test_imports.get(test_path, [])
            covers.extend(_import_covers(imports, source_paths))

        # Deduplicate by (path, method), sort by path
        seen: set[tuple[str, str]] = set()
        unique: list[dict] = []
        for c in sorted(covers, key=lambda x: x["path"]):
            key = (c["path"], c["method"])
            if key not in seen:
                seen.add(key)
                unique.append(c)

        entry["covers"] = unique

    return entries
