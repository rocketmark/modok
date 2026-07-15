"""Markdown formatting of a DebugPacket for GitHub write-back comments.

Renders the *full* packet — the same content
ui/src/components/modok/DebugPacketView.tsx shows in the demo app, not a
subset. See docs/llds/standing-queries.md § GitHub Write-Back."""
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

    anchors = packet.issue.anchors
    if anchors.features or anchors.errors or anchors.symptoms:
        parts = []
        if anchors.features:
            parts.append(f"Features: {', '.join(anchors.features)}")
        if anchors.errors:
            parts.append(f"Errors: {', '.join(anchors.errors)}")
        if anchors.symptoms:
            parts.append(f"Symptoms: {', '.join(anchors.symptoms)}")
        lines.append(f"**Anchors:** {' · '.join(parts)}")
        lines.append("")

    if packet.affected_areas:
        badges = [
            f"{'⬡' if area.type == 'feature' else '○'} {area.name}" for area in packet.affected_areas
        ]
        lines.append(f"**Affected areas:** {', '.join(badges)}")
        lines.append("")

    if packet.scored_candidates:
        lines.append("**Top suspects:**")
        for c in packet.scored_candidates:
            lines.append(f"- `[{c.confidence.upper()}]` `{c.path}` (score {c.score})")
            for ev in c.evidence:
                lines.append(f"  - {ev.type}: {ev.explanation}")
        lines.append("")

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

    if packet.recent_commits:
        lines.append("**Recent commits:**")
        for c in packet.recent_commits:
            sha = c.sha[:7] if c.sha else ""
            date = c.timestamp[:10] if c.timestamp else ""
            lines.append(f"- `{sha}` ({date}) {c.author_name} — {c.message}")
        lines.append("")

    lines.append(f"_Investigation: `{investigation_id}`_")

    return "\n".join(lines)
