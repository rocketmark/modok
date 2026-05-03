export interface AnchorSet {
  feature_slugs: string[]
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
  evidence: EvidenceAnchor[]
  confidence: number
}
