import type { DebugPacket } from '@/types/debug-packet'
import { PacketSection } from './PacketSection'
import { RawJsonCollapsible } from './RawJsonCollapsible'

interface Props {
  packet: DebugPacket
}

// @spec DEMO-MODOK-004, DEMO-MODOK-005, DEMO-MODOK-006, DEMO-MODOK-011
export function DebugPacketView({ packet }: Props) {
  const { issue, affected_areas, relevant_files, relevant_tests, known_issues, prior_fixes, recent_commits, summary } = packet
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

      <RawJsonCollapsible data={packet} />
    </div>
  )
}
