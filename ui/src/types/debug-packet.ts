export interface AnchorSet {
  feature_slugs: string[]
  module_slugs: string[]
  error_signatures: string[]
  symptoms: string[]
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
  pr_url?: string | null
}

export interface CommitRef {
  sha: string
  message: string
  author_name: string
  timestamp: string
  files_touched: string[]
}

export interface EvidenceAnchor {
  anchor_type: string
  anchor_value: string
  matched_node_ids: string[]
}

export interface DebugPacket {
  issue_summary: string
  anchors: AnchorSet
  anchor_count: number
  known_issues: KnownIssueRef[]
  recent_fixes: FixRef[]
  relevant_files: FileRef[]
  recent_commits: CommitRef[]
  evidence: EvidenceAnchor[]
  confidence: number
  summary?: string
}
