"""File-to-dependency usage mapping — mechanically maps a file's imports
(from the existing code map, docs/llds/code-map.md § Symbol Extraction) to
declared DependencyPackage nodes. Runs during ingest-docs (local), not the
GitHub poller — imports are local source facts, not GitHub data. See
docs/llds/dependency-graph-ingestion.md § File-to-Dependency Usage Mapping."""
# @spec DEPG-USAGE-001, DEPG-USAGE-002, DEPG-USAGE-003, DEPG-USAGE-004,
#       DEPG-USAGE-005, DEPG-USAGE-006, DEPG-EDGE-007

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

from modok.ingestion.dependency_ingestion import build_purl

_CODE_MAP_PATH = Path(".modok") / "code-map.yml"
_DEPENDENCY_MAP_PATH = Path(".modok") / "dependency-map.yml"


def top_level_module(import_name: str) -> str:
    return import_name.split(".", 1)[0]


# @spec DEPG-USAGE-003, DEPG-USAGE-004
def resolve_import_to_purl(
    import_module: str, overrides: dict[str, str], ecosystem: str = "pypi"
) -> str | None:
    top = top_level_module(import_module)
    if top in sys.stdlib_module_names:
        return None
    package_name = overrides.get(top, top)
    return build_purl(ecosystem, package_name)


def load_dependency_map_overrides(repo_root: Path) -> dict[str, str]:
    path = Path(repo_root) / _DEPENDENCY_MAP_PATH
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    overrides = data.get("import_overrides", {})
    return overrides if isinstance(overrides, dict) else {}


def load_code_map(repo_root: Path) -> dict | None:
    path = Path(repo_root) / _CODE_MAP_PATH
    if not path.exists():
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or None
    except Exception:
        return None


# @spec DEPG-USAGE-001, DEPG-USAGE-002, DEPG-USAGE-005, DEPG-USAGE-006, DEPG-EDGE-007
async def write_file_dependency_usage_edges(
    client: Any,
    project_slug: str,
    code_map: dict,
    overrides: dict[str, str] | None = None,
) -> None:
    overrides = overrides or {}
    for entry in code_map.get("files", []):
        # DEPG-USAGE-006 — v1 scope: File (role=source) and Python only.
        if entry.get("role") != "source" or entry.get("language") != "python":
            continue
        path = entry.get("path", "")
        if not path:
            continue
        # DEPG-USAGE-002 — never invent a File node; skip entirely if absent.
        if not await client.node_exists_by_parts(("file", project_slug, path)):
            continue

        target_purls: set[str] = set()
        for imp in entry.get("imports", []):
            module = imp.get("module", "")
            if not module:
                continue
            purl = resolve_import_to_purl(module, overrides)
            if purl is None:
                continue
            # DEPG-USAGE-005 — never invent a DependencyPackage node; a
            # package with no known manifest declaration gets no edge yet.
            if await client.node_exists_by_parts(("dependency-package", project_slug, purl)):
                target_purls.add(purl)

        to_parts_list = [
            ("dependency-package", project_slug, purl) for purl in sorted(target_purls)
        ]
        # DEPG-EDGE-007 — reconciled (not merely additive) so a file that
        # stops importing a package loses the edge on the next run.
        await client.replace_edges_by_parts(
            ("file", project_slug, path), "USES_DEPENDENCY", to_parts_list
        )
