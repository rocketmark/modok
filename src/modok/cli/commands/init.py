"""modok init command."""
# @spec CLI-INIT-001, CLI-INIT-002, CLI-INIT-003, CLI-INIT-004, CLI-INIT-005,
#        CLI-INIT-006, CLI-INIT-007, CLI-INIT-008, CLI-INIT-009, CLI-INIT-010, CLI-INIT-011

from __future__ import annotations

from pathlib import Path

import click

from modok.cli.config import CONFIG_PATH, ModokConfig, append_project, ensure_config_exists
from modok.ingestion.hook import install_post_commit_hook
from modok.llm.errors import LLMUnavailableError
from modok.quine.client import QuineClient  # noqa: F401 — imported so CLI-INIT-006 test can patch it
from modok.registry.proposal import propose_registries

_REGISTRY_STUBS = {
    "features.yml": "features: {}\n",
    "modules.yml": "modules: {}\n",
    "errors.yml": "errors: {}\n",
    "doc-types.yml": "doc_types: {}\n",
}


@click.command("init")
@click.option("--project", required=True, help="Project slug.")
@click.option("--repo", required=True, type=click.Path(), help="Path to the project git repo.")
@click.option(
    "--assisted", is_flag=True, default=False, help="Use LLM to propose registry contents."
)
def init_cmd(project: str, repo: str, assisted: bool) -> None:
    """Register a project with MODOK: stub registries, install the git hook, update config."""
    repo_path = Path(repo).expanduser().resolve()

    if not (repo_path / ".git").is_dir():
        raise click.ClickException(f"not a git repository: {repo_path}")

    # Ensure config exists and project is registered before anything else
    ensure_config_exists()
    append_project(project, str(repo_path))

    if assisted:
        # CLI-INIT-008: delegate to proposal engine before hook
        cfg = ModokConfig.load()
        try:
            summary = propose_registries(repo_path, cfg)
        except LLMUnavailableError:
            click.echo(
                "LLM gateway is not reachable — start Ollama or check `local_endpoint` in config"
            )
            raise click.exceptions.Exit(2)

        # CLI-INIT-011: print summary to stdout
        processed = summary.sections_processed
        failed = summary.sections_failed
        docs = summary.docs_processed
        fail_note = f" ({failed} failed)" if failed else ""
        click.echo(f"Processed {processed} sections across {docs} docs{fail_note}.")
        for fname, count in summary.entries_written.items():
            click.echo(f"Wrote registries/{fname} ({count} raw entries)")
        click.echo(
            "Run `modok normalise --project <slug>` to normalise and write final registries."
        )

        # Emit failed sections to stderr
        for heading, doc_path in summary.failed_sections:
            click.echo(f"  Failed: '{heading}' in {doc_path}", err=True)
    else:
        # CLI-INIT-002: create stub files when not --assisted
        reg_dir = repo_path / "registries"
        reg_dir.mkdir(parents=True, exist_ok=True)
        for fname, stub in _REGISTRY_STUBS.items():
            fpath = reg_dir / fname
            if not fpath.exists():
                fpath.write_text(stub, encoding="utf-8")
                click.echo(f"Created {fpath}")

    # CLI-INIT-003: install post-commit hook regardless of mode
    install_post_commit_hook(repo_path, project, ["docs", "registries"])
    click.echo(f"Installed post-commit hook in {repo_path}")

    click.echo(f"Registered project `{project}` in {CONFIG_PATH}")
