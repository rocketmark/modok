import type { DebugPacket, RecentCommit, ScoredCandidate } from '@/types/debug-packet'
import { PacketSection } from './PacketSection'
import { RawJsonCollapsible } from './RawJsonCollapsible'

interface Props {
  packet: DebugPacket
}

const CONFIDENCE_STYLES: Record<string, string> = {
  high: 'bg-red-50 text-red-700 border-red-200',
  medium: 'bg-amber-50 text-amber-700 border-amber-200',
  low: 'bg-slate-50 text-slate-500 border-slate-200',
}

function fmtDate(ts: string): string {
  try {
    return new Date(ts).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
  } catch {
    return ts.slice(0, 10)
  }
}

function shaFrom(explanation: string): string {
  return explanation.split('commit ')[1] ?? ''
}

function CandidateRow({
  candidate,
  commitMeta,
}: {
  candidate: ScoredCandidate
  commitMeta: Record<string, RecentCommit>
}) {
  const badge = CONFIDENCE_STYLES[candidate.confidence] ?? CONFIDENCE_STYLES.low

  const recentCommitEvidence = candidate.evidence.filter(ev => ev.type === 'recent_commit')
  const otherEvidence = candidate.evidence.filter(ev => ev.type !== 'recent_commit')

  const mostRecentSha = recentCommitEvidence[0] ? shaFrom(recentCommitEvidence[0].explanation) : ''
  const mostRecentMeta = commitMeta[mostRecentSha]
  const additionalEvidence = recentCommitEvidence.slice(1)

  // Pull touched function from function_anchor_match if it names the most recent commit
  const anchorMatch = candidate.evidence.find(
    ev => ev.type === 'function_anchor_match' && ev.explanation.includes(mostRecentSha)
  )
  const touchedFn = anchorMatch
    ? (anchorMatch.explanation.split('Defines ')[1]?.split(' in ')[0] ?? null)
    : null

  return (
    <li className="border border-slate-100 rounded p-2 space-y-1.5">
      <div className="flex items-center gap-2">
        <span className={`text-xs font-medium px-1.5 py-0.5 rounded border ${badge}`}>
          {candidate.confidence}
        </span>
        <span className="font-mono text-xs text-slate-700 flex-1 truncate">{candidate.path}</span>
        <span className="text-xs text-slate-400 flex-shrink-0">score {candidate.score}</span>
      </div>
      {(otherEvidence.length > 0 || recentCommitEvidence.length > 0) && (
        <ul className="space-y-0.5">
          {otherEvidence.map((ev, i) => (
            <li key={i} className={`text-xs flex gap-1.5 ${ev.score < 0 ? 'text-orange-500' : 'text-slate-500'}`}>
              <span className={ev.score < 0 ? 'text-orange-300' : 'text-slate-300'}>·</span>
              <span className="flex-shrink-0 font-medium">{ev.type}</span>
              <span className="flex-1">{ev.explanation}</span>
              {ev.score < 0 && <span className="flex-shrink-0 font-mono">{ev.score}</span>}
            </li>
          ))}
          {recentCommitEvidence.length > 0 && (
            <li className="text-xs flex gap-1.5 text-slate-500">
              <span className="text-slate-300">·</span>
              <span className="flex-shrink-0 font-medium">recent_commit</span>
              <div className="flex-1 space-y-0.5">
                <div>
                  <span className="font-mono">{mostRecentSha}</span>
                  {mostRecentMeta && (
                    <>
                      <span className="text-slate-400"> · {fmtDate(mostRecentMeta.timestamp)} · {mostRecentMeta.author_name}</span>
                      {touchedFn && <span className="text-slate-400"> · fn: {touchedFn}</span>}
                      <span className="text-slate-600"> — {mostRecentMeta.message.slice(0, 60)}{mostRecentMeta.message.length > 60 ? '…' : ''}</span>
                    </>
                  )}
                </div>
                {additionalEvidence.length > 0 && (
                  <ul className="space-y-0.5 text-slate-400">
                    {additionalEvidence.map((ev, i) => {
                      const sha = shaFrom(ev.explanation)
                      const meta = commitMeta[sha]
                      return (
                        <li key={i}>
                          <span className="font-mono">{sha}</span>
                          {meta && (
                            <>
                              <span> · {fmtDate(meta.timestamp)} · {meta.author_name}</span>
                              <span> — {meta.message.slice(0, 60)}{meta.message.length > 60 ? '…' : ''}</span>
                            </>
                          )}
                        </li>
                      )
                    })}
                  </ul>
                )}
              </div>
            </li>
          )}
        </ul>
      )}
    </li>
  )
}

// @spec DEMO-MODOK-004, DEMO-MODOK-005, DEMO-MODOK-006, DEMO-MODOK-011
export function DebugPacketView({ packet }: Props) {
  const { issue, affected_areas, relevant_files, relevant_tests, known_issues, prior_fixes, recent_commits, scored_candidates, summary } = packet

  const commitMeta: Record<string, RecentCommit> = {}
  for (const c of recent_commits ?? []) {
    commitMeta[c.sha.slice(0, 7)] = c
  }
  const anchors = issue.anchors
  const hasAnchors =
    anchors.features.length > 0 ||
    anchors.errors.length > 0 ||
    anchors.symptoms.length > 0

  return (
    <div>
      {summary && (
        <div className="mb-5 p-3 bg-slate-50 border border-slate-200 rounded text-sm text-slate-700">
          <p className="leading-relaxed">{summary}</p>
        </div>
      )}

      <p className="text-sm font-medium text-slate-800 mb-4">{issue.summary}</p>

      {hasAnchors && (
        <PacketSection title="Anchors">
          <div className="space-y-1 text-sm text-slate-700">
            {anchors.features.length > 0 && (
              <p>
                <span className="text-xs text-slate-500">Features: </span>
                {anchors.features.join(', ')}
              </p>
            )}
            {anchors.errors.length > 0 && (
              <p>
                <span className="text-xs text-slate-500">Errors: </span>
                {anchors.errors.join(', ')}
              </p>
            )}
            {anchors.symptoms.length > 0 && (
              <p>
                <span className="text-xs text-slate-500">Symptoms: </span>
                {anchors.symptoms.join(', ')}
              </p>
            )}
          </div>
        </PacketSection>
      )}

      {affected_areas.length > 0 && (
        <PacketSection title="Affected Areas">
          <div className="flex flex-wrap gap-1.5">
            {affected_areas.map((area) => (
              <span
                key={area.id}
                className="text-xs px-2 py-0.5 rounded bg-blue-50 text-blue-700"
              >
                {area.type === 'feature' ? '⬡' : '○'} {area.name}
              </span>
            ))}
          </div>
        </PacketSection>
      )}

      {scored_candidates && scored_candidates.length > 0 && (
        <PacketSection title="Top Suspects">
          <ul className="space-y-2">
            {scored_candidates.map((c, i) => (
              <CandidateRow key={i} candidate={c} commitMeta={commitMeta} />
            ))}
          </ul>
        </PacketSection>
      )}

      {relevant_files.length > 0 && (
        <PacketSection title="Code Files">
          <ul className="space-y-1">
            {relevant_files.map((path, i) => (
              <li key={i} className="font-mono text-xs text-slate-600 bg-slate-50 px-2 py-1 rounded">
                {path}
              </li>
            ))}
          </ul>
        </PacketSection>
      )}

      {relevant_tests.length > 0 && (
        <PacketSection title="Test Files">
          <ul className="space-y-1">
            {relevant_tests.map((path, i) => (
              <li key={i} className="font-mono text-xs text-slate-600 bg-slate-50 px-2 py-1 rounded">
                {path}
              </li>
            ))}
          </ul>
        </PacketSection>
      )}

      {known_issues.length > 0 && (
        <PacketSection title="Known Issues">
          <ul className="space-y-1.5">
            {known_issues.map((ki) => (
              <li key={ki.id} className="text-sm text-slate-700">{ki.summary}</li>
            ))}
          </ul>
        </PacketSection>
      )}

      {prior_fixes.length > 0 && (
        <PacketSection title="Prior Fixes">
          <ul className="space-y-1.5">
            {prior_fixes.map((fix) => (
              <li key={fix.id} className="flex items-start gap-2 text-sm">
                {fix.commit && (
                  <span className="font-mono text-xs px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 flex-shrink-0">
                    {fix.commit}
                  </span>
                )}
                <span className="text-slate-700">{fix.summary}</span>
              </li>
            ))}
          </ul>
        </PacketSection>
      )}

      {recent_commits && recent_commits.length > 0 && (
        <PacketSection title="Recent Commits">
          <ul className="space-y-2">
            {recent_commits.map((commit) => (
              <li key={commit.sha} className="text-sm">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-xs px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 flex-shrink-0">
                    {commit.sha.slice(0, 7)}
                  </span>
                  <span className="text-xs text-slate-400 flex-shrink-0">{commit.timestamp.slice(0, 10)}</span>
                  <span className="text-xs text-slate-500 flex-shrink-0">{commit.author_name}</span>
                </div>
                <p className="mt-0.5 text-slate-700 leading-snug">{commit.message}</p>
              </li>
            ))}
          </ul>
        </PacketSection>
      )}

      <RawJsonCollapsible data={packet} defaultOpen={!summary} />
    </div>
  )
}
