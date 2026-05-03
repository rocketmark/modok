"""
Tests for modok.code_map — deterministic repo code map extractor.
All tests written before implementation (Phase 5). Tests cite specs via @spec annotation.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from unittest.mock import patch

import shutil
import tempfile

import pytest
import yaml
from hypothesis import given, settings
import hypothesis.strategies as st


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def make_repo(tmp_path: Path) -> Path:
    """Minimal repo with a few files of different types."""
    write(tmp_path / "src" / "engine.py", "def run(): pass\n")
    write(tmp_path / "src" / "utils.py", "class Helper: pass\n")
    write(tmp_path / "tests" / "test_engine.py", "import src.engine\ndef test_run(): pass\n")
    write(tmp_path / "README.md", "# Project\n")
    write(tmp_path / "config.toml", "[settings]\n")
    return tmp_path


# ---------------------------------------------------------------------------
# CM-CMD-001 — command interface
# ---------------------------------------------------------------------------

# @spec CM-CMD-001
def test_extract_code_map_command_exists():
    from modok.cli.main import cli
    assert "extract-code-map" in [cmd.name for cmd in cli.commands.values()]


# @spec CM-CMD-003
def test_extract_code_map_bad_repo_exits_nonzero(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    runner = CliRunner()
    result = runner.invoke(cli, ["extract-code-map", "--project", "x", "--repo", str(tmp_path / "nonexistent")])
    assert result.exit_code != 0
    assert "not a directory" in result.output.lower()


# @spec CM-CMD-004
def test_extract_code_map_prints_summary_on_success(tmp_path):
    from click.testing import CliRunner
    from modok.cli.main import cli

    make_repo(tmp_path)
    out_path = tmp_path / ".modok" / "code-map.yml"

    runner = CliRunner()
    result = runner.invoke(cli, [
        "extract-code-map", "--project", "x",
        "--repo", str(tmp_path),
        "--output", str(out_path),
    ])
    assert result.exit_code == 0
    assert "Extracted code map:" in result.output
    assert str(out_path) in result.output


# ---------------------------------------------------------------------------
# CM-SCAN-001, CM-SCAN-002 — scanning and ignore rules
# ---------------------------------------------------------------------------

# @spec CM-SCAN-001
def test_scanner_finds_regular_files(tmp_path):
    from modok.code_map.scanner import scan_repo

    write(tmp_path / "src" / "main.py", "x = 1")
    write(tmp_path / "README.md", "# hi")

    entries = scan_repo(tmp_path)
    paths = [e["path"] for e in entries]
    assert "src/main.py" in paths
    assert "README.md" in paths


# @spec CM-SCAN-002
def test_scanner_skips_git_directory(tmp_path):
    from modok.code_map.scanner import scan_repo

    write(tmp_path / ".git" / "config", "[core]")
    write(tmp_path / "src" / "main.py", "x = 1")

    entries = scan_repo(tmp_path)
    paths = [e["path"] for e in entries]
    assert not any(".git" in p for p in paths)


# @spec CM-SCAN-002
def test_scanner_skips_node_modules(tmp_path):
    from modok.code_map.scanner import scan_repo

    write(tmp_path / "node_modules" / "lib" / "index.js", "module.exports = {}")
    write(tmp_path / "src" / "app.ts", "export {}")

    entries = scan_repo(tmp_path)
    paths = [e["path"] for e in entries]
    assert not any("node_modules" in p for p in paths)
    assert "src/app.ts" in paths


# @spec CM-SCAN-002
@pytest.mark.parametrize("ignored_name", [
    "secret.key", "cert.pem", "cert.pfx", ".env",
])
def test_scanner_skips_sensitive_files(tmp_path, ignored_name):
    from modok.code_map.scanner import scan_repo

    write(tmp_path / ignored_name, "sensitive")
    write(tmp_path / "src" / "main.py", "x = 1")

    entries = scan_repo(tmp_path)
    paths = [e["path"] for e in entries]
    assert ignored_name not in paths


# @spec CM-SCAN-004
def test_scanner_records_symlinks_as_ignored(tmp_path):
    from modok.code_map.scanner import scan_repo

    target = write(tmp_path / "real.py", "x = 1")
    link = tmp_path / "link.py"
    link.symlink_to(target)

    entries = scan_repo(tmp_path)
    link_entry = next((e for e in entries if e["path"] == "link.py"), None)
    assert link_entry is not None
    assert link_entry["role"] == "ignored"
    assert link_entry.get("symlink") is True


# @spec CM-SCAN-005
def test_scanner_records_large_files_as_ignored(tmp_path):
    from modok.code_map.scanner import scan_repo

    large = tmp_path / "huge.bin"
    large.write_bytes(b"x" * (11 * 1024 * 1024))  # 11 MB

    entries = scan_repo(tmp_path)
    entry = next((e for e in entries if e["path"] == "huge.bin"), None)
    assert entry is not None
    assert entry["role"] == "ignored"
    assert entry.get("skipped") == "file too large"
    assert "sha256" not in entry


# @spec CM-SCAN-006
def test_scanner_is_deterministic(tmp_path):
    from modok.code_map.scanner import scan_repo

    make_repo(tmp_path)
    run1 = scan_repo(tmp_path)
    run2 = scan_repo(tmp_path)
    assert run1 == run2


# ---------------------------------------------------------------------------
# CM-LANG-001, CM-LANG-002, CM-LANG-003 — language detection
# ---------------------------------------------------------------------------

# @spec CM-LANG-001, CM-LANG-003
@pytest.mark.parametrize("filename,expected_lang", [
    ("main.py", "python"),
    ("engine.cs", "csharp"),
    ("app.ts", "typescript"),
    ("component.tsx", "typescript"),
    ("index.js", "javascript"),
    ("main.go", "go"),
    ("lib.rs", "rust"),
    ("engine.cpp", "cpp"),
    ("header.h", "cpp"),
    ("notes.md", "markdown"),
    ("config.yaml", "yaml"),
    ("config.yml", "yaml"),
    ("settings.toml", "toml"),
    ("data.json", "json"),
    ("deploy.sh", "shell"),
    ("Asset.uasset", "unreal_asset"),
    ("Level.umap", "unreal_asset"),
    ("Plugin.uplugin", "unreal_project"),
])
def test_language_detection(filename, expected_lang):
    from modok.code_map.languages import detect_language
    assert detect_language(Path(filename)) == expected_lang


# @spec CM-LANG-002
def test_language_detection_unknown_extension():
    from modok.code_map.languages import detect_language
    assert detect_language(Path("file.xyz123")) == "unknown"


# ---------------------------------------------------------------------------
# CM-ROLE-001 through CM-ROLE-006 — role classification
# ---------------------------------------------------------------------------

# @spec CM-ROLE-002
@pytest.mark.parametrize("path", [
    "src/Generated/Foo.cs",
    "src/Intermediate/Bar.cs",
    "Engine.g.cs",
    "Form.designer.cs",
    "Api.generated.h",
])
def test_role_generated(path):
    from modok.code_map.roles import classify_role
    assert classify_role(Path(path)) == "generated"


# @spec CM-ROLE-003
@pytest.mark.parametrize("path", [
    "tests/test_engine.py",
    "test_utils.py",
    "engine_test.py",
    "Tests/TestEngine.cs",
    "EngineTests.cs",
    "test/helpers.py",
])
def test_role_test(path):
    from modok.code_map.roles import classify_role
    assert classify_role(Path(path)) == "test"


# @spec CM-ROLE-004
@pytest.mark.parametrize("path", [
    "config.toml",
    "settings.yaml",
    "package.json",
    "Project.csproj",
    "Solution.sln",
    "Plugin.uplugin",
    "Game.uproject",
    ".env.production",
])
def test_role_config(path):
    from modok.code_map.roles import classify_role
    assert classify_role(Path(path)) == "config"


# @spec CM-ROLE-005
def test_role_docs():
    from modok.code_map.roles import classify_role
    assert classify_role(Path("docs/design.md")) == "docs"
    assert classify_role(Path("README.mdx")) == "docs"


# @spec CM-ROLE-006
def test_role_source_fallback():
    from modok.code_map.roles import classify_role
    assert classify_role(Path("src/engine.cs")) == "source"
    assert classify_role(Path("src/main.py")) == "source"


# @spec CM-ROLE-001
def test_role_priority_generated_beats_test(tmp_path):
    # A file named test_foo.g.cs should be generated, not test
    from modok.code_map.roles import classify_role
    assert classify_role(Path("test_foo.g.cs")) == "generated"


# ---------------------------------------------------------------------------
# CM-FILE-001 through CM-FILE-004 — file facts
# ---------------------------------------------------------------------------

# @spec CM-FILE-001
def test_file_entry_contains_required_fields(tmp_path):
    from modok.code_map.scanner import scan_repo

    write(tmp_path / "src" / "main.py", "x = 1\n")
    entries = scan_repo(tmp_path)
    entry = next(e for e in entries if e["path"] == "src/main.py")

    assert "path" in entry
    assert "sha256" in entry
    assert "language" in entry
    assert "role" in entry
    assert "line_count" in entry


# @spec CM-FILE-001
def test_pattern_ignored_files_not_in_output(tmp_path):
    from modok.code_map.scanner import scan_repo

    write(tmp_path / ".git" / "HEAD", "ref: refs/heads/main")
    write(tmp_path / "src" / "main.py", "x = 1")

    entries = scan_repo(tmp_path)
    assert not any(".git" in e["path"] for e in entries)


# @spec CM-FILE-002
def test_binary_language_omits_line_count(tmp_path):
    from modok.code_map.scanner import scan_repo

    asset = tmp_path / "Content" / "Level.uasset"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"\x00\x01\x02\x03")

    entries = scan_repo(tmp_path)
    entry = next((e for e in entries if e["path"] == "Content/Level.uasset"), None)
    assert entry is not None
    assert "line_count" not in entry


# @spec CM-FILE-003
def test_paths_are_repo_relative_posix(tmp_path):
    from modok.code_map.scanner import scan_repo

    write(tmp_path / "src" / "deep" / "file.py", "x = 1")
    entries = scan_repo(tmp_path)
    entry = next(e for e in entries if "file.py" in e["path"])

    assert not entry["path"].startswith("/")
    assert not entry["path"].startswith("./")
    assert "\\" not in entry["path"]
    assert entry["path"] == "src/deep/file.py"


# @spec CM-FILE-004
def test_sha256_is_stable(tmp_path):
    from modok.code_map.scanner import scan_repo

    content = "hello world\n"
    write(tmp_path / "file.py", content)

    entries1 = scan_repo(tmp_path)
    entries2 = scan_repo(tmp_path)

    h1 = next(e["sha256"] for e in entries1 if e["path"] == "file.py")
    h2 = next(e["sha256"] for e in entries2 if e["path"] == "file.py")
    assert h1 == h2

    expected = hashlib.sha256(content.encode()).hexdigest()
    assert h1 == expected


# ---------------------------------------------------------------------------
# CM-SYM-001 through CM-SYM-006 — Python symbol extraction
# ---------------------------------------------------------------------------

# @spec CM-SYM-001
def test_symbol_extraction_finds_functions_and_classes(tmp_path):
    from modok.code_map.python_ast import extract_symbols

    src = tmp_path / "engine.py"
    src.write_text(
        "class Engine:\n"
        "    def start(self): pass\n"
        "\n"
        "def helper(): pass\n",
        encoding="utf-8",
    )

    result = extract_symbols(src)
    kinds = {s["name"]: s["kind"] for s in result["symbols"]}
    assert kinds["Engine"] == "class"
    assert kinds["start"] == "method"
    assert kinds["helper"] == "function"


# @spec CM-SYM-001
def test_symbol_extraction_records_parent_for_methods(tmp_path):
    from modok.code_map.python_ast import extract_symbols

    src = tmp_path / "engine.py"
    src.write_text("class Foo:\n    def bar(self): pass\n", encoding="utf-8")

    result = extract_symbols(src)
    method = next(s for s in result["symbols"] if s["name"] == "bar")
    assert method["parent"] == "Foo"


# @spec CM-SYM-002
def test_symbol_extraction_finds_imports(tmp_path):
    from modok.code_map.python_ast import extract_symbols

    src = tmp_path / "main.py"
    src.write_text(
        "import os\n"
        "from pathlib import Path, PurePath\n",
        encoding="utf-8",
    )

    result = extract_symbols(src)
    mods = {i["module"]: i["names"] for i in result["imports"]}
    assert "os" in mods
    assert mods["os"] == []
    assert "pathlib" in mods
    assert "Path" in mods["pathlib"]
    assert "PurePath" in mods["pathlib"]


# @spec CM-SYM-003
def test_symbol_extraction_encoding_error(tmp_path):
    from modok.code_map.python_ast import extract_symbols

    src = tmp_path / "bad.py"
    src.write_bytes(b"\xff\xfe invalid utf-8 \x80\x81")

    result = extract_symbols(src)
    assert result.get("parse_error") == "encoding"
    assert "symbols" not in result
    assert "imports" not in result


# @spec CM-SYM-004
def test_symbol_extraction_syntax_error(tmp_path):
    from modok.code_map.python_ast import extract_symbols

    src = tmp_path / "broken.py"
    src.write_text("def foo(:\n    pass\n", encoding="utf-8")

    result = extract_symbols(src)
    assert result.get("parse_error") == "syntax"
    assert "symbols" not in result
    assert "imports" not in result


# @spec CM-SYM-005
def test_non_python_files_have_no_symbols_key(tmp_path):
    from modok.code_map.scanner import scan_repo

    write(tmp_path / "Engine.cs", "class Engine {}")
    entries = scan_repo(tmp_path)
    entry = next(e for e in entries if e["path"] == "Engine.cs")
    assert "symbols" not in entry
    assert "imports" not in entry


# @spec CM-SYM-001 — Python test files excluded from symbol extraction
def test_python_test_files_have_no_symbols(tmp_path):
    from modok.code_map.scanner import scan_repo

    write(tmp_path / "tests" / "test_engine.py", "def test_run(): assert True\n")
    entries = scan_repo(tmp_path)
    entry = next(e for e in entries if "test_engine" in e["path"])
    assert entry["role"] == "test"
    assert "symbols" not in entry


# @spec CM-SYM-006
def test_symbol_extraction_is_deterministic(tmp_path):
    from modok.code_map.python_ast import extract_symbols

    src = tmp_path / "app.py"
    src.write_text(
        "import os\nfrom pathlib import Path\nclass Foo:\n    def bar(self): pass\n",
        encoding="utf-8",
    )

    r1 = extract_symbols(src)
    r2 = extract_symbols(src)
    assert r1 == r2


# ---------------------------------------------------------------------------
# CM-TEST-001 through CM-TEST-005 — test coverage detection
# ---------------------------------------------------------------------------

# @spec CM-TEST-001
def test_coverage_mirrored_path(tmp_path):
    from modok.code_map.scanner import scan_repo

    write(tmp_path / "src" / "engine.py", "def run(): pass\n")
    write(tmp_path / "tests" / "test_engine.py", "def test_run(): pass\n")

    entries = scan_repo(tmp_path)
    test_entry = next(e for e in entries if e["path"] == "tests/test_engine.py")
    covers_paths = [c["path"] for c in test_entry.get("covers", [])]
    assert "src/engine.py" in covers_paths
    assert any(c["method"] == "mirrored_path" for c in test_entry.get("covers", []))


# @spec CM-TEST-002
def test_coverage_records_all_mirrored_matches(tmp_path):
    from modok.code_map.scanner import scan_repo

    write(tmp_path / "src" / "engine.py", "x = 1")
    write(tmp_path / "lib" / "engine.py", "x = 2")
    write(tmp_path / "tests" / "test_engine.py", "def test_run(): pass\n")

    entries = scan_repo(tmp_path)
    test_entry = next(e for e in entries if e["path"] == "tests/test_engine.py")
    mirrored = [c for c in test_entry.get("covers", []) if c["method"] == "mirrored_path"]
    assert len(mirrored) >= 2


# @spec CM-TEST-003
def test_coverage_python_import_reference(tmp_path):
    from modok.code_map.scanner import scan_repo

    write(tmp_path / "src" / "parser.py", "def parse(): pass\n")
    write(tmp_path / "tests" / "test_misc.py", "from src import parser\ndef test_x(): pass\n")

    entries = scan_repo(tmp_path)
    test_entry = next(e for e in entries if "test_misc" in e["path"])
    import_covers = [c for c in test_entry.get("covers", []) if c["method"] == "import_reference"]
    assert any("parser.py" in c["path"] for c in import_covers)


# @spec CM-TEST-005
def test_coverage_empty_when_no_match(tmp_path):
    from modok.code_map.scanner import scan_repo

    write(tmp_path / "tests" / "test_orphan.py", "def test_nothing(): pass\n")

    entries = scan_repo(tmp_path)
    test_entry = next(e for e in entries if "test_orphan" in e["path"])
    assert test_entry.get("covers", []) == []


# ---------------------------------------------------------------------------
# CM-OUT-001 through CM-OUT-006 — output artifact
# ---------------------------------------------------------------------------

# @spec CM-OUT-001
def test_output_is_valid_yaml(tmp_path):
    from modok.code_map.writer import write_code_map

    make_repo(tmp_path)
    out = tmp_path / ".modok" / "code-map.yml"

    from modok.code_map.scanner import scan_repo
    entries = scan_repo(tmp_path)
    write_code_map(out, project="x", repo_root=tmp_path, entries=entries, git_commit=None)

    parsed = yaml.safe_load(out.read_text())
    assert isinstance(parsed, dict)
    assert "files" in parsed


# @spec CM-OUT-002
def test_output_contains_required_top_level_fields(tmp_path):
    from modok.code_map.scanner import scan_repo
    from modok.code_map.writer import write_code_map

    make_repo(tmp_path)
    out = tmp_path / ".modok" / "code-map.yml"
    entries = scan_repo(tmp_path)
    write_code_map(out, project="testproj", repo_root=tmp_path, entries=entries, git_commit="abc123")

    parsed = yaml.safe_load(out.read_text())
    assert parsed["project"] == "testproj"
    assert "repo_root" in parsed
    assert "generated_at" in parsed
    assert parsed["git_commit"] == "abc123"
    assert "files" in parsed


# @spec CM-OUT-003
def test_output_files_sorted_by_path(tmp_path):
    from modok.code_map.scanner import scan_repo
    from modok.code_map.writer import write_code_map

    write(tmp_path / "z_file.py", "")
    write(tmp_path / "a_file.py", "")
    write(tmp_path / "m_file.py", "")

    out = tmp_path / "code-map.yml"
    entries = scan_repo(tmp_path)
    write_code_map(out, project="x", repo_root=tmp_path, entries=entries, git_commit=None)

    parsed = yaml.safe_load(out.read_text())
    paths = [f["path"] for f in parsed["files"]]
    assert paths == sorted(paths)


# @spec CM-OUT-004
def test_output_git_commit_null_when_not_git_repo(tmp_path):
    from modok.code_map.scanner import scan_repo
    from modok.code_map.writer import write_code_map
    from modok.code_map.git import get_head_sha

    # tmp_path is not a git repo
    sha = get_head_sha(tmp_path)
    assert sha is None

    write(tmp_path / "file.py", "")
    out = tmp_path / "code-map.yml"
    entries = scan_repo(tmp_path)
    write_code_map(out, project="x", repo_root=tmp_path, entries=entries, git_commit=sha)

    parsed = yaml.safe_load(out.read_text())
    assert parsed["git_commit"] is None


# @spec CM-OUT-005
def test_output_byte_identical_on_rerun(tmp_path):
    from modok.code_map.scanner import scan_repo
    from modok.code_map.writer import write_code_map

    make_repo(tmp_path)
    out = tmp_path / ".modok" / "code-map.yml"  # .modok/ is excluded by scanner

    entries1 = scan_repo(tmp_path)
    write_code_map(out, project="x", repo_root=tmp_path, entries=entries1, git_commit=None)
    first = out.read_text()

    entries2 = scan_repo(tmp_path)
    write_code_map(out, project="x", repo_root=tmp_path, entries=entries2, git_commit=None)
    second = out.read_text()

    # Strip generated_at line before comparing
    def strip_timestamp(text):
        return "\n".join(l for l in text.splitlines() if "generated_at" not in l)

    assert strip_timestamp(first) == strip_timestamp(second)


# @spec CM-OUT-006
def test_output_empty_files_list_when_no_files(tmp_path):
    from modok.code_map.writer import write_code_map

    out = tmp_path / "code-map.yml"
    write_code_map(out, project="x", repo_root=tmp_path, entries=[], git_commit=None)

    parsed = yaml.safe_load(out.read_text())
    assert parsed["files"] == []


# ---------------------------------------------------------------------------
# Property tests (hypothesis) — specs marked [P]
# ---------------------------------------------------------------------------

# @spec CM-SCAN-006 [P]
@given(
    names=st.lists(
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=15),
        min_size=1, max_size=8, unique=True,
    )
)
@settings(max_examples=15)
def test_property_scanner_deterministic(names):
    from modok.code_map.scanner import scan_repo

    tmp = Path(tempfile.mkdtemp())
    try:
        for name in names:
            write(tmp / f"{name}.py", "x = 1")
        run1 = scan_repo(tmp)
        run2 = scan_repo(tmp)
        assert run1 == run2
    finally:
        shutil.rmtree(tmp)


# @spec CM-LANG-003 [P]
@given(filename=st.from_regex(r"[a-z]{1,15}\.[a-z]{1,6}", fullmatch=True))
@settings(max_examples=50)
def test_property_language_detection_pure(filename):
    from modok.code_map.languages import detect_language

    path = Path(filename)
    assert detect_language(path) == detect_language(path)


# @spec CM-FILE-004 [P]
@given(content=st.binary(min_size=0, max_size=1024))
@settings(max_examples=25)
def test_property_sha256_stable_for_any_content(content):
    from modok.code_map.scanner import scan_repo

    tmp = Path(tempfile.mkdtemp())
    try:
        (tmp / "target.bin").write_bytes(content)
        e1 = next((e for e in scan_repo(tmp) if e.get("sha256")), None)
        e2 = next((e for e in scan_repo(tmp) if e.get("sha256")), None)
        assert e1 is not None
        assert e1["sha256"] == e2["sha256"]
    finally:
        shutil.rmtree(tmp)


# @spec CM-OUT-003 [P]
@given(
    paths=st.lists(
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz_/.", min_size=1, max_size=25),
        min_size=0, max_size=12, unique=True,
    )
)
@settings(max_examples=25)
def test_property_output_always_sorted(paths):
    import random
    from modok.code_map.writer import write_code_map

    entries = [
        {"path": p, "sha256": "a" * 64, "language": "python", "role": "source", "line_count": 1}
        for p in paths
    ]
    random.shuffle(entries)

    tmp = Path(tempfile.mkdtemp())
    try:
        out = tmp / "code-map.yml"
        write_code_map(out, project="x", repo_root=tmp, entries=entries, git_commit=None)
        parsed = yaml.safe_load(out.read_text())
        result_paths = [f["path"] for f in parsed["files"]]
        assert result_paths == sorted(result_paths)
    finally:
        shutil.rmtree(tmp)


# @spec CM-SYM-006 [P]
@given(
    src=st.text(
        alphabet="abcdefghijklmnopqrstuvwxyz _():\n=.\"'0123456789",
        min_size=0,
        max_size=200,
    )
)
@settings(max_examples=25)
def test_property_symbol_extraction_deterministic(src):
    from modok.code_map.python_ast import extract_symbols

    tmp = Path(tempfile.mkdtemp())
    try:
        f = tmp / "module.py"
        f.write_text(src, encoding="utf-8")
        r1 = extract_symbols(f)
        r2 = extract_symbols(f)
        assert r1 == r2
    finally:
        shutil.rmtree(tmp)
