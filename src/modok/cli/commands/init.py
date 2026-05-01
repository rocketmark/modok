"""modok init command."""
# @spec CLI-INIT-001, CLI-INIT-002, CLI-INIT-003, CLI-INIT-004, CLI-INIT-005, CLI-INIT-006, CLI-INIT-007

from __future__ import annotations

from pathlib import Path

import click

from modok.cli.config import CONFIG_PATH, append_project, ensure_config_exists
from modok.ingestion.hook import install_post_commit_hook
from modok.quine.client import QuineClient  # noqa: F401 — imported so CLI-INIT-006 test can patch it

_REGISTRY_STUBS = {
    "features.yml": "features: {}\n",
    "modules.yml": "modules: {}\n",
    "errors.yml": "errors: {}\n",
    "doc-types.yml": "doc_types: {}\n",
}


@click.command("init")
@click.option("--project", required=True, help="Project slug.")
@click.option("--repo", required=True, type=click.Path(), help="Path to the project git repo.")
def init_cmd(project: str, repo: str) -> None:
    repo_path = Path(repo).expanduser().resolve()

    if not (repo_path / ".git").is_dir():
        raise click.ClickException(f"not a git repository: {repo_path}")

    reg_dir = repo_path / "registries"
    reg_dir.mkdir(parents=True, exist_ok=True)
    for fname, stub in _REGISTRY_STUBS.items():
        fpath = reg_dir / fname
        if not fpath.exists():
            fpath.write_text(stub, encoding="utf-8")
            click.echo(f"Created {fpath}")

    install_post_commit_hook(repo_path, project, ["docs", "registries"])
    click.echo(f"Installed post-commit hook in {repo_path}")

    ensure_config_exists()
    append_project(project, str(repo_path))
    click.echo(f"Registered project `{project}` in {CONFIG_PATH}")
