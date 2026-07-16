"""
Tests for File-to-dependency usage mapping
(docs/llds/dependency-graph-ingestion.md § File-to-Dependency Usage Mapping).
Written before implementation (Phase 4) — the module these tests target does
not exist yet, so every test here fails with ImportError until Phase 5.

Module name assumption: src/modok/ingestion/dependency_usage.py, a new
sibling to the existing src/modok/ingestion/pipeline.py.

Interface assumptions (Phase 5 may adjust; the behavioral requirements
DEPG-USAGE-*/DEPG-EDGE-007 do not depend on exact names):
  - top_level_module(import_name: str) -> str
  - resolve_import_to_purl(import_module: str, overrides: dict[str, str],
    ecosystem: str = "pypi") -> str | None (None for stdlib modules)
  - load_dependency_map_overrides(repo_root: Path) -> dict[str, str]
    (reads .modok/dependency-map.yml's import_overrides; {} if absent)
  - write_file_dependency_usage_edges(client, project_slug, code_map: dict,
    overrides: dict[str, str] | None = None) -> None
    (code_map is the already-parsed .modok/code-map.yml structure)

Specs verified: DEPG-USAGE-001 through DEPG-USAGE-006, DEPG-EDGE-007.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _mock_client(file_exists: bool = True, package_exists: bool = True) -> MagicMock:
    client = MagicMock()
    client.replace_edges_by_parts = AsyncMock()

    async def _node_exists_by_parts(parts):
        if parts[0] == "file":
            return file_exists
        if parts[0] == "dependency-package":
            return package_exists
        return True

    client.node_exists_by_parts = AsyncMock(side_effect=_node_exists_by_parts)
    return client


def _code_map(files: list[dict]) -> dict:
    return {"project": "stagehand", "files": files}


def _bleak_file_entry(path: str = "client/stagehand_client/stagehand_ble.py") -> dict:
    return {
        "path": path,
        "language": "python",
        "role": "source",
        "imports": [{"module": "bleak", "names": ["BleakClient"]}],
    }


# ---------------------------------------------------------------------------
# DEPG-USAGE-003 — stdlib modules skipped
# ---------------------------------------------------------------------------


# @spec DEPG-USAGE-003
def test_top_level_module_takes_root_package():
    from modok.ingestion.dependency_usage import top_level_module

    assert top_level_module("bleak.backends.scanner") == "bleak"
    assert top_level_module("bleak") == "bleak"


# @spec DEPG-USAGE-003
def test_stdlib_import_resolves_to_none():
    from modok.ingestion.dependency_usage import resolve_import_to_purl

    assert resolve_import_to_purl("os", overrides={}) is None
    assert resolve_import_to_purl("sys", overrides={}) is None
    assert resolve_import_to_purl("pathlib", overrides={}) is None


# ---------------------------------------------------------------------------
# DEPG-USAGE-004 — explicit override vs identity mapping
# ---------------------------------------------------------------------------


# @spec DEPG-USAGE-004
def test_third_party_import_with_no_override_uses_identity_mapping():
    from modok.ingestion.dependency_usage import resolve_import_to_purl

    assert resolve_import_to_purl("bleak", overrides={}) == "pkg:pypi/bleak"


# @spec DEPG-USAGE-004
def test_third_party_import_with_override_uses_override_package_name():
    from modok.ingestion.dependency_usage import resolve_import_to_purl

    result = resolve_import_to_purl("cv2", overrides={"cv2": "opencv-python"})
    assert result == "pkg:pypi/opencv-python"


# @spec DEPG-USAGE-004
def test_load_dependency_map_overrides_returns_empty_dict_when_file_absent(tmp_path):
    from modok.ingestion.dependency_usage import load_dependency_map_overrides

    assert load_dependency_map_overrides(tmp_path) == {}


# @spec DEPG-USAGE-004
def test_load_dependency_map_overrides_reads_import_overrides_section(tmp_path):
    from modok.ingestion.dependency_usage import load_dependency_map_overrides

    modok_dir = tmp_path / ".modok"
    modok_dir.mkdir()
    (modok_dir / "dependency-map.yml").write_text(
        "import_overrides:\n  cv2: opencv-python\n  yaml: PyYAML\n"
    )
    overrides = load_dependency_map_overrides(tmp_path)
    assert overrides == {"cv2": "opencv-python", "yaml": "PyYAML"}


# ---------------------------------------------------------------------------
# DEPG-USAGE-002 — only process existing File nodes; never invent one
# ---------------------------------------------------------------------------


# @spec DEPG-USAGE-002
@pytest.mark.asyncio
async def test_only_files_with_existing_file_node_are_processed():
    from modok.ingestion.dependency_usage import write_file_dependency_usage_edges

    client = _mock_client(file_exists=False)
    code_map = _code_map([_bleak_file_entry()])

    await write_file_dependency_usage_edges(client, "stagehand", code_map)

    client.replace_edges_by_parts.assert_not_awaited()


# @spec DEPG-USAGE-002
@pytest.mark.asyncio
async def test_never_creates_a_file_node():
    """write_file_dependency_usage_edges must not call upsert_node for File —
    that remains ingest_doc's responsibility (docs/llds/ingestion-pipeline.md)."""
    from modok.ingestion.dependency_usage import write_file_dependency_usage_edges

    client = _mock_client(file_exists=True)
    client.upsert_node = AsyncMock()
    code_map = _code_map([_bleak_file_entry()])

    await write_file_dependency_usage_edges(client, "stagehand", code_map)

    for call in client.upsert_node.call_args_list:
        assert call.args[0].node_type != "File"


# ---------------------------------------------------------------------------
# DEPG-USAGE-005 — only edge to an existing DependencyPackage; never invent
# ---------------------------------------------------------------------------


# @spec DEPG-USAGE-005
@pytest.mark.asyncio
async def test_no_edge_when_dependency_package_does_not_exist_yet():
    from modok.ingestion.dependency_usage import write_file_dependency_usage_edges

    client = _mock_client(file_exists=True, package_exists=False)
    code_map = _code_map([_bleak_file_entry()])

    await write_file_dependency_usage_edges(client, "stagehand", code_map)

    call = client.replace_edges_by_parts.call_args
    to_parts_list = call[0][2]
    assert to_parts_list == []


# @spec DEPG-USAGE-005, DEPG-EDGE-007
@pytest.mark.asyncio
async def test_edge_written_when_dependency_package_exists():
    from modok.ingestion.dependency_usage import write_file_dependency_usage_edges

    client = _mock_client(file_exists=True, package_exists=True)
    code_map = _code_map([_bleak_file_entry()])

    await write_file_dependency_usage_edges(client, "stagehand", code_map)

    client.replace_edges_by_parts.assert_awaited()
    from_parts, edge_type, to_parts_list = client.replace_edges_by_parts.call_args[0]
    assert from_parts[0] == "file"
    assert edge_type == "USES_DEPENDENCY"
    assert any(p[0] == "dependency-package" for p in to_parts_list)


# ---------------------------------------------------------------------------
# DEPG-EDGE-007 — reconciled per file, not merely additive
# ---------------------------------------------------------------------------


# @spec DEPG-EDGE-007
@pytest.mark.asyncio
async def test_uses_dependency_reconciled_via_replace_not_write():
    """A file that stops importing a package must lose the edge on the next
    run — this requires replace_edges_by_parts (full reconciliation), not an
    additive write_edge_by_parts call that would leave stale edges behind."""
    from modok.ingestion.dependency_usage import write_file_dependency_usage_edges

    client = _mock_client(file_exists=True, package_exists=True)
    client.write_edge_by_parts = AsyncMock()
    code_map = _code_map([_bleak_file_entry()])

    await write_file_dependency_usage_edges(client, "stagehand", code_map)

    client.replace_edges_by_parts.assert_awaited()
    client.write_edge_by_parts.assert_not_awaited()


# ---------------------------------------------------------------------------
# DEPG-USAGE-006 — Python + File only in v1
# ---------------------------------------------------------------------------


# @spec DEPG-USAGE-006
@pytest.mark.asyncio
async def test_non_python_source_file_skipped():
    from modok.ingestion.dependency_usage import write_file_dependency_usage_edges

    client = _mock_client(file_exists=True, package_exists=True)
    code_map = _code_map([
        {
            "path": "agent/src/main.cs",
            "language": "csharp",
            "role": "source",
            "imports": [{"module": "System.Net.Http", "names": []}],
        }
    ])

    await write_file_dependency_usage_edges(client, "stagehand", code_map)

    client.replace_edges_by_parts.assert_not_awaited()


# @spec DEPG-USAGE-006
@pytest.mark.asyncio
async def test_test_role_file_skipped_in_v1():
    from modok.ingestion.dependency_usage import write_file_dependency_usage_edges

    client = _mock_client(file_exists=True, package_exists=True)
    code_map = _code_map([
        {
            "path": "tests/test_stagehand_ble.py",
            "language": "python",
            "role": "test",
            "imports": [{"module": "bleak", "names": []}],
        }
    ])

    await write_file_dependency_usage_edges(client, "stagehand", code_map)

    client.replace_edges_by_parts.assert_not_awaited()


# ---------------------------------------------------------------------------
# DEPG-USAGE-001 — run once, after the existing per-doc write loop
# ---------------------------------------------------------------------------


# @spec DEPG-USAGE-001
@pytest.mark.asyncio
async def test_run_ingestion_calls_usage_mapping_after_the_per_doc_loop(tmp_path):
    """Additive wiring check on the existing run_ingestion orchestrator
    (docs/llds/ingestion-pipeline.md) — the new call happens once, after the
    existing per-doc loop, not interleaved with it. Mirrors the existing
    run_ingestion test setup in test_ingestion_pipeline.py (MagicMock(spec=
    Registry), AsyncMock client, discover_docs/get_commit_sha patched)."""
    from unittest.mock import patch

    from modok.ingestion.discovery import DocRecord
    from modok.ingestion.pipeline import run_ingestion
    from modok.ingestion.registry import Registry

    call_order: list[str] = []

    async def _fake_ingest_doc(*args, **kwargs):
        call_order.append("ingest_doc")

    async def _fake_usage_mapping(*args, **kwargs):
        call_order.append("usage_mapping")

    doc_path = tmp_path / "doc.md"
    doc_path.write_text("# Doc\n")
    rec = DocRecord(path=doc_path, doc_type="lld", feature="shtp-receiver", tier=2)

    # load_code_map (not patched — it's a plain file read) needs a real,
    # if minimal, .modok/code-map.yml on disk to return non-None so the
    # gated call to write_file_dependency_usage_edges is reached at all —
    # a missing code map is a deliberate no-op (§ File-to-Dependency Usage
    # Mapping), covered separately below.
    modok_dir = tmp_path / ".modok"
    modok_dir.mkdir()
    (modok_dir / "code-map.yml").write_text("project: stagehand\nfiles: []\n")

    registry = MagicMock(spec=Registry)
    client = AsyncMock()

    with patch("modok.ingestion.pipeline.ingest_doc", new=_fake_ingest_doc), patch(
        "modok.ingestion.discovery.discover_docs", return_value=([rec], [], 0)
    ), patch("modok.ingestion.parser.get_commit_sha", return_value="abc123"), patch(
        "modok.ingestion.pipeline.write_file_dependency_usage_edges",
        new=_fake_usage_mapping,
    ):
        await run_ingestion(tmp_path, registry=registry, client=client, project_slug="stagehand")

    assert call_order == ["ingest_doc", "usage_mapping"]


# @spec DEPG-USAGE-001
@pytest.mark.asyncio
async def test_run_ingestion_skips_usage_mapping_when_no_code_map_exists(tmp_path):
    """A missing .modok/code-map.yml is a silent no-op, not an error —
    ingest-docs works before the first extract-code-map run, same as the
    extractor's own optional-artifact precedent (docs/llds/code-map.md)."""
    from unittest.mock import patch

    from modok.ingestion.discovery import DocRecord
    from modok.ingestion.pipeline import run_ingestion
    from modok.ingestion.registry import Registry

    doc_path = tmp_path / "doc.md"
    doc_path.write_text("# Doc\n")
    rec = DocRecord(path=doc_path, doc_type="lld", feature="shtp-receiver", tier=2)

    registry = MagicMock(spec=Registry)
    client = AsyncMock()
    usage_mapping = AsyncMock()

    with patch("modok.ingestion.pipeline.ingest_doc", new=AsyncMock()), patch(
        "modok.ingestion.discovery.discover_docs", return_value=([rec], [], 0)
    ), patch("modok.ingestion.parser.get_commit_sha", return_value="abc123"), patch(
        "modok.ingestion.pipeline.write_file_dependency_usage_edges", new=usage_mapping
    ):
        await run_ingestion(tmp_path, registry=registry, client=client, project_slug="stagehand")

    usage_mapping.assert_not_awaited()
