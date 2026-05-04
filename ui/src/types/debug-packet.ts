export interface IssueAnchors {
  features: string[]
  errors: string[]
  symptoms: string[]
}

export interface IssueSummary {
  summary: string
  anchors: IssueAnchors
}

export interface AffectedArea {
  type: string   // "feature" | "module"
  id: string
  name: string
}

export interface KnownIssueRef {
  id: string
  summary: string
}

export interface PriorFix {
  id: string
  commit: string
  summary: string
}

export interface RecentCommit {
  sha: string
  timestamp: string
  author_name: string
  message: string
  files_touched: string[]
}

export interface EvidenceItem {
  type: string
  score: number
  explanation: string
}

export interface ScoredCandidate {
  path: string
  kind: 'source' | 'test'
  score: number
  confidence: 'high' | 'medium' | 'low'
  evidence: EvidenceItem[]
}

export interface DebugPacket {
  issue: IssueSummary
  affected_areas: AffectedArea[]
  relevant_files: string[]
  relevant_tests: string[]
  known_issues: KnownIssueRef[]
  prior_fixes: PriorFix[]
  recent_commits?: RecentCommit[]
  scored_candidates?: ScoredCandidate[]
  summary?: string
}
