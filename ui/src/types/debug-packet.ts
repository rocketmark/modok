export interface AnchorSet {
  feature_slugs: string[]
  error_signatures: string[]
  symptoms: string[]
}

export interface DocRef {
  doc_path: string
  heading: string
  match_count: number
}

export interface FileRef {
  repo_path: string
  match_count: number
}

export interface KnownIssueRef {
  known_issue_id: string
  summary: string
  status: string
  match_count: number
}

export interface FixRef {
  fix_id: string
  summary: string
  kind: string
  match_count: number
}

export interface DebugPacket {
  issue: {
    id: string
    summary: string
  }
  anchors: AnchorSet
  relevant_docs: DocRef[]
  relevant_files: FileRef[]
  relevant_tests: FileRef[]
  known_issues: KnownIssueRef[]
  prior_fixes: FixRef[]
}
