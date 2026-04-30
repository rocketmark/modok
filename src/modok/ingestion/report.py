from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class IngestionReport:
    docs_processed: int = 0
    nodes_written: int = 0
    edges_written: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    llm_proposals: int = 0
    pending_items: int = 0
    files_ignored: int = 0
    files_skipped: int = 0
    commits_processed: int = 0
    file_changes_written: int = 0
    duration_seconds: float = 0.0

    def __str__(self) -> str:
        raise NotImplementedError
