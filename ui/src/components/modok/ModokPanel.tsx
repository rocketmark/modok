'use client'

import type { ModokRun } from '@/types/modok-run'
import { DebugPacketView } from './DebugPacketView'

const ERROR_MESSAGES: Record<string, string> = {
  modok_not_found: 'modok CLI not found. Is it installed and on PATH?',
  quine_unreachable: 'MODOK could not reach Quine. Try: modok quine start',
  issue_not_found: 'MODOK could not find this issue in the project graph.',
  timeout: 'MODOK timed out after 60s.',
  timeout_or_crash: 'MODOK timed out or crashed.',
  parse_error: 'MODOK returned unexpected output.',
}

interface Props {
  ticketId: string
  run: ModokRun
  onRun: () => void
  onClear: () => void
  isRunning?: boolean
}

// @spec DEMO-MODOK-001, DEMO-MODOK-002, DEMO-MODOK-003, DEMO-MODOK-004, DEMO-MODOK-005,
//       DEMO-MODOK-006, DEMO-MODOK-007, DEMO-MODOK-008, DEMO-MODOK-009, DEMO-MODOK-010, DEMO-MODOK-011
export function ModokPanel({ ticketId: _ticketId, run, onRun, onClear, isRunning = false }: Props) {
  return (
    <div className="flex flex-col h-full overflow-y-auto p-5">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
          MODOK Analysis
        </h2>
        {!isRunning && (run.status === 'complete' || run.status === 'failed') && (
          <button
            onClick={onClear}
            className="text-xs text-slate-400 hover:text-slate-600 transition-colors"
          >
            Clear
          </button>
        )}
      </div>

      {isRunning && !run.debug_packet ? (
        <div role="status" className="flex items-center gap-2 text-sm text-slate-500">
          <span className="inline-block w-4 h-4 border-2 border-blue-500 border-t-transparent rounded-full animate-spin" />
          Running…
        </div>
      ) : (isRunning || run.status === 'complete') && run.debug_packet ? (
        <>
          {isRunning && (
            <div className="mb-4 flex items-center gap-2 text-xs text-slate-400">
              <span className="inline-block w-3 h-3 border-2 border-blue-400 border-t-transparent rounded-full animate-spin" />
              Generating summary…
            </div>
          )}
          {run.ingest_partial && (
            <div className="mb-4 px-3 py-2 bg-amber-50 border border-amber-200 rounded text-sm text-amber-700">
              Ingestion completed with errors — results may be incomplete.
            </div>
          )}
          <DebugPacketView packet={run.debug_packet} />
        </>
      ) : run.status === 'failed' ? (
        <div className="space-y-3">
          <p className="text-sm text-red-600">
            {run.error
              ? (ERROR_MESSAGES[run.error] ?? `Error: ${run.error}`)
              : 'MODOK run failed.'}
          </p>
          {run.error === 'parse_error' && run.raw_stdout && (
            <pre className="p-3 bg-slate-50 border border-slate-200 rounded text-xs text-slate-600 overflow-x-auto whitespace-pre-wrap">
              {run.raw_stdout}
            </pre>
          )}
          <button
            onClick={onRun}
            className="px-4 py-2 text-sm bg-blue-600 text-white rounded hover:bg-blue-700 transition-colors"
          >
            Build Debug Packet
          </button>
        </div>
      ) : (
        <button
          onClick={onRun}
          className="px-4 py-2 text-sm bg-blue-600 text-white rounded hover:bg-blue-700 transition-colors"
        >
          Build Debug Packet
        </button>
      )}
    </div>
  )
}
