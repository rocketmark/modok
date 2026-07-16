"""Markdown formatting for GitHub write-back comments — two separate
messages per investigation (SQ-GH-007): an immediate, fast "triggered"
comment (format_investigation_triggered_markdown) posted before the slow
retrieve() pipeline runs, and a later "results" comment
(format_debug_packet_markdown) with the *full* packet — all of the same
underlying data ui/src/components/modok/DebugPacketView.tsx shows in the
demo app, not a subset (the two have since diverged in presentation, not
data — see docs/llds/standing-queries.md § GitHub Write-Back)."""
# @spec SQ-GH-002, SQ-GH-006, SQ-GH-007, SQ-GH-008, SQ-GH-009, SQ-GH-010

from __future__ import annotations

from modok.retrieval.models import DebugPacket, EvidenceItem


def _commit_evidence_label(ev: EvidenceItem) -> str:
    """Render a commit-grouped evidence item's sub-bullet text, stripping the
    trailing `· {sha}` — the commit SHA is already the group's own header."""
    if ev.type == "recent_commit":
        return "Touched"
    if ev.type == "commit_message_match":
        return f"Commit message: {ev.explanation.rsplit(' · ', 1)[0]}"
    if ev.type == "function_anchor_match":
        return f"Function match: {ev.explanation.rsplit(' · ', 1)[0]}"
    return f"{ev.type}: {ev.explanation}"


def _render_candidate_evidence(
    lines: list[str], evidence: list[EvidenceItem], commit_dates: dict[str, str]
) -> None:
    """Non-commit evidence renders flat, as before. Commit-derived evidence
    (recent_commit / commit_message_match / function_anchor_match — anything
    carrying a commit_sha) groups under one bullet per commit, sorted by how
    many distinct signals that commit has (most first) — a commit that's
    both recent *and* has a matching message or matching function is a much
    stronger "look here first" signal than one that's merely recent."""
    other_items = [ev for ev in evidence if not ev.commit_sha]
    commit_items: dict[str, list[EvidenceItem]] = {}
    for ev in evidence:
        if ev.commit_sha:
            commit_items.setdefault(ev.commit_sha, []).append(ev)

    for ev in other_items:
        lines.append(f"  - {ev.type}: {ev.explanation}")

    for sha in sorted(commit_items, key=lambda s: -len(commit_items[s])):
        # sha is deliberately bare (no backticks) — GitHub auto-links a bare
        # commit SHA to the commit within the same repo; wrapping it in an
        # inline code span (as an earlier version of this did) suppresses
        # that auto-linking, found live when commit references stopped being
        # clickable after this grouping was introduced.
        date = commit_dates.get(sha, "")
        date_suffix = f" ({date})" if date else ""
        lines.append(f"  - Recent commit {sha}{date_suffix}:")
        for ev in commit_items[sha]:
            lines.append(f"    - {_commit_evidence_label(ev)}")


def format_investigation_triggered_markdown(summary: str, standing_query_name: str, investigation_id: str) -> str:
    """The immediate, fast comment posted as soon as a standing query fires
    — before retrieve()'s traversal/scoring/LLM-summary work runs. `summary`
    comes from quick_investigation_summary(), not the full packet."""
    lines = [
        "## 🔎 MODOK investigation triggered",
        "",
        f"Standing query `{standing_query_name}` matched this issue against existing graph evidence.",
        "",
        f"**Summary:** {summary}",
        "",
        f"_Investigation: `{investigation_id}`_",
    ]
    return "\n".join(lines)


def format_ci_corroboration_milestone_markdown(
    *,
    error_signature: str,
    test_failure_id: str,
    workflow_name: str,
    head_sha: str,
    workflow_run_id: str,
) -> str:
    """Standalone comment for a first CI-corroboration milestone on an issue
    (SQ-MILE-009) — deliberately worded as additional evidence for the same
    issue, not a new investigation and not a supersession of any earlier
    comment (SQ-MILE-010, SQ-MILE-011); no comment-scraping/linking exists in
    this slice, so this never references another comment."""
    lines = [
        "## 🧪 Additional evidence: CI test failure matches this issue",
        "",
        f"A CI test failure was linked to this issue via a shared error signature: `{error_signature}`.",
        "",
        f"- Test: `{test_failure_id}`",
    ]
    if workflow_name:
        lines.append(f"- Workflow: {workflow_name}")
    if head_sha:
        lines.append(f"- Commit: {head_sha[:7]}")
    if workflow_run_id:
        lines.append(f"- Workflow run: {workflow_run_id}")
    lines.append("")
    lines.append(
        "This is additional evidence for the same issue, gathered from continuous CI ingestion."
    )
    return "\n".join(lines)


def format_debug_packet_markdown(
    packet: DebugPacket, investigation_id: str, standing_query_name: str
) -> str:
    lines = [
        "## 🔍 MODOK investigation results",
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

    if packet.scored_candidates or packet.covered_tests:
        lines.append("**Top suspects:**")
        doc_penalized = [
            c for c in packet.scored_candidates if any(ev.type == "doc_penalty" for ev in c.evidence)
        ]
        regular = [c for c in packet.scored_candidates if c not in doc_penalized]
        commit_dates = {
            c.sha[:7]: c.timestamp[:10] for c in packet.recent_commits if c.sha and c.timestamp
        }
        for c in regular:
            lines.append(f"- `[{c.confidence.upper()}]` `{c.path}` (score {c.score})")
            _render_candidate_evidence(lines, c.evidence, commit_dates)
        # DRE-TESTCOV-002 — tests reached via HAS_TEST with no other evidence
        # tying them to this ticket are informational, not ranked; a test
        # with real evidence is in `regular` above instead, not listed twice.
        if packet.covered_tests:
            count = len(packet.covered_tests)
            noun = "file" if count == 1 else "files"
            lines.append(f"- `[LOW]` {count} test {noun} covering this feature (no other evidence):")
            for ct in packet.covered_tests:
                lines.append(f"  - `{ct.path}`")
        if doc_penalized:
            count = len(doc_penalized)
            noun = "file" if count == 1 else "files"
            lines.append(f"- `[LOW]` {count} supporting doc/config {noun} (non-source, low relevance):")
            for c in doc_penalized:
                lines.append(f"  - `{c.path}`")
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
