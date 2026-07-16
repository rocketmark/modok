"""
Tests for the new Quine node types introduced by Dependency-Graph Ingestion
(docs/llds/dependency-graph-ingestion.md § Graph Model and Deterministic IDs).
Written before implementation (Phase 4) — none of these types exist yet, so
every test here fails with ImportError until Phase 5.

Specs verified: DEPG-NODE-001 through DEPG-NODE-006.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# DEPG-NODE-001 — DependencyPackage
# ---------------------------------------------------------------------------


# @spec DEPG-NODE-001
def test_dependency_package_model_has_required_fields():
    from modok.quine.models import DependencyPackage

    node = DependencyPackage(
        node_type="DependencyPackage",
        project_slug="stagehand",
        purl="pkg:pypi/bleak",
        ecosystem="pypi",
        name="bleak",
    )
    assert node.purl == "pkg:pypi/bleak"
    assert node.ecosystem == "pypi"
    assert node.name == "bleak"


# @spec DEPG-NODE-001
def test_dependency_package_idfrom_key():
    from modok.quine.client import _idFrom_cypher_args
    from modok.quine.models import DependencyPackage

    node = DependencyPackage(
        node_type="DependencyPackage",
        project_slug="stagehand",
        purl="pkg:pypi/bleak",
        ecosystem="pypi",
        name="bleak",
    )
    args, params = _idFrom_cypher_args(node)
    assert "dependency-package" in args
    assert params["idf_project_slug"] == "stagehand"
    assert params["idf_purl"] == "pkg:pypi/bleak"


# ---------------------------------------------------------------------------
# DEPG-NODE-002 — DependencyVersion
# ---------------------------------------------------------------------------


# @spec DEPG-NODE-002
def test_dependency_version_model_has_required_fields():
    from modok.quine.models import DependencyVersion

    node = DependencyVersion(
        node_type="DependencyVersion",
        project_slug="stagehand",
        package_purl="pkg:pypi/bleak",
        version="0.22.0",
        relationship="direct",
    )
    assert node.package_purl == "pkg:pypi/bleak"
    assert node.version == "0.22.0"
    assert node.relationship == "direct"


# @spec DEPG-NODE-002
def test_dependency_version_idfrom_key_includes_package_and_version():
    from modok.quine.client import _idFrom_cypher_args
    from modok.quine.models import DependencyVersion

    node = DependencyVersion(
        node_type="DependencyVersion",
        project_slug="stagehand",
        package_purl="pkg:pypi/bleak",
        version="0.22.0",
        relationship="unknown",
    )
    args, params = _idFrom_cypher_args(node)
    assert "dependency-version" in args
    assert params["idf_package_purl"] == "pkg:pypi/bleak"
    assert params["idf_version"] == "0.22.0"


# ---------------------------------------------------------------------------
# DEPG-NODE-003 — DependencyManifest
# ---------------------------------------------------------------------------


# @spec DEPG-NODE-003
def test_dependency_manifest_model_has_required_fields():
    from modok.quine.models import DependencyManifest

    node = DependencyManifest(
        node_type="DependencyManifest",
        project_slug="stagehand",
        manifest_path="client/requirements.txt",
        ecosystem="pypi",
        format="requirements-txt",
    )
    assert node.manifest_path == "client/requirements.txt"
    assert node.format == "requirements-txt"


# @spec DEPG-NODE-003
def test_dependency_manifest_idfrom_key():
    from modok.quine.client import _idFrom_cypher_args
    from modok.quine.models import DependencyManifest

    node = DependencyManifest(
        node_type="DependencyManifest",
        project_slug="stagehand",
        manifest_path="client/requirements.txt",
        ecosystem="pypi",
        format="requirements-txt",
    )
    args, params = _idFrom_cypher_args(node)
    assert "dependency-manifest" in args
    assert params["idf_manifest_path"] == "client/requirements.txt"


# ---------------------------------------------------------------------------
# DEPG-NODE-004 — DependencySnapshot
# ---------------------------------------------------------------------------


# @spec DEPG-NODE-004
def test_dependency_snapshot_model_has_required_fields():
    from modok.quine.models import DependencySnapshot

    node = DependencySnapshot(
        node_type="DependencySnapshot",
        project_slug="stagehand",
        manifest_path="client/requirements.txt",
        commit_sha="merge123",
        captured_at="2026-07-16T12:00:00Z",
    )
    assert node.commit_sha == "merge123"
    assert node.captured_at == "2026-07-16T12:00:00Z"


# @spec DEPG-NODE-004
def test_dependency_snapshot_idfrom_key_includes_manifest_and_commit():
    from modok.quine.client import _idFrom_cypher_args
    from modok.quine.models import DependencySnapshot

    node = DependencySnapshot(
        node_type="DependencySnapshot",
        project_slug="stagehand",
        manifest_path="client/requirements.txt",
        commit_sha="merge123",
        captured_at="2026-07-16T12:00:00Z",
    )
    args, params = _idFrom_cypher_args(node)
    assert "dependency-snapshot" in args
    assert params["idf_manifest_path"] == "client/requirements.txt"
    assert params["idf_commit_sha"] == "merge123"


# ---------------------------------------------------------------------------
# DEPG-NODE-005 — DependencyChange
# ---------------------------------------------------------------------------


# @spec DEPG-NODE-005
def test_dependency_change_model_has_required_fields():
    from modok.quine.models import DependencyChange

    node = DependencyChange(
        node_type="DependencyChange",
        project_slug="stagehand",
        manifest_path="client/requirements.txt",
        package_purl="pkg:pypi/bleak",
        commit_sha="merge123",
        change_kind="changed",
        version_source="dependabot_title",
        observed_at="2026-07-16T12:00:00Z",
    )
    assert node.change_kind == "changed"
    assert node.version_source == "dependabot_title"


# @spec DEPG-NODE-005
def test_dependency_change_idfrom_key_is_deterministic_per_pr():
    """Two writes for the same (manifest, package, commit) must address the
    same node — this is what makes re-ingestion idempotent (DEPG-DIFF-005)."""
    from modok.quine.client import _idFrom_cypher_args
    from modok.quine.models import DependencyChange

    node_a = DependencyChange(
        node_type="DependencyChange",
        project_slug="stagehand",
        manifest_path="client/requirements.txt",
        package_purl="pkg:pypi/bleak",
        commit_sha="merge123",
        change_kind="changed",
        version_source="manifest_diff",
        observed_at="2026-07-16T12:00:00Z",
    )
    node_b = DependencyChange(
        node_type="DependencyChange",
        project_slug="stagehand",
        manifest_path="client/requirements.txt",
        package_purl="pkg:pypi/bleak",
        commit_sha="merge123",
        change_kind="changed",
        version_source="dependency_review",  # differs — must not affect identity
        observed_at="2026-07-16T13:00:00Z",
    )
    args_a, params_a = _idFrom_cypher_args(node_a)
    args_b, params_b = _idFrom_cypher_args(node_b)
    assert args_a == args_b
    assert params_a == params_b


# ---------------------------------------------------------------------------
# DEPG-NODE-006 — no new PullRequest node type; Fix reused
# ---------------------------------------------------------------------------


# @spec DEPG-NODE-006
def test_no_pull_request_node_type_exists():
    import modok.quine.models as models

    assert not hasattr(models, "PullRequest"), (
        "Merged-PR provenance for a DependencyChange must be represented via "
        "MERGED_VIA to the existing Fix node, not a new PullRequest node type "
        "(docs/llds/github-ingestion.md already models merged PRs as Fix)."
    )


# @spec DEPG-NODE-001, DEPG-NODE-002, DEPG-NODE-003, DEPG-NODE-004, DEPG-NODE-005
def test_all_new_types_registered_in_node_type_map():
    from modok.quine.models import _NODE_TYPE_MAP

    for name in (
        "DependencyPackage",
        "DependencyVersion",
        "DependencyManifest",
        "DependencySnapshot",
        "DependencyChange",
    ):
        assert name in _NODE_TYPE_MAP, f"{name} must be registered in _NODE_TYPE_MAP"
