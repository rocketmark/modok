from __future__ import annotations
from typing import Literal
from pydantic import BaseModel


class QuineNode(BaseModel):
    node_type: str


class Project(QuineNode):
    node_type: Literal["Project"]
    project_slug: str
    name: str


class Feature(QuineNode):
    node_type: Literal["Feature"]
    project_slug: str
    feature_slug: str
    name: str
    product_area_slug: str | None = None


class Module(QuineNode):
    node_type: Literal["Module"]
    project_slug: str
    module_slug: str
    name: str


class File(QuineNode):
    node_type: Literal["File"]
    project_slug: str
    repo_path: str


class TestFile(QuineNode):
    node_type: Literal["TestFile"]
    project_slug: str
    repo_path: str


class Doc(QuineNode):
    node_type: Literal["Doc"]
    project_slug: str
    doc_path: str
    doc_type: str
    feature_slug: str | None = None
    commit_sha: str | None = None


class DocSection(QuineNode):
    node_type: Literal["DocSection"]
    project_slug: str
    doc_path: str
    heading_slug: str
    heading_text: str
    doc_type: str
    line_start: int | None = None
    line_end: int | None = None


class ErrorSignature(QuineNode):
    node_type: Literal["ErrorSignature"]
    project_slug: str
    normalized_error: str
    display_text: str


class KnownIssue(QuineNode):
    node_type: Literal["KnownIssue"]
    project_slug: str
    issue_id: str
    summary: str
    status: str


class CustomerIssue(QuineNode):
    node_type: Literal["CustomerIssue"]
    project_slug: str
    source_system: str
    ticket_id: str
    summary: str
    raw_text: str | None = None
    status: str
    ticket_kind: str | None = None


class SimilarityMatch(QuineNode):
    node_type: Literal["SimilarityMatch"]
    project_slug: str
    customer_issue_id: str  # str repr of the CustomerIssue QuineNodeId
    known_issue_id: str  # str repr of the KnownIssue QuineNodeId
    method: str
    score: float
    evidence_anchors: list[str]
    review_status: str


class Fix(QuineNode):
    node_type: Literal["Fix"]
    project_slug: str
    fix_id: str
    summary: str
    kind: str
    pr_url: str | None = None


class ResolutionEvent(QuineNode):
    node_type: Literal["ResolutionEvent"]
    project_slug: str
    source_system: str
    ticket_id: str
    fix_id: str
    resolved_at: str


class DiagnosticNote(QuineNode):
    node_type: Literal["DiagnosticNote"]
    project_slug: str
    note_id: str
    body: str
    source: str
    created_at: str


class Commit(QuineNode):
    node_type: Literal["Commit"]
    project_slug: str
    sha: str
    timestamp: str
    author_name: str
    author_email: str
    message: str
    branch: str | None = None
    file_hunks: str = ""  # JSON: {file_path: [{lines, function, defs}]}


class Investigation(QuineNode):
    node_type: Literal["Investigation"]
    project_slug: str
    investigation_id: str
    status: str
    trigger_type: str
    triggered_at: str
    standing_query_name: str


# @spec CIING-NODE-001
class WorkflowRun(QuineNode):
    node_type: Literal["WorkflowRun"]
    project_slug: str
    run_id: str
    workflow_name: str
    head_sha: str
    head_branch: str
    event: str
    status: str
    conclusion: str | None = None
    run_number: int
    latest_run_attempt: int
    created_at: str
    updated_at: str
    url: str
    # Expansion state machine — decoupled from the discovery cursor so one bad
    # run can't block discovery of newer ones (see docs/llds/continuous-ci-ingestion.md
    # § Poll Cycle Extension).
    expansion_state: str = "discovered"
    expansion_attempts: int = 0
    expansion_last_error: str | None = None
    expansion_last_attempted_at: str | None = None


# @spec CIING-NODE-002, CIING-NODE-006
class WorkflowJob(QuineNode):
    node_type: Literal["WorkflowJob"]
    project_slug: str
    github_job_id: str
    run_id: str
    # Included in the natural key alongside github_job_id so identity stays
    # correct regardless of unverified GitHub behavior around job-ID reuse
    # across attempts, and so an earlier failing attempt's evidence survives
    # a later passing retry (see § Workflow Job Identity).
    run_attempt: int
    name: str
    status: str
    conclusion: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    url: str


# @spec CIING-NODE-003
class WorkflowJobStep(QuineNode):
    node_type: Literal["WorkflowJobStep"]
    project_slug: str
    run_id: str
    run_attempt: int
    github_job_id: str
    step_number: int
    name: str
    status: str
    conclusion: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


# @spec CIING-NODE-004
class TestExecution(QuineNode):
    node_type: Literal["TestExecution"]
    project_slug: str
    run_id: str
    run_attempt: int
    suite_name: str
    classname: str
    test_name: str
    status: str
    duration_seconds: float | None = None
    # @spec TCLINK-EDGE-003 — "resolved" | "ambiguous" | unset. A no-match
    # ("unresolved") classname->TestFile resolution result is never persisted
    # here; it stays unset so the reconciliation sweep retries it
    # indefinitely (docs/llds/test-coverage-ci-linking.md § Where Resolution
    # Runs, Cost caveat).
    link_state: str | None = None


# @spec CIING-NODE-005
class TestFailure(QuineNode):
    node_type: Literal["TestFailure"]
    project_slug: str
    run_id: str
    run_attempt: int
    classname: str
    test_name: str
    failure_type: str
    message: str
    assertion_text: str | None = None
    stack_trace_excerpt: str | None = None
    observed_at: str
    # Reserved for a future slice — TestFailure existence must never be
    # conflated with "currently failing" (see § Historical vs. Current Failure
    # State). No retraction logic in this slice; fields exist so a later slice
    # can populate them without a schema migration.
    latest_outcome: str | None = None
    superseded_by: str | None = None
    resolved_at: str | None = None
    is_current: bool | None = None


# @spec DEPG-NODE-001
class DependencyPackage(QuineNode):
    node_type: Literal["DependencyPackage"]
    project_slug: str
    purl: str
    ecosystem: str
    name: str


# @spec DEPG-NODE-002
class DependencyVersion(QuineNode):
    node_type: Literal["DependencyVersion"]
    project_slug: str
    package_purl: str
    version: str
    relationship: str = "unknown"


# @spec DEPG-NODE-003
class DependencyManifest(QuineNode):
    node_type: Literal["DependencyManifest"]
    project_slug: str
    manifest_path: str
    ecosystem: str
    format: str


# @spec DEPG-NODE-004
class DependencySnapshot(QuineNode):
    node_type: Literal["DependencySnapshot"]
    project_slug: str
    manifest_path: str
    commit_sha: str
    captured_at: str


# @spec DEPG-NODE-005
class DependencyChange(QuineNode):
    node_type: Literal["DependencyChange"]
    project_slug: str
    manifest_path: str
    package_purl: str
    commit_sha: str
    change_kind: str
    version_source: str
    observed_at: str


# @spec SQ-MILE-003
class InvestigationMilestone(QuineNode):
    node_type: Literal["InvestigationMilestone"]
    project_slug: str
    investigation_id: str = ""
    milestone_kind: str
    standing_query_name: str
    test_failure_id: str | None = None
    error_signature: str | None = None
    workflow_run_id: str | None = None
    created_at: str


_NODE_TYPE_MAP: dict[str, type[QuineNode]] = {
    "Project": Project,
    "Feature": Feature,
    "Module": Module,
    "File": File,
    "TestFile": TestFile,
    "DocSection": DocSection,
    "ErrorSignature": ErrorSignature,
    "KnownIssue": KnownIssue,
    "CustomerIssue": CustomerIssue,
    "SimilarityMatch": SimilarityMatch,
    "Fix": Fix,
    "ResolutionEvent": ResolutionEvent,
    "DiagnosticNote": DiagnosticNote,
    "Commit": Commit,
    "Investigation": Investigation,
    "WorkflowRun": WorkflowRun,
    "WorkflowJob": WorkflowJob,
    "WorkflowJobStep": WorkflowJobStep,
    "TestExecution": TestExecution,
    "TestFailure": TestFailure,
    "InvestigationMilestone": InvestigationMilestone,
    "DependencyPackage": DependencyPackage,
    "DependencyVersion": DependencyVersion,
    "DependencyManifest": DependencyManifest,
    "DependencySnapshot": DependencySnapshot,
    "DependencyChange": DependencyChange,
}
