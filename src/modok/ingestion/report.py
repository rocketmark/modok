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
    unregistered_count: int = 0
    unregistered_paths: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0

    def __str__(self) -> str:
        lines = [
            "Ingestion complete",
            f"  Docs processed:  {self.docs_processed}",
            f"  Nodes written:   {self.nodes_written}",
            f"  Edges written:   {self.edges_written}",
            f"  Warnings:        {len(self.warnings)}",
            f"  Errors:          {len(self.errors)}",
            f"  LLM proposals:   {self.llm_proposals}",
            f"  Pending items:   {self.pending_items}",
            f"  Files ignored:   {self.files_ignored}",
            f"  Unregistered:    {self.unregistered_count}",
            f"  Duration:        {self.duration_seconds:.1f}s",
        ]
        for w in self.warnings:
            lines.append(f"  WARNING: {w}")
        for e in self.errors:
            lines.append(f"  ERROR:   {e}")
        if self.unregistered_paths:
            lines.append("  Unregistered docs:")
            for p in self.unregistered_paths:
                lines.append(f"    {p}")
        return "\n".join(lines)
