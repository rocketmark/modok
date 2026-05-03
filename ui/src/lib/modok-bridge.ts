import { spawn } from 'child_process'
import fs from 'fs'
import path from 'path'
import { loadConfig, ConfigError } from '@/lib/config'
import { readTickets, readNotes, writeRun } from '@/lib/data'
import { renderTicketMarkdown } from '@/lib/markdown'
import type { ModokRun } from '@/types/modok-run'
import type { DebugPacket } from '@/types/debug-packet'

export { resolveStaleRuns } from '@/lib/stale'

interface SpawnResult {
  stdout: string
  stderr: string
  exitCode: number
  timedOut: boolean
}

function spawnWithTimeout(cmd: string, args: string[], timeoutMs: number): Promise<SpawnResult> {
  return new Promise((resolve, reject) => {
    let timedOut = false
    const proc = spawn(cmd, args, { shell: false })
    let stdout = ''
    let stderr = ''

    const timer = setTimeout(() => {
      timedOut = true
      proc.kill('SIGTERM')
    }, timeoutMs)

    proc.stdout.on('data', (d: Buffer) => { stdout += d.toString() })
    proc.stderr.on('data', (d: Buffer) => { stderr += d.toString() })
    proc.on('close', (code) => {
      clearTimeout(timer)
      resolve({ stdout, stderr, exitCode: code ?? -1, timedOut })
    })
    proc.on('error', (err) => {
      clearTimeout(timer)
      reject(err)
    })
  })
}

function failedRun(ticketId: string, error: ModokRun['error'], extra?: Partial<ModokRun>): ModokRun {
  return { ticket_id: ticketId, status: 'failed', error, ...extra }
}

// @spec DEMO-BRIDGE-002, DEMO-BRIDGE-003, DEMO-BRIDGE-004, DEMO-BRIDGE-005,
//       DEMO-BRIDGE-006, DEMO-BRIDGE-007, DEMO-BRIDGE-008, DEMO-BRIDGE-009,
//       DEMO-BRIDGE-010, DEMO-BRIDGE-011, DEMO-BRIDGE-012, DEMO-BRIDGE-013,
//       DEMO-BRIDGE-014, DEMO-BRIDGE-015
export async function runModok(ticketId: string): Promise<ModokRun> {
  const config = loadConfig()
  const tickets = readTickets()
  const ticket = tickets.find((t) => t.id === ticketId)
  if (!ticket) {
    const run = failedRun(ticketId, 'issue_not_found')
    writeRun(run)
    return run
  }

  // @spec DEMO-BRIDGE-002
  if (config.mockMode) {
    let packet: DebugPacket | undefined
    try {
      const fixturePath = path.join(process.cwd(), 'data', 'mock-debug-packets.json')
      const raw = fs.readFileSync(fixturePath, 'utf-8') as string
      if (raw) packet = JSON.parse(raw)
    } catch { /* fixture unavailable in test or offline mode */ }
    const run: ModokRun = { ticket_id: ticketId, status: 'complete', debug_packet: packet, mock: true }
    writeRun(run)
    return run
  }

  const notes = readNotes(ticketId)

  // @spec DEMO-BRIDGE-003
  writeRun({ ticket_id: ticketId, status: 'running', ran_at: new Date().toISOString() })

  // @spec DEMO-BRIDGE-013
  const ticketDir = path.join(process.cwd(), 'demo-data', 'customer-tickets')
  fs.mkdirSync(ticketDir, { recursive: true })
  const mdPath = path.join(ticketDir, `${ticketId}.md`)
  fs.writeFileSync(mdPath, renderTicketMarkdown(ticket, notes))

  const ingestArgs = ['ingest', '--project', config.project_slug, mdPath]
  let ingestResult: SpawnResult

  try {
    // @spec DEMO-BRIDGE-004, DEMO-BRIDGE-006
    ingestResult = await spawnWithTimeout('modok', ingestArgs, 60_000)
  } catch (err: unknown) {
    // @spec DEMO-BRIDGE-007
    if ((err as NodeJS.ErrnoException).code === 'ENOENT') {
      const run = failedRun(ticketId, 'modok_not_found')
      writeRun(run)
      return run
    }
    throw err
  }

  if (ingestResult.timedOut) {
    const run = failedRun(ticketId, 'timeout')
    writeRun(run)
    return run
  }

  // @spec DEMO-BRIDGE-008
  if (ingestResult.exitCode === 2) {
    const run = failedRun(ticketId, 'quine_unreachable', { ingest_exit_code: 2, ingest_stderr: ingestResult.stderr })
    writeRun(run)
    return run
  }

  // @spec DEMO-BRIDGE-009
  const ingestPartial = ingestResult.exitCode === 3

  // @spec DEMO-BRIDGE-015 (exit 0) and DEMO-BRIDGE-009 (exit 3) both proceed here
  const retrieveArgs = ['retrieve', '--project', config.project_slug, '--source', config.modok_source, '--ticket', ticketId]
  let retrieveResult: SpawnResult

  try {
    // @spec DEMO-BRIDGE-005, DEMO-BRIDGE-006
    retrieveResult = await spawnWithTimeout('modok', retrieveArgs, 60_000)
  } catch (err: unknown) {
    if ((err as NodeJS.ErrnoException).code === 'ENOENT') {
      const run = failedRun(ticketId, 'modok_not_found')
      writeRun(run)
      return run
    }
    throw err
  }

  if (retrieveResult.timedOut) {
    const run = failedRun(ticketId, 'timeout')
    writeRun(run)
    return run
  }

  // @spec DEMO-BRIDGE-010
  if (retrieveResult.exitCode === 1) {
    const run = failedRun(ticketId, 'issue_not_found', { retrieve_exit_code: 1, retrieve_stderr: retrieveResult.stderr })
    writeRun(run)
    return run
  }

  // @spec DEMO-BRIDGE-011
  if (retrieveResult.exitCode === 2) {
    const run = failedRun(ticketId, 'quine_unreachable', { retrieve_exit_code: 2, retrieve_stderr: retrieveResult.stderr })
    writeRun(run)
    return run
  }

  let packet: DebugPacket
  try {
    packet = JSON.parse(retrieveResult.stdout)
  } catch {
    // @spec DEMO-BRIDGE-012
    const run = failedRun(ticketId, 'parse_error', {
      raw_stdout: retrieveResult.stdout,
      retrieve_exit_code: retrieveResult.exitCode,
      retrieve_stderr: retrieveResult.stderr,
    })
    writeRun(run)
    return run
  }

  // @spec DEMO-BRIDGE-014
  const run: ModokRun = {
    ticket_id: ticketId,
    status: 'complete',
    debug_packet: packet,
    ingest_exit_code: ingestResult.exitCode,
    ingest_stderr: ingestResult.stderr,
    ingest_partial: ingestPartial,
    retrieve_exit_code: retrieveResult.exitCode,
    retrieve_stderr: retrieveResult.stderr,
  }
  writeRun(run)
  return run
}
