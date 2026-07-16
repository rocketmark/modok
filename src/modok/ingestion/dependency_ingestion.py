"""Dependency-graph ingestion — extends the existing 30-second GitHub poller
with its own cursor to track a project's dependency package/version topology,
manifest provenance, and historical dependency-version changes. See
docs/llds/dependency-graph-ingestion.md."""
# @spec DEPG-SRC-001, DEPG-SRC-002, DEPG-SRC-003, DEPG-SRC-004,
#       DEPG-DETECT-001, DEPG-DETECT-002, DEPG-DETECT-003,
#       DEPG-PARSE-001, DEPG-PARSE-002, DEPG-PARSE-003, DEPG-PARSE-004,
#       DEPG-DIFF-001, DEPG-DIFF-002, DEPG-DIFF-003, DEPG-DIFF-004, DEPG-DIFF-005,
#       DEPG-EDGE-001, DEPG-EDGE-002, DEPG-EDGE-003, DEPG-EDGE-004, DEPG-EDGE-005,
#       DEPG-EDGE-006, DEPG-POLL-001, DEPG-POLL-002, DEPG-POLL-003, DEPG-POLL-004,
#       DEPG-POLL-005, DEPG-POLL-006, DEPG-RECON-001, DEPG-ERR-001, DEPG-ERR-002,
#       DEPG-ERR-003, DEPG-SCOPE-001

from __future__ import annotations

import base64
import fnmatch
import re
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from modok.ingestion.ci_ingestion import _gh_headers, _with_retry_async
from modok.ingestion.git_history import _update_project_config_field
from modok.quine.models import (
    DependencyChange,
    DependencyManifest,
    DependencyPackage,
    DependencySnapshot,
    DependencyVersion,
)

_API_BASE = "https://api.github.com"

# ---------------------------------------------------------------------------
# DEPG-DETECT-001/002/003 — tracked-manifest detection (static table, mirrors
# the code map's own language-detection table — deterministic, no sniffing).
# ---------------------------------------------------------------------------

_MANIFEST_PATTERNS: list[tuple[str, str]] = [
    ("requirements*.txt", "pypi"),
    ("pyproject.toml", "pypi"),
    ("Pipfile", "pypi"),
    ("Pipfile.lock", "pypi"),
    ("poetry.lock", "pypi"),
    ("uv.lock", "pypi"),
    ("package.json", "npm"),
    ("package-lock.json", "npm"),
    ("yarn.lock", "npm"),
    ("*.csproj", "nuget"),
    ("packages.config", "nuget"),
    ("Directory.Packages.props", "nuget"),
    ("Gemfile", "rubygems"),
    ("Gemfile.lock", "rubygems"),
    ("go.mod", "go"),
    ("go.sum", "go"),
    ("Cargo.toml", "cargo"),
    ("Cargo.lock", "cargo"),
]

# v1 scope: only pypi manifests are actually parsed (§ File Format Parsing).
# Every other ecosystem above is detected (so a touching PR isn't silently
# ignored) but not parsed — mirrors code-map.md's Python-only precedent.
_PARSEABLE_ECOSYSTEMS = {"pypi"}


def manifest_ecosystem_for_path(path: str) -> str | None:
    filename = path.rsplit("/", 1)[-1]
    for pattern, ecosystem in _MANIFEST_PATTERNS:
        if fnmatch.fnmatch(filename, pattern):
            return ecosystem
    return None


def is_manifest_path(path: str, globs: list[str] | None = None) -> bool:
    if manifest_ecosystem_for_path(path) is None:
        return False
    if globs is None:
        return True
    return any(fnmatch.fnmatch(path, glob) for glob in globs)


def is_parseable_ecosystem(ecosystem: str | None) -> bool:
    return ecosystem in _PARSEABLE_ECOSYSTEMS


# ---------------------------------------------------------------------------
# DEPG-PARSE-004 — PEP 503 name normalization + purl construction
# ---------------------------------------------------------------------------

_NORMALIZE_SEPARATORS_RE = re.compile(r"[-_.]+")


def normalize_package_name(name: str) -> str:
    return _NORMALIZE_SEPARATORS_RE.sub("-", name).lower()


def build_purl(ecosystem: str, name: str) -> str:
    return f"pkg:{ecosystem}/{normalize_package_name(name)}"


# ---------------------------------------------------------------------------
# DEPG-PARSE-001/002/003 — v1 (pypi) manifest parsing
# ---------------------------------------------------------------------------

# Package name, optional extras, optional comparator+version spec. A line
# that doesn't start with a valid package-name character (directives like
# "-r other.txt", "-e .", "--index-url ..." all start with "-") never
# matches, so directives fall out naturally rather than needing special-casing.
_REQUIREMENT_LINE_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)"  # package name
    r"(\[[^\]]*\])?"  # optional extras
    r"\s*"
    r"([=<>!~].*)?$"  # optional comparator + version spec
)


def _parse_requirement_line(line: str) -> tuple[str, str] | None:
    line = line.split("#", 1)[0].strip()
    if not line:
        return None
    match = _REQUIREMENT_LINE_RE.match(line)
    if not match:
        return None
    name = match.group(1)
    version = (match.group(3) or "").split(";", 1)[0].strip()
    return name, version


def parse_requirements_txt(content: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in content.splitlines():
        parsed = _parse_requirement_line(line)
        if parsed is None:
            continue
        name, version = parsed
        result[name] = version
    return result


def parse_pyproject_dependencies(content: str) -> dict[str, str]:
    try:
        data = tomllib.loads(content)
    except Exception:
        return {}
    deps = data.get("project", {}).get("dependencies", [])
    result: dict[str, str] = {}
    for entry in deps:
        parsed = _parse_requirement_line(str(entry))
        if parsed is None:
            continue
        name, version = parsed
        result[name] = version
    return result


def _manifest_format_for_path(path: str) -> str:
    filename = path.rsplit("/", 1)[-1]
    if filename == "pyproject.toml":
        return "pyproject-pep621"
    if filename.startswith("requirements") and filename.endswith(".txt"):
        return "requirements-txt"
    return "unknown"


def parse_manifest(path: str, content: str) -> dict[str, str] | None:
    ecosystem = manifest_ecosystem_for_path(path)
    if ecosystem is None or not is_parseable_ecosystem(ecosystem):
        return None
    fmt = _manifest_format_for_path(path)
    if fmt == "pyproject-pep621":
        return parse_pyproject_dependencies(content)
    if fmt == "requirements-txt":
        return parse_requirements_txt(content)
    return None


# ---------------------------------------------------------------------------
# DEPG-SRC-001/002/003/004 — version-fidelity source priority
# ---------------------------------------------------------------------------

# Dependabot's title wording varies by ecosystem/updater, not just by
# package — found live against a real repo's pip-ecosystem PRs, which read
# "Update X requirement from A to B in /path" rather than "Bump X from A to
# B" (the format most other ecosystems use). Both are checked; the pip
# "Update ... requirement" form only fires when Dependabot couldn't resolve
# an exact pinned version and is instead reporting the requirement range
# itself changing.
_DEPENDABOT_TITLE_PATTERNS = [
    re.compile(
        r"^Bump\s+(?P<package>[A-Za-z0-9][A-Za-z0-9._-]*)\s+from\s+(?P<from>\S+)\s+to\s+(?P<to>\S+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"^Update\s+(?P<package>[A-Za-z0-9][A-Za-z0-9._-]*)\s+requirement\s+from\s+"
        r"(?P<from>\S+)\s+to\s+(?P<to>\S+)",
        re.IGNORECASE,
    ),
]


def parse_dependabot_bump_title(title: str, package_name: str) -> tuple[str, str] | None:
    stripped = (title or "").strip()
    normalized_target = normalize_package_name(package_name)
    for pattern in _DEPENDABOT_TITLE_PATTERNS:
        match = pattern.match(stripped)
        if match and normalize_package_name(match.group("package")) == normalized_target:
            return match.group("from"), match.group("to")
    return None


def resolve_version_for_change(
    package_name: str,
    *,
    raw_from: str | None,
    raw_to: str | None,
    review_data: dict,
    is_dependabot: bool,
    pr_title: str,
) -> tuple[str | None, str | None, str, str]:
    """Priority order (§ Data Sources): dependency-review API result (source
    1, refines only the TO version and relationship — source 1's response is
    scoped to the resulting state of the compare, not the prior state) ->
    Dependabot PR title (source 2, gives both exact from/to) -> raw
    manifest-declared text already captured by the snapshot diff (source 3,
    the floor — always available)."""
    normalized = normalize_package_name(package_name)
    for candidate_name, entry in (review_data or {}).items():
        if normalize_package_name(candidate_name) == normalized:
            resolved_to = entry.get("version") or raw_to
            relationship = entry.get("relationship") or "unknown"
            return raw_from, resolved_to, relationship, "dependency_review"

    if is_dependabot:
        parsed = parse_dependabot_bump_title(pr_title, package_name)
        if parsed is not None:
            return parsed[0], parsed[1], "unknown", "dependabot_title"

    return raw_from, raw_to, "unknown", "manifest_diff"


# ---------------------------------------------------------------------------
# DEPG-DIFF-002/003/004 — prior-snapshot lookup and package-set diffing
# ---------------------------------------------------------------------------


async def _snapshot_packages(
    client: Any, project_slug: str, manifest_path: str, commit_sha: str
) -> dict[str, str]:
    rows = await client.query(
        "MATCH (s) WHERE id(s) = idFrom('dependency-snapshot', $p, $m, $c) "
        "MATCH (s)-[:CONTAINS]->(v) RETURN v",
        {"p": project_slug, "m": manifest_path, "c": commit_sha},
    )
    result: dict[str, str] = {}
    for row in rows:
        if not row or not row[0]:
            continue
        props = row[0].get("properties", {})
        purl = props.get("package_purl", "")
        name = purl.rsplit("/", 1)[-1] if purl else ""
        if name:
            result[name] = props.get("version", "")
    return result


# @spec DEPG-DIFF-002
async def find_prior_snapshot(
    client: Any, project_slug: str, manifest_path: str, captured_at: str
) -> dict | None:
    rows = await client.query(
        "MATCH (n) WHERE n.node_type = 'DependencySnapshot' AND n.project_slug = $project_slug "
        "AND n.manifest_path = $manifest_path AND n.captured_at < $captured_at "
        "RETURN n ORDER BY n.captured_at DESC, n.commit_sha DESC LIMIT 1",
        {
            "project_slug": project_slug,
            "manifest_path": manifest_path,
            "captured_at": captured_at,
        },
    )
    if not rows or not rows[0]:
        return None
    props = rows[0][0].get("properties", {})
    packages = await _snapshot_packages(
        client, project_slug, manifest_path, props.get("commit_sha", "")
    )
    return {"properties": props, "packages": packages}


# @spec DEPG-DIFF-003, DEPG-DIFF-004
def diff_manifest_packages(
    prior: dict[str, str] | None, new: dict[str, str]
) -> list[dict]:
    # DEPG-DIFF-003: no prior snapshot -> every package is "first observed",
    # not "changed". Writing a change record for each would flood the graph
    # the moment a repo starts being tracked.
    if prior is None:
        return []
    result: list[dict] = []
    for name in sorted(set(prior) | set(new)):
        old_version = prior.get(name)
        new_version = new.get(name)
        if old_version is None:
            result.append({"package": name, "change_kind": "added", "from": None, "to": new_version})
        elif new_version is None:
            result.append({"package": name, "change_kind": "removed", "from": old_version, "to": None})
        elif old_version != new_version:
            result.append(
                {"package": name, "change_kind": "changed", "from": old_version, "to": new_version}
            )
        # else: unchanged, no record
    return result


# ---------------------------------------------------------------------------
# DEPG-EDGE-001..006 — node/edge writers
# ---------------------------------------------------------------------------


async def _upsert_dependency_version(
    client: Any, project_slug: str, purl: str, version: str, relationship: str
) -> None:
    await client.upsert_node(
        DependencyVersion(
            node_type="DependencyVersion",
            project_slug=project_slug,
            package_purl=purl,
            version=version,
            relationship=relationship,
        )
    )
    # @spec DEPG-EDGE-001 — written at every DependencyVersion creation site,
    # immutable thereafter (idempotent MERGE means repeated writes are a no-op).
    await client.write_edge_by_parts(
        ("dependency-version", project_slug, purl, version),
        "VERSION_OF",
        ("dependency-package", project_slug, purl),
    )


async def write_dependency_snapshot(
    client: Any,
    project_slug: str,
    manifest_path: str,
    *,
    commit_sha: str,
    captured_at: str,
    packages: dict[str, str],
    ecosystem: str = "pypi",
) -> None:
    manifest_parts = ("dependency-manifest", project_slug, manifest_path)
    await client.upsert_node(
        DependencyManifest(
            node_type="DependencyManifest",
            project_slug=project_slug,
            manifest_path=manifest_path,
            ecosystem=ecosystem,
            format=_manifest_format_for_path(manifest_path),
        )
    )

    await client.upsert_node(
        DependencySnapshot(
            node_type="DependencySnapshot",
            project_slug=project_slug,
            manifest_path=manifest_path,
            commit_sha=commit_sha,
            captured_at=captured_at,
        )
    )
    snapshot_parts = ("dependency-snapshot", project_slug, manifest_path, commit_sha)

    version_parts_list: list[tuple[str, ...]] = []
    for name, version in packages.items():
        purl = build_purl(ecosystem, name)
        await client.upsert_node(
            DependencyPackage(
                node_type="DependencyPackage",
                project_slug=project_slug,
                purl=purl,
                ecosystem=ecosystem,
                name=normalize_package_name(name),
            )
        )
        await _upsert_dependency_version(client, project_slug, purl, version, "unknown")
        version_parts = ("dependency-version", project_slug, purl, version)
        # @spec DEPG-EDGE-002 — one CONTAINS edge per package declared in
        # this snapshot; written once, never removed.
        await client.write_edge_by_parts(snapshot_parts, "CONTAINS", version_parts)
        version_parts_list.append(version_parts)

    # @spec DEPG-EDGE-003 — never invent a Commit node.
    commit_parts = ("commit", project_slug, commit_sha)
    if await client.node_exists_by_parts(commit_parts):
        await client.write_edge_by_parts(snapshot_parts, "FOR_COMMIT", commit_parts)

    # @spec DEPG-EDGE-006 — current-state pointer, reconciled to exactly this
    # snapshot's CONTAINS set every time a new snapshot is written.
    await client.replace_edges_by_parts(manifest_parts, "DECLARES", version_parts_list)


async def write_dependency_change(
    client: Any,
    project_slug: str,
    manifest_path: str,
    *,
    commit_sha: str,
    package_name: str,
    ecosystem: str,
    change_kind: str,
    from_version: str | None,
    to_version: str | None,
    relationship: str,
    version_source: str,
    fix_id: str | None = None,
) -> None:
    purl = build_purl(ecosystem, package_name)
    await client.upsert_node(
        DependencyPackage(
            node_type="DependencyPackage",
            project_slug=project_slug,
            purl=purl,
            ecosystem=ecosystem,
            name=normalize_package_name(package_name),
        )
    )

    await client.upsert_node(
        DependencyChange(
            node_type="DependencyChange",
            project_slug=project_slug,
            manifest_path=manifest_path,
            package_purl=purl,
            commit_sha=commit_sha,
            change_kind=change_kind,
            version_source=version_source,
            observed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )
    change_parts = ("dependency-change", project_slug, manifest_path, purl, commit_sha)

    await client.write_edge_by_parts(
        change_parts, "CHANGED_PACKAGE", ("dependency-package", project_slug, purl)
    )

    # @spec DEPG-EDGE-004 — omission by change_kind
    if change_kind != "removed" and to_version is not None:
        await _upsert_dependency_version(client, project_slug, purl, to_version, relationship)
        await client.write_edge_by_parts(
            change_parts, "TO_VERSION", ("dependency-version", project_slug, purl, to_version)
        )
    if change_kind != "added" and from_version is not None:
        await _upsert_dependency_version(client, project_slug, purl, from_version, relationship)
        await client.write_edge_by_parts(
            change_parts, "FROM_VERSION", ("dependency-version", project_slug, purl, from_version)
        )

    # @spec DEPG-EDGE-005 — never invent a Commit or Fix node.
    commit_parts = ("commit", project_slug, commit_sha)
    if await client.node_exists_by_parts(commit_parts):
        await client.write_edge_by_parts(change_parts, "INTRODUCED_BY", commit_parts)

    # @spec DEPG-NODE-006 — MERGED_VIA points at the existing Fix node; no
    # separate PullRequest node type is ever created.
    if fix_id:
        fix_parts = ("fix", project_slug, fix_id)
        if await client.node_exists_by_parts(fix_parts):
            await client.write_edge_by_parts(change_parts, "MERGED_VIA", fix_parts)


# @spec DEPG-RECON-001
async def reconcile_dependency_change_edges(client: Any, project_slug: str) -> None:
    """Once per poll cycle, per project, independent of any cursor: fix up
    INTRODUCED_BY/MERGED_VIA for any DependencyChange whose target now
    exists. Re-derives the candidate Fix via the existing
    Fix -[:IMPLEMENTED_IN]-> Commit edge (docs/llds/github-ingestion.md)
    rather than requiring a stored fix_id on DependencyChange itself."""
    rows = await client.query(
        "MATCH (n) WHERE n.node_type = 'DependencyChange' AND n.project_slug = $p RETURN n",
        {"p": project_slug},
    )
    for row in rows:
        if not row or not row[0]:
            continue
        props = row[0].get("properties", {})
        manifest_path = props.get("manifest_path", "")
        purl = props.get("package_purl", "")
        commit_sha = props.get("commit_sha", "")
        if not (manifest_path and purl and commit_sha):
            continue
        change_parts = ("dependency-change", project_slug, manifest_path, purl, commit_sha)
        commit_parts = ("commit", project_slug, commit_sha)

        if await client.node_exists_by_parts(commit_parts):
            await client.write_edge_by_parts(change_parts, "INTRODUCED_BY", commit_parts)

        fix_rows = await client.query(
            "MATCH (fix)-[:IMPLEMENTED_IN]->(c) WHERE id(c) = idFrom('commit', $p, $sha) RETURN fix",
            {"p": project_slug, "sha": commit_sha},
        )
        for fix_row in fix_rows:
            if not fix_row or not fix_row[0]:
                continue
            fix_id = fix_row[0].get("properties", {}).get("fix_id")
            if fix_id:
                await client.write_edge_by_parts(
                    change_parts, "MERGED_VIA", ("fix", project_slug, fix_id)
                )


# ---------------------------------------------------------------------------
# GitHub API fetch helpers — thin, real implementations; patched out in tests.
# ---------------------------------------------------------------------------


async def fetch_merged_prs_since(github_repo: str, token: str, since: str | None) -> list[dict]:
    prs: list[dict] = []
    page = 1
    async with httpx.AsyncClient(base_url=_API_BASE, headers=_gh_headers(token)) as http:
        while True:
            resp = await _with_retry_async(
                http,
                f"/repos/{github_repo}/pulls",
                params={
                    "state": "closed",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": 100,
                    "page": page,
                },
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            stop = False
            for pr in batch:
                if not pr.get("merged_at"):
                    continue
                if since and pr.get("updated_at", "") <= since:
                    stop = True
                    break
                prs.append(pr)
            if stop or len(batch) < 100:
                break
            page += 1
    return prs


async def _fetch_pr_files(github_repo: str, token: str, pr_number: int) -> list[dict]:
    files: list[dict] = []
    page = 1
    async with httpx.AsyncClient(base_url=_API_BASE, headers=_gh_headers(token)) as http:
        while True:
            resp = await _with_retry_async(
                http,
                f"/repos/{github_repo}/pulls/{pr_number}/files",
                params={"per_page": 100, "page": page},
            )
            resp.raise_for_status()
            batch = resp.json()
            files.extend(batch)
            if len(batch) < 100:
                break
            page += 1
    return files


# @spec DEPG-DIFF-001, DEPG-ERR-002
async def _fetch_manifest_content(
    github_repo: str, token: str, path: str, commit_sha: str
) -> str | None:
    async with httpx.AsyncClient(base_url=_API_BASE, headers=_gh_headers(token)) as http:
        resp = await _with_retry_async(
            http, f"/repos/{github_repo}/contents/{path}", params={"ref": commit_sha}
        )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    content = data.get("content", "")
    if data.get("encoding") == "base64":
        return base64.b64decode(content).decode("utf-8", errors="replace")
    return content


async def _fetch_dependency_review(
    github_repo: str, token: str, base_sha: str, head_sha: str
) -> dict:
    """Returns {manifest_path: {package_name: {"version":..., "relationship":...}}}
    — an empty dict on any non-2xx or malformed response (DEPG-SRC-002:
    treated as unavailable, never raises to the caller)."""
    if not base_sha or not head_sha:
        return {}
    try:
        async with httpx.AsyncClient(base_url=_API_BASE, headers=_gh_headers(token)) as http:
            resp = await _with_retry_async(
                http, f"/repos/{github_repo}/dependency-graph/compare/{base_sha}...{head_sha}"
            )
        if resp.status_code != 200:
            return {}
        entries = resp.json()
    except Exception:
        return {}

    result: dict[str, dict] = {}
    for entry in entries:
        manifest = entry.get("manifest", "")
        name = entry.get("name", "")
        if not manifest or not name:
            continue
        result.setdefault(manifest, {})[name] = {
            "version": entry.get("version", ""),
            "relationship": entry.get("relationship") or "unknown",
        }
    return result


def _pr_captured_at(pr: dict) -> str:
    return pr.get("merged_at") or pr.get("updated_at") or ""


# @spec DEPG-POLL-005, DEPG-ERR-001, DEPG-ERR-002, DEPG-SCOPE-001
async def process_merged_pr_for_dependencies(
    client: Any,
    project_slug: str,
    github_repo: str,
    token: str,
    pr: dict,
    *,
    manifest_globs: list[str] | None = None,
) -> bool:
    """Returns True if any tracked manifest in this PR was processed (a
    snapshot or a removal was written), False for a clean no-manifest-touched
    no-op. Raises on a genuine fetch failure — the caller (§ Poll Cycle) uses
    that to freeze cursor advancement, not this function."""
    pr_number = pr["number"]
    files = await _fetch_pr_files(github_repo, token, pr_number)
    touched_manifests = [
        f["filename"]
        for f in files
        if is_manifest_path(f.get("filename", ""), globs=manifest_globs)
        and is_parseable_ecosystem(manifest_ecosystem_for_path(f.get("filename", "")))
    ]
    if not touched_manifests:
        return False

    merge_commit_sha = pr.get("merge_commit_sha") or ""
    is_dependabot = pr.get("user", {}).get("login") == "dependabot[bot]"
    pr_title = pr.get("title", "")
    fix_id = f"gh-{pr_number}"
    captured_at = _pr_captured_at(pr)

    any_processed = False
    for manifest_path in touched_manifests:
        ecosystem = manifest_ecosystem_for_path(manifest_path) or "pypi"
        prior = await find_prior_snapshot(client, project_slug, manifest_path, captured_at)
        content = await _fetch_manifest_content(github_repo, token, manifest_path, merge_commit_sha)

        if content is None:
            # @spec DEPG-ERR-002 — deleted/renamed manifest (404 at the merge
            # commit): every package in the prior snapshot is "removed"; no
            # new DependencySnapshot is written (there is no content to snapshot).
            if prior:
                for pkg_name, pkg_version in prior["packages"].items():
                    await write_dependency_change(
                        client,
                        project_slug,
                        manifest_path,
                        commit_sha=merge_commit_sha,
                        package_name=pkg_name,
                        ecosystem=ecosystem,
                        change_kind="removed",
                        from_version=pkg_version,
                        to_version=None,
                        relationship="unknown",
                        version_source="manifest_diff",
                        fix_id=fix_id,
                    )
                any_processed = True
            continue

        new_packages = parse_manifest(manifest_path, content) or {}
        await write_dependency_snapshot(
            client,
            project_slug,
            manifest_path,
            commit_sha=merge_commit_sha,
            captured_at=captured_at,
            packages=new_packages,
            ecosystem=ecosystem,
        )
        any_processed = True

        prior_packages = prior["packages"] if prior else None
        diff = diff_manifest_packages(prior_packages, new_packages)
        if not diff:
            continue

        review_data = await _fetch_dependency_review(
            github_repo, token, pr.get("base", {}).get("sha", ""), merge_commit_sha
        )
        review_for_manifest = review_data.get(manifest_path, {})

        for entry in diff:
            from_v, to_v, relationship, version_source = resolve_version_for_change(
                entry["package"],
                raw_from=entry["from"],
                raw_to=entry["to"],
                review_data=review_for_manifest,
                is_dependabot=is_dependabot,
                pr_title=pr_title,
            )
            await write_dependency_change(
                client,
                project_slug,
                manifest_path,
                commit_sha=merge_commit_sha,
                package_name=entry["package"],
                ecosystem=ecosystem,
                change_kind=entry["change_kind"],
                from_version=from_v,
                to_version=to_v,
                relationship=relationship,
                version_source=version_source,
                fix_id=fix_id,
            )

    return any_processed


def save_last_dependency_sync(config_path: Path, project_slug: str, timestamp: str) -> None:
    """Write last_dependency_sync for a project to the TOML config file
    in-place — its own cursor, independent of last_github_sync/
    last_workflow_sync (§ Polling and Checkpoint Behavior)."""
    _update_project_config_field(config_path, project_slug, "last_dependency_sync", timestamp)


# @spec DEPG-POLL-001, DEPG-POLL-002, DEPG-POLL-003, DEPG-POLL-004, DEPG-POLL-006
async def run_dependency_ingestion_cycle(
    client: Any,
    project_slug: str,
    github_repo: str,
    token: str,
    *,
    since: str | None,
    config_path: Path,
) -> str:
    """Every fetched PR is attempted this cycle regardless of an earlier
    PR's outcome (best-effort). The cursor, however, only ever advances
    through a strictly successful prefix from the start of the batch: once
    one PR fails, the cursor stops advancing for the rest of the cycle, even
    if later PRs in the same batch succeed — this is what guarantees the
    failed PR is refetched next cycle rather than silently skipped once the
    cursor moves past its timestamp."""
    try:
        prs = await fetch_merged_prs_since(github_repo, token, since)
    except Exception as exc:
        print(f"dependency-ingestion: {project_slug} — PR fetch failed: {exc}", file=sys.stderr)
        return ""

    if not prs:
        return ""

    prs_sorted = sorted(prs, key=lambda pr: pr.get("updated_at", ""))
    cursor_frozen = False
    manifest_touched_count = 0

    for pr in prs_sorted:
        try:
            touched = await process_merged_pr_for_dependencies(
                client, project_slug, github_repo, token, pr
            )
            if touched:
                manifest_touched_count += 1
            if not cursor_frozen:
                save_last_dependency_sync(config_path, project_slug, pr.get("updated_at", ""))
        except Exception as exc:
            cursor_frozen = True
            print(
                f"dependency-ingestion: {project_slug} — PR {pr.get('number')} failed: {exc}",
                file=sys.stderr,
            )

    if not manifest_touched_count:
        return ""
    return f", {manifest_touched_count} dependency manifest(s) updated"
