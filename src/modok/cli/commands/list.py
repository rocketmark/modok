"""modok list command — lists valid feature/module slugs and per-module code
identifiers from the project's registries. Registry-only: no Quine dependency."""
# @spec CLI-LIST-001, CLI-LIST-002, CLI-LIST-003, CLI-LIST-004, CLI-LIST-005,
#       CLI-LIST-006, CLI-LIST-007, CLI-LIST-008, CLI-LIST-009, CLI-LIST-010,
#       CLI-LIST-011, CLI-LIST-012, CLI-LIST-013, CLI-LIST-014, CLI-LIST-015,
#       CLI-LIST-016, CLI-LIST-017, CLI-LIST-018, CLI-LIST-019

from __future__ import annotations

import json
from pathlib import Path

import click

from modok.cli.config import ModokConfig
from modok.ingestion.errors import RegistryNotFoundError
from modok.ingestion.registry import Registry


@click.command("list")
@click.option("--project", required=True, help="Project slug.")
@click.option("--features", "show_features", is_flag=True, default=False, help="List features only.")
@click.option("--modules", "show_modules", is_flag=True, default=False, help="List modules only.")
@click.option(
    "--elements", "show_elements", is_flag=True, default=False, help="List module elements only."
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON.")
def list_cmd(
    project: str,
    show_features: bool,
    show_modules: bool,
    show_elements: bool,
    as_json: bool,
) -> None:
    """List the valid feature/module slugs and module elements for a project."""
    config = ModokConfig.load()
    proj = config.project(project)

    # @spec CLI-LIST-012, CLI-LIST-019 — elements.yml itself is optional at
    # the Registry level (_load_optional); only features.yml/modules.yml
    # absence raises RegistryNotFoundError.
    try:
        registry = Registry(repo_root=Path(proj.repo))
    except RegistryNotFoundError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(1)

    # @spec CLI-LIST-002, CLI-LIST-003, CLI-LIST-004, CLI-LIST-005, CLI-LIST-014
    if not show_features and not show_modules and not show_elements:
        include_features = include_modules = include_elements = True
    else:
        include_features = show_features
        include_modules = show_modules
        include_elements = show_elements

    # @spec CLI-LIST-006
    feature_entries = sorted(registry.feature_names().items()) if include_features else []
    module_entries = sorted(registry.module_names().items()) if include_modules else []
    # @spec CLI-LIST-015, CLI-LIST-016 — only modules present in elements.yml;
    # sorted by module slug; element order within a module preserved as-is.
    element_entries = sorted(registry.module_elements().items()) if include_elements else []

    if as_json:
        # @spec CLI-LIST-010, CLI-LIST-018
        result: dict = {"project": project}
        if include_features:
            result["features"] = [{"slug": s, "name": n} for s, n in feature_entries]
        if include_modules:
            result["modules"] = [{"slug": s, "name": n} for s, n in module_entries]
        if include_elements:
            result["elements"] = [{"module": m, "elements": els} for m, els in element_entries]
        click.echo(json.dumps(result))
        return

    # @spec CLI-LIST-007, CLI-LIST-008, CLI-LIST-009
    if include_features:
        click.echo("Features:")
        for slug, name in feature_entries:
            click.echo(f"  {slug}  {name}")
        if not feature_entries:
            click.echo("  (none)")

    if include_modules:
        click.echo("Modules:")
        for slug, name in module_entries:
            click.echo(f"  {slug}  {name}")
        if not module_entries:
            click.echo("  (none)")

    # @spec CLI-LIST-017
    if include_elements:
        click.echo("Elements:")
        for module_slug, els in element_entries:
            click.echo(f"  {module_slug}  {', '.join(els)}")
        if not element_entries:
            click.echo("  (none)")
