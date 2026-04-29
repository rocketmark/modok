from __future__ import annotations
from pathlib import Path


class Registry:
    """Loaded feature/module/error/doc-type registries from {repo_root}/registries/."""

    def __init__(self, repo_root: Path) -> None:
        raise NotImplementedError

    def has_feature(self, slug: str) -> bool:
        raise NotImplementedError

    def has_module(self, slug: str) -> bool:
        raise NotImplementedError

    def has_error(self, slug: str) -> bool:
        raise NotImplementedError

    def has_doc_type(self, doc_type: str) -> bool:
        raise NotImplementedError

    def required_fields(self, doc_type: str) -> list[str]:
        raise NotImplementedError
