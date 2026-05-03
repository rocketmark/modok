import type { ModokRunsMap } from '@/types/modok-run'

const STALE_MS = 5 * 60 * 1000

export function resolveStaleRuns(runs: ModokRunsMap): ModokRunsMap {
  const now = Date.now()
  const out: ModokRunsMap = {}
  for (const [id, run] of Object.entries(runs)) {
    if (run.status === 'running' && run.ran_at && now - new Date(run.ran_at).getTime() > STALE_MS) {
      out[id] = { ...run, status: 'failed', error: 'timeout_or_crash' }
    } else {
      out[id] = run
    }
  }
  return out
}
