"""modok normalise command."""

from __future__ import annotations

from pathlib import Path

import click

from modok.cli.config import ModokConfig
from modok.registry.normalise import normalise_registries


@click.command("normalise")
@click.option("--project", required=True, help="Project slug.")
def normalise_cmd(project: str) -> None:
    """Normalise raw registry candidates produced by modok init --assisted."""
    cfg = ModokConfig.load()

    proj = next((p for p in cfg.projects if p.slug == project), None)
    if proj is None:
        raise click.ClickException(
            f"project `{project}` not found — run `modok init --project {project} --repo <path>` first"
        )

    repo_path = Path(proj.repo).expanduser().resolve()

    try:
        summary = normalise_registries(repo_path, cfg)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    for field_type, count in summary.fields_normalised.items():
        raw_count = _raw_count(repo_path, field_type)
        click.echo(f"Normalised {field_type}: {raw_count} → {count} entries")

    for field_type in ("features", "modules", "errors"):
        if field_type in summary.fields_normalised:
            click.echo(f"Wrote registries/{field_type}.yml")

    for ft in summary.fields_failed:
        click.echo(f"Warning: '{ft}' fell back to raw candidates (CEGIS exhausted)", err=True)


def _raw_count(repo_path: Path, field_type: str) -> int:
    import yaml

    raw_path = repo_path / "registries" / f"{field_type}.raw.yml"
    if not raw_path.exists():
        return 0
    data = yaml.safe_load(raw_path.read_text(encoding="utf-8")) or {}
    return len(data.get(field_type, {}))
