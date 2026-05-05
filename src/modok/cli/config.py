"""Config loading for the MODOK CLI."""
# @spec CLI-CFG-001, CLI-CFG-002, CLI-CFG-003, CLI-CFG-004

from __future__ import annotations

import tomllib
from pathlib import Path

import click
from pydantic import BaseModel, field_validator

CONFIG_PATH = Path.home() / ".modok" / "config.toml"

_MINIMAL_TOML = """\
[quine]
url = "http://127.0.0.1:8080"
jar = "~/.modok/quine.jar"

[llm]
provider = "ollama"
base_url = "http://127.0.0.1:11434/v1"
model = "llama3"
"""


class QuineConfig(BaseModel):
    url: str = "http://127.0.0.1:8080"
    jar: str = "~/.modok/quine.jar"

    @field_validator("jar", mode="after")
    @classmethod
    def expand_jar(cls, v: str) -> str:
        return str(Path(v).expanduser())


class LLMConfig(BaseModel):
    provider: str = "ollama"
    base_url: str = "http://127.0.0.1:11434/v1"
    model: str = "llama3"
    timeout_seconds: int = 30
    timeout_propose_registry: int = 60
    cegis_max_repairs: int = 1
    normalise_batch_size: int = 200
    normalise_refinement_passes: int = 2
    backend: str = "local"
    local_endpoint: str = "http://localhost:11434"
    local_model: str = "llama3.2"
    skip_summary: bool = False
    remote_endpoint: str = ""
    remote_model: str = ""
    remote_api_key: str = ""


class ProjectConfig(BaseModel):
    slug: str
    repo: str
    github_repo: str | None = None
    last_github_sync: str | None = None

    @field_validator("repo", mode="after")
    @classmethod
    def expand_repo(cls, v: str) -> str:
        return str(Path(v).expanduser())


class ModokConfig(BaseModel):
    quine: QuineConfig = QuineConfig()
    llm: LLMConfig = LLMConfig()
    projects: list[ProjectConfig] = []

    @classmethod
    def load(cls) -> "ModokConfig":
        path = CONFIG_PATH
        if not path.exists():
            raise click.ClickException(
                f"Config not found at {path}. See the setup guide to create it."
            )
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
            # tomllib parses [[projects]] as "projects" list
            return cls.model_validate(raw)
        except click.ClickException:
            raise
        except Exception as exc:
            raise click.ClickException(f"Config parse error: {exc}") from exc

    def project(self, slug: str) -> ProjectConfig:
        for p in self.projects:
            if p.slug == slug:
                return p
        raise click.ClickException(f"project `{slug}` not found in config")


def ensure_config_exists() -> None:
    """Create a minimal config file if one does not already exist."""
    path = CONFIG_PATH
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_MINIMAL_TOML, encoding="utf-8")


def append_project(slug: str, repo: str) -> None:
    """Add a [[projects]] entry to config if slug is not already present."""
    path = CONFIG_PATH
    content = path.read_text(encoding="utf-8")
    if f'slug = "{slug}"' in content:
        return
    entry = f'\n[[projects]]\nslug = "{slug}"\nrepo = "{repo}"\n'
    path.write_text(content + entry, encoding="utf-8")
