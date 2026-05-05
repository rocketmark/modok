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

function spawnLineStreaming(
  cmd: string,
  args: string[],
  onLine: (line: string) => void,
  timeoutMs: number,
): Promise<SpawnResult> {
  return new Promise((resolve, reject) => {
    let timedOut = false
    const proc = spawn(cmd, args, { shell: false })
    let lineBuffer = ''
    let stdout = ''
    let stderr = ''

    const timer = setTimeout(() => {
      timedOut = true
      proc.kill('SIGTERM')
    }, timeoutMs)

    proc.stdout.on('data', (d: Buffer) => {
      const chunk = d.toString()
      stdout += chunk
      lineBuffer += chunk
      const lines = lineBuffer.split('\n')
      lineBuffer = lines.pop()!
      for (const line of lines) {
        if (line.trim()) onLine(line)
      }
    })
    proc.stderr.on('data', (d: Buffer) => { stderr += d.toString() })
    proc.on('close', (code) => {
      clearTimeout(timer)
      if (lineBuffer.trim()) onLine(lineBuffer)
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
  fs.writeFileSync(mdPath, renderTicketMarkdown(ticket, notes, config.modok_source))

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
  const retrieveArgs = ['retrieve', '--project', config.project_slug, '--ticket', ticketId]
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

export type StreamEvent =
  | { type: 'status'; message: string }
  | { type: 'partial'; packet: DebugPacket }
  | { type: 'complete'; run: ModokRun }

export async function runModokStream(
  ticketId: string,
  emit: (event: StreamEvent) => void,
): Promise<void> {
  const config = loadConfig()
  const tickets = readTickets()
  const ticket = tickets.find((t) => t.id === ticketId)
  if (!ticket) {
    emit({ type: 'complete', run: failedRun(ticketId, 'issue_not_found') })
    return
  }

  if (config.mockMode) {
    let packet: DebugPacket | undefined
    try {
      const fixturePath = path.join(process.cwd(), 'data', 'mock-debug-packets.json')
      const raw = fs.readFileSync(fixturePath, 'utf-8') as string
      if (raw) packet = JSON.parse(raw)
    } catch { /* fixture unavailable */ }
    const run: ModokRun = { ticket_id: ticketId, status: 'complete', debug_packet: packet, mock: true }
    writeRun(run)
    emit({ type: 'complete', run })
    return
  }

  const notes = readNotes(ticketId)
  writeRun({ ticket_id: ticketId, status: 'running', ran_at: new Date().toISOString() })
  emit({ type: 'status', message: 'Ingesting ticket…' })

  const ticketDir = path.join(process.cwd(), 'demo-data', 'customer-tickets')
  fs.mkdirSync(ticketDir, { recursive: true })
  const mdPath = path.join(ticketDir, `${ticketId}.md`)
  fs.writeFileSync(mdPath, renderTicketMarkdown(ticket, notes, config.modok_source))

  const ingestArgs = ['ingest', '--project', config.project_slug, mdPath]
  let ingestResult: SpawnResult

  try {
    ingestResult = await spawnWithTimeout('modok', ingestArgs, 60_000)
  } catch (err: unknown) {
    if ((err as NodeJS.ErrnoException).code === 'ENOENT') {
      emit({ type: 'complete', run: failedRun(ticketId, 'modok_not_found') })
      return
    }
    throw err
  }

  if (ingestResult.timedOut) { emit({ type: 'complete', run: failedRun(ticketId, 'timeout') }); return }
  if (ingestResult.exitCode === 2) {
    emit({ type: 'complete', run: failedRun(ticketId, 'quine_unreachable', { ingest_exit_code: 2, ingest_stderr: ingestResult.stderr }) })
    return
  }

  const ingestPartial = ingestResult.exitCode === 3
  emit({ type: 'status', message: 'Analyzing…' })

  const retrieveArgs = ['retrieve', '--project', config.project_slug, '--ticket', ticketId, '--stream']
  let lastPartialPacket: DebugPacket | undefined
  let retrieveStdout = ''

  let retrieveResult: SpawnResult
  try {
    retrieveResult = await spawnLineStreaming('modok', retrieveArgs, (line) => {
      retrieveStdout += line + '\n'
      try {
        const parsed = JSON.parse(line) as { step: string; data: DebugPacket }
        if (parsed.step !== 'complete') {
          lastPartialPacket = parsed.data
          emit({ type: 'partial', packet: parsed.data })
        }
      } catch { /* ignore non-JSON lines */ }
    }, 120_000)
  } catch (err: unknown) {
    if ((err as NodeJS.ErrnoException).code === 'ENOENT') {
      emit({ type: 'complete', run: failedRun(ticketId, 'modok_not_found') })
      return
    }
    throw err
  }

  if (retrieveResult.timedOut) {
    // If we got a partial packet before the timeout, return it as best-effort
    const run: ModokRun = lastPartialPacket
      ? { ticket_id: ticketId, status: 'complete', debug_packet: lastPartialPacket, ingest_exit_code: ingestResult.exitCode, ingest_partial: ingestPartial, retrieve_stderr: retrieveResult.stderr }
      : failedRun(ticketId, 'timeout')
    writeRun(run)
    emit({ type: 'complete', run })
    return
  }

  if (retrieveResult.exitCode === 1) {
    emit({ type: 'complete', run: failedRun(ticketId, 'issue_not_found', { retrieve_exit_code: 1, retrieve_stderr: retrieveResult.stderr }) })
    return
  }
  if (retrieveResult.exitCode === 2) {
    emit({ type: 'complete', run: failedRun(ticketId, 'quine_unreachable', { retrieve_exit_code: 2, retrieve_stderr: retrieveResult.stderr }) })
    return
  }

  // Extract the complete packet from the final NDJSON line
  let packet: DebugPacket | undefined
  for (const line of retrieveStdout.trim().split('\n').reverse()) {
    if (!line.trim()) continue
    try {
      const parsed = JSON.parse(line) as { step: string; data: DebugPacket }
      if (parsed.step === 'complete') { packet = parsed.data; break }
    } catch { /* skip */ }
  }

  if (!packet) {
    emit({ type: 'complete', run: failedRun(ticketId, 'parse_error', { raw_stdout: retrieveStdout, retrieve_exit_code: retrieveResult.exitCode, retrieve_stderr: retrieveResult.stderr }) })
    return
  }

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
  emit({ type: 'complete', run })
}
