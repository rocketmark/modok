"""Markdown formatting of a DebugPacket for GitHub write-back comments.
See docs/llds/standing-queries.md § GitHub Write-Back."""
# @spec SQ-GH-002

from __future__ import annotations

from modok.retrieval.models import DebugPacket


def format_debug_packet_markdown(
    packet: DebugPacket, investigation_id: str, standing_query_name: str
) -> str:
    lines = [
        "## 🔎 MODOK investigation triggered",
        "",
        f"Standing query `{standing_query_name}` matched this issue against existing graph evidence.",
        "",
        f"**Summary:** {packet.summary or packet.issue.summary}",
        "",
    ]

    if packet.known_issues:
        lines.append("**Known issues:**")
        for ki in packet.known_issues:
            lines.append(f"- {ki.id}: {ki.summary}")
        lines.append("")

    if packet.prior_fixes:
        lines.append("**Prior fixes:**")
        for fix in packet.prior_fixes:
            commit = f" ({fix.commit})" if fix.commit else ""
            lines.append(f"- {fix.id}{commit}: {fix.summary}")
        lines.append("")

    if packet.relevant_files:
        lines.append("**Relevant files:**")
        for f in packet.relevant_files:
            lines.append(f"- {f}")
        lines.append("")

    if packet.relevant_tests:
        lines.append("**Relevant tests:**")
        for t in packet.relevant_tests:
            lines.append(f"- {t}")
        lines.append("")

    lines.append(f"_Investigation: `{investigation_id}`_")

    return "\n".join(lines)
