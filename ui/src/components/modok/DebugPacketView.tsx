import type { DebugPacket } from '@/types/debug-packet'
import { PacketSection } from './PacketSection'
import { RawJsonCollapsible } from './RawJsonCollapsible'

interface Props {
  packet: DebugPacket
}

// @spec DEMO-MODOK-004, DEMO-MODOK-005, DEMO-MODOK-006, DEMO-MODOK-011
export function DebugPacketView({ packet }: Props) {
  const { anchors, relevant_docs, relevant_files, known_issues, prior_fixes } = packet
  const hasAnchors =
    anchors.feature_slugs.length > 0 ||
    anchors.error_signatures.length > 0 ||
    anchors.symptoms.length > 0

  return (
    <div>
      <p className="text-sm font-medium text-slate-800 mb-4">{packet.issue.summary}</p>

      {hasAnchors && (
        <PacketSection title="Anchors">
          <div className="space-y-1 text-sm text-slate-700">
            {anchors.feature_slugs.length > 0 && (
              <p>
                <span className="text-xs text-slate-500">Features: </span>
                {anchors.feature_slugs.join(', ')}
              </p>
            )}
            {anchors.error_signatures.length > 0 && (
              <p>
                <span className="text-xs text-slate-500">Errors: </span>
                {anchors.error_signatures.join(', ')}
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

      {relevant_docs.length > 0 && (
        <PacketSection title="Relevant Docs">
          <ul className="space-y-1">
            {relevant_docs.map((d, i) => (
              <li key={i} className="text-sm text-slate-700">
                <span className="font-mono text-xs text-slate-500">{d.doc_path}</span>
                {d.heading && <span className="text-slate-400 ml-1">— {d.heading}</span>}
              </li>
            ))}
          </ul>
        </PacketSection>
      )}

      {relevant_files.length > 0 && (
        <PacketSection title="Code Files">
          <ul className="space-y-1">
            {relevant_files.map((f, i) => (
              <li
                key={i}
                className="font-mono text-xs text-slate-600 bg-slate-50 px-2 py-1 rounded"
              >
                {f.repo_path}
              </li>
            ))}
          </ul>
        </PacketSection>
      )}

      {known_issues.length > 0 && (
        <PacketSection title="Known Issues">
          <ul className="space-y-1.5">
            {known_issues.map((ki) => (
              <li key={ki.known_issue_id} className="flex items-start gap-2 text-sm">
                <span
                  className={`text-xs px-1.5 py-0.5 rounded flex-shrink-0 ${
                    ki.status === 'open'
                      ? 'bg-red-50 text-red-700'
                      : 'bg-slate-100 text-slate-600'
                  }`}
                >
                  {ki.status}
                </span>
                <span className="text-slate-700">{ki.summary}</span>
              </li>
            ))}
          </ul>
        </PacketSection>
      )}

      {prior_fixes.length > 0 && (
        <PacketSection title="Prior Fixes">
          <ul className="space-y-1.5">
            {prior_fixes.map((fix) => (
              <li key={fix.fix_id} className="flex items-start gap-2 text-sm">
                <span className="text-xs px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-700 flex-shrink-0">
                  {fix.kind}
                </span>
                <span className="text-slate-700">{fix.summary}</span>
              </li>
            ))}
          </ul>
        </PacketSection>
      )}

      <RawJsonCollapsible data={packet} />
    </div>
  )
}
