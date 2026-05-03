import type { DebugPacket } from './debug-packet'

export type ModokRunStatus = 'not_run' | 'running' | 'complete' | 'failed'

export type ModokRunError =
  | 'modok_not_found'
  | 'quine_unreachable'
  | 'issue_not_found'
  | 'timeout'
  | 'parse_error'
  | 'timeout_or_crash'

export interface ModokRun {
  ticket_id: string
  status: ModokRunStatus
  debug_packet?: DebugPacket
  ingest_exit_code?: number
  ingest_stderr?: string
  ingest_partial?: boolean
  retrieve_exit_code?: number
  retrieve_stderr?: string
  ran_at?: string // ISO 8601; set when status transitions to "running"
  mock?: boolean
  error?: ModokRunError
  raw_stdout?: string // populated when error === "parse_error"
}

export type ModokRunsMap = Record<string, ModokRun>
