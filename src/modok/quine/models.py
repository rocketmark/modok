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
}
