"""
Tests for GitHub write-back: posting the Diagnostic Retrieval Engine's debug
packet as a comment on the originating GitHub issue when a standing query
fires. All tests are written before implementation (Phase 5). Every test
cites the EARS spec it verifies via @spec annotation.

Specs verified: SQ-GH-001, SQ-GH-002, SQ-GH-003, SQ-GH-004.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from modok.retrieval.formatting import (
    format_debug_packet_markdown,
    format_investigation_triggered_markdown,
)
from modok.retrieval.models import (
    AffectedArea,
    CoveredTest,
    DebugPacket,
    EvidenceItem,
    IssueAnchors,
    IssueSummary,
    KnownIssueRef,
    PriorFix,
    RecentCommit,
    ScoredCandidate,
)


def make_packet(**overrides) -> DebugPacket:
    defaults = dict(
        issue=IssueSummary(
            summary="Client rejects v2 header",
            anchors=IssueAnchors(features=["shtp-receiver"], errors=["shtp-version-mismatch"], symptoms=[]),
        ),
        affected_areas=[AffectedArea(type="feature", id="feature:shtp-receiver", name="shtp-receiver")],
        relevant_files=["agent/src/shtp.c"],
        relevant_tests=[],
        known_issues=[KnownIssueRef(id="ki-shtp-version-mismatch", summary="Version mismatch corrupts calibration")],
        prior_fixes=[PriorFix(id="fix-shtp-version-offset", commit="a3f9c12", summary="Fix byte offset")],
        scored_candidates=[],
        summary="The client compares the version field at the wrong byte offset.",
    )
    defaults.update(overrides)
    return DebugPacket(**defaults)


# ---------------------------------------------------------------------------
# SQ-GH-002 — markdown formatting
# ---------------------------------------------------------------------------


# @spec SQ-GH-002
def test_markdown_includes_standing_query_name_and_investigation_id():
    md = format_debug_packet_markdown(make_packet(), "inv-42", "actionable-issue-pattern")
    assert "actionable-issue-pattern" in md
    assert "inv-42" in md


# @spec SQ-GH-007
def test_triggered_markdown_has_header_summary_and_investigation_id():
    md = format_investigation_triggered_markdown(
        "Likely a logic error in wifi_provision_logic.py.", "new-bug-report-pattern", "inv-77"
    )
    assert "investigation triggered" in md
    assert "new-bug-report-pattern" in md
    assert "Likely a logic error in wifi_provision_logic.py." in md
    assert "inv-77" in md
    # This is the fast, short comment — it must not contain full-packet
    # sections that only the later "results" comment has.
    assert "Top suspects" not in md
    assert "Relevant files" not in md


# @spec SQ-GH-007
def test_results_markdown_header_says_results_not_triggered():
    md = format_debug_packet_markdown(make_packet(), "inv-42", "actionable-issue-pattern")
    assert "investigation results" in md
    assert "investigation triggered" not in md


# @spec SQ-GH-002
def test_markdown_includes_summary_known_issues_and_fixes():
    md = format_debug_packet_markdown(make_packet(), "inv-42", "actionable-issue-pattern")
    assert "byte offset" in md
    assert "ki-shtp-version-mismatch" in md
    assert "fix-shtp-version-offset" in md
    assert "agent/src/shtp.c" in md


# @spec SQ-GH-002
def test_markdown_omits_empty_sections():
    packet = make_packet(relevant_tests=[], prior_fixes=[])
    md = format_debug_packet_markdown(packet, "inv-42", "actionable-issue-pattern")
    assert "Relevant tests" not in md
    assert "Prior fixes" not in md


# @spec SQ-GH-002
def test_markdown_includes_anchors():
    packet = make_packet(
        issue=IssueSummary(
            summary="Client rejects v2 header",
            anchors=IssueAnchors(
                features=["shtp-receiver"], errors=["shtp-version-mismatch"], symptoms=["freeze"]
            ),
        )
    )
    md = format_debug_packet_markdown(packet, "inv-42", "actionable-issue-pattern")
    assert "shtp-receiver" in md
    assert "shtp-version-mismatch" in md
    assert "freeze" in md


# @spec SQ-GH-002
def test_markdown_omits_anchors_section_when_empty():
    packet = make_packet(
        issue=IssueSummary(
            summary="Client rejects v2 header",
            anchors=IssueAnchors(features=[], errors=[], symptoms=[]),
        )
    )
    md = format_debug_packet_markdown(packet, "inv-42", "actionable-issue-pattern")
    assert "Anchors" not in md


# @spec SQ-GH-002
def test_markdown_includes_affected_areas():
    md = format_debug_packet_markdown(make_packet(), "inv-42", "actionable-issue-pattern")
    assert "shtp-receiver" in md
    assert "Affected areas" in md


# @spec SQ-GH-002
def test_markdown_omits_affected_areas_when_empty():
    packet = make_packet(affected_areas=[])
    md = format_debug_packet_markdown(packet, "inv-42", "actionable-issue-pattern")
    assert "Affected areas" not in md


# @spec SQ-GH-002
def test_markdown_includes_top_suspects_with_confidence_and_evidence():
    packet = make_packet(
        scored_candidates=[
            ScoredCandidate(
                path="agent/src/shtp.c",
                kind="source",
                score=12.5,
                confidence="high",
                evidence=[EvidenceItem(type="feature_anchor", score=7.0, explanation="shtp-receiver")],
            ),
        ],
    )
    md = format_debug_packet_markdown(packet, "inv-42", "actionable-issue-pattern")
    assert "Top suspects" in md
    assert "agent/src/shtp.c" in md
    assert "HIGH" in md.upper()
    assert "feature_anchor" in md
    assert "shtp-receiver" in md


# @spec SQ-GH-002
def test_markdown_groups_doc_penalized_candidates_into_single_low():
    packet = make_packet(
        scored_candidates=[
            ScoredCandidate(
                path="agent/src/shtp.c",
                kind="source",
                score=12.5,
                confidence="high",
                evidence=[EvidenceItem(type="feature_anchor", score=7.0, explanation="shtp-receiver")],
            ),
            ScoredCandidate(
                path="docs/llds/shtp.md",
                kind="source",
                score=1.8,
                confidence="low",
                evidence=[
                    EvidenceItem(type="feature_anchor", score=7.0, explanation="shtp-receiver"),
                    EvidenceItem(
                        type="doc_penalty", score=-5.2, explanation="Non-source file (×0.25 actionability penalty)"
                    ),
                ],
            ),
            ScoredCandidate(
                path="docs/specs/shtp-specs.md",
                kind="source",
                score=1.8,
                confidence="low",
                evidence=[
                    EvidenceItem(type="feature_anchor", score=7.0, explanation="shtp-receiver"),
                    EvidenceItem(
                        type="doc_penalty", score=-5.2, explanation="Non-source file (×0.25 actionability penalty)"
                    ),
                ],
            ),
        ],
    )
    md = format_debug_packet_markdown(packet, "inv-42", "actionable-issue-pattern")
    assert "agent/src/shtp.c" in md
    assert "docs/llds/shtp.md" in md
    assert "docs/specs/shtp-specs.md" in md
    # The two doc-penalized files collapse into a single grouped LOW line,
    # not two separate `[LOW]` entries with their own evidence breakdown —
    # but each grouped file still gets its own line (not a comma-separated
    # inline list) so the paths stay individually readable/clickable.
    assert md.count("`[LOW]`") == 1
    assert "2 supporting doc/config files" in md
    assert "- `docs/llds/shtp.md`" in md
    assert "- `docs/specs/shtp-specs.md`" in md
    assert "doc_penalty" not in md


# @spec SQ-GH-002, DRE-TESTCOV-002
def test_markdown_groups_covered_tests_into_single_low():
    """covered_tests (informational — no other evidence tying the test to
    this ticket) render as a single collapsed [LOW] bucket; a test with real
    evidence beyond bare coverage is a scored_candidates entry instead and
    stays listed individually, not duplicated into the collapsed bucket."""
    packet = make_packet(
        scored_candidates=[
            ScoredCandidate(
                path="agent/src/shtp.c",
                kind="source",
                score=12.5,
                confidence="high",
                evidence=[EvidenceItem(type="feature_anchor", score=7.0, explanation="shtp-receiver")],
            ),
            # A test file with real evidence beyond bare coverage is a
            # more interesting candidate and must stay listed individually.
            ScoredCandidate(
                path="agent/tests/test_shtp_relevant.py",
                kind="test",
                score=10.0,
                confidence="medium",
                evidence=[EvidenceItem(type="ticket_mention", score=10.0, explanation="named in ticket")],
            ),
        ],
        covered_tests=[
            CoveredTest(path="agent/tests/test_shtp_a.py", covering_slugs=["shtp-receiver"]),
            CoveredTest(path="agent/tests/test_shtp_b.py", covering_slugs=["shtp-receiver"]),
        ],
    )
    md = format_debug_packet_markdown(packet, "inv-42", "actionable-issue-pattern")
    assert "agent/src/shtp.c" in md
    assert "agent/tests/test_shtp_a.py" in md
    assert "agent/tests/test_shtp_b.py" in md
    assert "agent/tests/test_shtp_relevant.py" in md
    assert "2 test files covering this feature" in md
    assert "- `agent/tests/test_shtp_a.py`" in md
    assert "- `agent/tests/test_shtp_b.py`" in md
    assert "ticket_mention" in md


# @spec SQ-GH-002
def test_markdown_groups_evidence_by_commit_sorted_by_signal_count():
    packet = make_packet(
        scored_candidates=[
            ScoredCandidate(
                path="pi-image/chroot-customize.sh",
                kind="source",
                score=26.9,
                confidence="high",
                evidence=[
                    EvidenceItem(type="feature_primary_file", score=9.0, explanation="wifi-provisioning"),
                    EvidenceItem(
                        type="recent_commit",
                        score=1.5,
                        explanation="Touched in recent commit 7c0e771",
                        commit_sha="7c0e771",
                    ),
                    EvidenceItem(
                        type="recent_commit",
                        score=1.5,
                        explanation="Touched in recent commit 3a3882d",
                        commit_sha="3a3882d",
                    ),
                    EvidenceItem(
                        type="commit_message_match",
                        score=9.0,
                        explanation="fixed wifi provisioning · 3a3882d",
                        commit_sha="3a3882d",
                    ),
                ],
            ),
        ],
    )
    md = format_debug_packet_markdown(packet, "inv-42", "actionable-issue-pattern")
    # The two-signal commit (3a3882d: touched + message match) is grouped
    # under one header and sorted ahead of the one-signal commit (7c0e771),
    # even though 7c0e771 appeared first in the evidence list.
    # sha is bare (no backticks) so GitHub auto-links it to the commit.
    assert "Recent commit 3a3882d" in md
    assert "Recent commit 7c0e771" in md
    assert "`3a3882d`" not in md
    assert "`7c0e771`" not in md
    assert md.index("Recent commit 3a3882d") < md.index("Recent commit 7c0e771")
    assert "Commit message: fixed wifi provisioning" in md
    assert "fixed wifi provisioning · 3a3882d" not in md  # sha not duplicated in the sub-bullet
    assert "Touched" in md


# @spec SQ-GH-009
def test_markdown_annotates_commit_group_header_with_date():
    packet = make_packet(
        scored_candidates=[
            ScoredCandidate(
                path="pi-image/chroot-customize.sh",
                kind="source",
                score=10.5,
                confidence="medium",
                evidence=[
                    EvidenceItem(
                        type="recent_commit",
                        score=1.5,
                        explanation="Touched in recent commit 3a3882d",
                        commit_sha="3a3882d",
                    ),
                ],
            ),
        ],
        recent_commits=[
            RecentCommit(
                sha="3a3882d44a7a13b140d90140663bf736c3265808",
                timestamp="2026-06-26T18:04:08-04:00",
                author_name="Mark Stalzer",
                message="fixed wifi provisioning",
                files_touched=["pi-image/chroot-customize.sh"],
            ),
        ],
    )
    md = format_debug_packet_markdown(packet, "inv-42", "actionable-issue-pattern")
    assert "Recent commit 3a3882d (2026-06-26):" in md


# @spec SQ-GH-002
def test_markdown_omits_top_suspects_when_empty():
    md = format_debug_packet_markdown(make_packet(scored_candidates=[]), "inv-42", "actionable-issue-pattern")
    assert "Top suspects" not in md


# @spec SQ-GH-002
def test_markdown_includes_recent_commits():
    packet = make_packet(
        recent_commits=[
            RecentCommit(
                sha="a3f9c1234567",
                timestamp="2026-07-10T12:00:00Z",
                author_name="Jane Dev",
                message="Fix byte offset in version check",
                files_touched=["agent/src/shtp.c"],
            ),
        ],
    )
    md = format_debug_packet_markdown(packet, "inv-42", "actionable-issue-pattern")
    assert "Recent commits" in md
    assert "a3f9c12" in md
    assert "Jane Dev" in md
    assert "Fix byte offset in version check" in md


# @spec SQ-GH-002
def test_markdown_omits_recent_commits_when_empty():
    md = format_debug_packet_markdown(make_packet(recent_commits=[]), "inv-42", "actionable-issue-pattern")
    assert "Recent commits" not in md


# ---------------------------------------------------------------------------
# GitHub comment POST — best-effort, never raises
# ---------------------------------------------------------------------------


# @spec SQ-GH-001
@pytest.mark.asyncio
async def test_post_issue_comment_sends_body_to_correct_url():
    requests_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(201, json={"id": 1})

    from modok.ingestion.github import post_issue_comment

    with patch("httpx.AsyncClient") as mock_cls:
        mock_instance = mock_cls.return_value.__aenter__.return_value
        mock_instance.post = AsyncMock(return_value=httpx.Response(201, json={"id": 1}))
        await post_issue_comment("acme/stagehand", "tok", "42", "hello world")
        call = mock_instance.post.call_args
        assert "acme/stagehand" in call.args[0] or "acme/stagehand" in str(call)
        assert "42" in str(call)


# @spec SQ-GH-004
@pytest.mark.asyncio
async def test_post_issue_comment_failure_is_swallowed():
    from modok.ingestion.github import post_issue_comment

    with patch("httpx.AsyncClient") as mock_cls:
        mock_instance = mock_cls.return_value.__aenter__.return_value
        mock_instance.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        await post_issue_comment("acme/stagehand", "tok", "42", "hello world")  # must not raise


# ---------------------------------------------------------------------------
# SQ-GH-001 / SQ-GH-003 — the run_ingest_event write-back gate
# ---------------------------------------------------------------------------


# @spec SQ-GH-001
@pytest.mark.asyncio
async def test_maybe_notify_github_posts_when_configured():
    from modok.webhook.server import _maybe_notify_github

    fake_project = type("P", (), {"slug": "stagehand", "github_repo": "acme/stagehand"})()
    fake_config = type("C", (), {"projects": [fake_project]})()

    with patch("modok.cli.config.ModokConfig.load", return_value=fake_config), \
         patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}), \
         patch("modok.retrieval.engine.quick_investigation_summary", new=AsyncMock(return_value="quick take")), \
         patch("modok.retrieval.engine.retrieve", new=AsyncMock(return_value=make_packet())), \
         patch("modok.ingestion.github.post_issue_comment", new=AsyncMock()) as mock_post:
        await _maybe_notify_github(
            client=AsyncMock(),
            project_slug="stagehand",
            source_system="github",
            ticket_id="42",
            investigation_id="inv-42",
            standing_query_name="actionable-issue-pattern",
        )

    # @spec SQ-GH-007 — one fast "triggered" comment, one full "results" comment
    assert mock_post.call_count == 2
    triggered_body = mock_post.call_args_list[0].args[3]
    results_body = mock_post.call_args_list[1].args[3]
    assert "investigation triggered" in triggered_body
    assert "quick take" in triggered_body
    assert "investigation results" in results_body


# @spec SQ-GH-003
@pytest.mark.asyncio
async def test_maybe_notify_github_skips_when_token_missing():
    from modok.webhook.server import _maybe_notify_github

    fake_project = type("P", (), {"slug": "stagehand", "github_repo": "acme/stagehand"})()
    fake_config = type("C", (), {"projects": [fake_project]})()

    with patch("modok.cli.config.ModokConfig.load", return_value=fake_config), \
         patch.dict("os.environ", {}, clear=True), \
         patch("modok.ingestion.github.post_issue_comment", new=AsyncMock()) as mock_post:
        await _maybe_notify_github(
            client=AsyncMock(),
            project_slug="stagehand",
            source_system="github",
            ticket_id="42",
            investigation_id="inv-42",
            standing_query_name="actionable-issue-pattern",
        )  # must not raise

    mock_post.assert_not_called()


# @spec SQ-GH-003
@pytest.mark.asyncio
async def test_maybe_notify_github_skips_when_repo_not_configured():
    from modok.webhook.server import _maybe_notify_github

    fake_project = type("P", (), {"slug": "stagehand", "github_repo": None})()
    fake_config = type("C", (), {"projects": [fake_project]})()

    with patch("modok.cli.config.ModokConfig.load", return_value=fake_config), \
         patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}), \
         patch("modok.ingestion.github.post_issue_comment", new=AsyncMock()) as mock_post:
        await _maybe_notify_github(
            client=AsyncMock(),
            project_slug="stagehand",
            source_system="github",
            ticket_id="42",
            investigation_id="inv-42",
            standing_query_name="actionable-issue-pattern",
        )

    mock_post.assert_not_called()


# @spec SQ-GH-003
@pytest.mark.asyncio
async def test_maybe_notify_github_skips_for_non_github_source():
    from modok.webhook.server import _maybe_notify_github

    with patch("modok.ingestion.github.post_issue_comment", new=AsyncMock()) as mock_post:
        await _maybe_notify_github(
            client=AsyncMock(),
            project_slug="stagehand",
            source_system="webhook",
            ticket_id="42",
            investigation_id="inv-42",
            standing_query_name="actionable-issue-pattern",
        )

    mock_post.assert_not_called()


# @spec SQ-GH-001
@pytest.mark.asyncio
async def test_maybe_notify_github_resolves_customer_issue_by_property_lookup():
    """The CustomerIssue was written via Quine's own idFrom() embedded in
    Cypher (upsert_node) — there is no Python-computable ID for it. This
    verifies _maybe_notify_github resolves the real Quine node ID via a
    property-match query rather than a synthetic Python-side ID, and passes
    that real ID into retrieve() (the exact bug found live: a synthetic ID
    made retrieve() always fail with DRENotFoundError, silently swallowed by
    SQ-GH-004, so the comment was never posted for any GitHub issue)."""
    from modok.webhook.server import _maybe_notify_github

    fake_project = type("P", (), {"slug": "stagehand", "github_repo": "acme/stagehand"})()
    fake_config = type("C", (), {"projects": [fake_project]})()

    mock_client = AsyncMock()
    mock_client.query = AsyncMock(return_value=[["real-quine-uuid-123"]])

    captured_ids = []

    async def fake_retrieve(issue_id, project_slug, client, **kwargs):
        captured_ids.append(issue_id)
        return make_packet()

    with patch("modok.cli.config.ModokConfig.load", return_value=fake_config), \
         patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}), \
         patch("modok.retrieval.engine.quick_investigation_summary", new=AsyncMock(return_value="quick take")), \
         patch("modok.retrieval.engine.retrieve", new=fake_retrieve), \
         patch("modok.ingestion.github.post_issue_comment", new=AsyncMock()) as mock_post:
        await _maybe_notify_github(
            client=mock_client,
            project_slug="stagehand",
            source_system="github",
            ticket_id="42",
            investigation_id="inv-42",
            standing_query_name="actionable-issue-pattern",
        )

    # First query() call is always the ci_id property-lookup, regardless of
    # how many more queries quick_investigation_summary's own graph-anchor
    # lookup makes afterward.
    query_call = mock_client.query.call_args_list[0]
    params = query_call.args[1]
    assert params == {"p": "stagehand", "s": "github", "t": "42"}
    assert captured_ids == ["real-quine-uuid-123"]
    assert mock_post.call_count == 2


# @spec SQ-GH-004
@pytest.mark.asyncio
async def test_maybe_notify_github_skips_when_customer_issue_not_found():
    from modok.webhook.server import _maybe_notify_github

    fake_project = type("P", (), {"slug": "stagehand", "github_repo": "acme/stagehand"})()
    fake_config = type("C", (), {"projects": [fake_project]})()

    mock_client = AsyncMock()
    mock_client.query = AsyncMock(return_value=[])

    with patch("modok.cli.config.ModokConfig.load", return_value=fake_config), \
         patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}), \
         patch("modok.ingestion.github.post_issue_comment", new=AsyncMock()) as mock_post:
        await _maybe_notify_github(
            client=mock_client,
            project_slug="stagehand",
            source_system="github",
            ticket_id="42",
            investigation_id="inv-42",
            standing_query_name="actionable-issue-pattern",
        )  # must not raise

    mock_post.assert_not_called()


# @spec SQ-GH-004
@pytest.mark.asyncio
async def test_maybe_notify_github_swallows_dre_failure():
    from modok.webhook.server import _maybe_notify_github

    fake_project = type("P", (), {"slug": "stagehand", "github_repo": "acme/stagehand"})()
    fake_config = type("C", (), {"projects": [fake_project]})()

    with patch("modok.cli.config.ModokConfig.load", return_value=fake_config), \
         patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}), \
         patch("modok.retrieval.engine.quick_investigation_summary", new=AsyncMock(return_value="quick take")), \
         patch("modok.retrieval.engine.retrieve", new=AsyncMock(side_effect=Exception("DRE unavailable"))), \
         patch("modok.ingestion.github.post_issue_comment", new=AsyncMock()) as mock_post:
        await _maybe_notify_github(
            client=AsyncMock(),
            project_slug="stagehand",
            source_system="github",
            ticket_id="42",
            investigation_id="inv-42",
            standing_query_name="actionable-issue-pattern",
        )  # must not raise

    # The fast "triggered" comment still posts even though the later,
    # slower retrieve() call (for the "results" comment) fails.
    assert mock_post.call_count == 1
    assert "investigation triggered" in mock_post.call_args_list[0].args[3]
