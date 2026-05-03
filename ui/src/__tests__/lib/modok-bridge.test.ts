import * as fc from 'fast-check'
import { EventEmitter } from 'events'

jest.mock('child_process')
jest.mock('fs')
jest.mock('@/lib/config')
jest.mock('@/lib/data')

import { spawn } from 'child_process'
import { runModok } from '@/lib/modok-bridge'
import { loadConfig } from '@/lib/config'
import { readTickets, readNotes, writeRun, getRun } from '@/lib/data'

const mockSpawn = spawn as jest.MockedFunction<typeof spawn>
const mockLoadConfig = loadConfig as jest.MockedFunction<typeof loadConfig>
const mockReadTickets = readTickets as jest.MockedFunction<typeof readTickets>
const mockReadNotes = readNotes as jest.MockedFunction<typeof readNotes>
const mockWriteRun = writeRun as jest.MockedFunction<typeof writeRun>

const baseConfig = { project_slug: 'stagehand', modok_source: 'demo-crm', mockMode: false }

const makeSpawnMock = (stdout: string, exitCode: number) => {
  const proc = new EventEmitter() as any
  proc.stdout = new EventEmitter()
  proc.stderr = new EventEmitter()
  proc.kill = jest.fn()
  setTimeout(() => {
    proc.stdout.emit('data', stdout)
    proc.emit('close', exitCode)
  }, 0)
  return proc
}

const validPacketJson = JSON.stringify({ issue_summary: 'y', anchors: { feature_slugs: [], error_signatures: [], symptoms: [] }, anchor_count: 0, relevant_files: [], known_issues: [], recent_fixes: [], evidence: [], confidence: 0.0 })
const baseTicket = { id: 'ACME-1842', subject: 'S', content: 'C', created_at: '2024-01-15T10:00:00Z' }

beforeEach(() => {
  jest.resetAllMocks()
  mockLoadConfig.mockReturnValue(baseConfig)
  mockReadTickets.mockReturnValue([baseTicket])
  mockReadNotes.mockReturnValue([])
})

// @spec DEMO-BRIDGE-003
it('writes running status before spawning', async () => {
  const calls: string[] = []
  mockWriteRun.mockImplementation((run) => { calls.push(run.status) })
  mockSpawn
    .mockReturnValueOnce(makeSpawnMock('', 0))
    .mockReturnValueOnce(makeSpawnMock(validPacketJson, 0))

  await runModok('ACME-1842')
  expect(calls[0]).toBe('running')
})

// @spec DEMO-BRIDGE-004 [P]
it('spawns ingest with shell:false for any ticket ID', async () => {
  await fc.assert(
    fc.asyncProperty(fc.string({ minLength: 1 }), async (ticketId) => {
      jest.clearAllMocks()
      mockLoadConfig.mockReturnValue(baseConfig)
      mockReadTickets.mockReturnValue([{ ...baseTicket, id: ticketId }])
      mockReadNotes.mockReturnValue([])
      mockSpawn
        .mockReturnValueOnce(makeSpawnMock('', 0) as any)
        .mockReturnValueOnce(makeSpawnMock('not-json', 0) as any)
      const call = mockSpawn.mock.calls[0]
      // spawn is called synchronously; check before awaiting
      runModok(ticketId).catch(() => {})
      const firstCall = mockSpawn.mock.calls[0]
      expect(firstCall).toBeDefined()
      const opts = firstCall[2] as any
      expect(opts?.shell).toBeFalsy()
      expect(Array.isArray(firstCall[1])).toBe(true)
      // drain all async work to avoid dangling timers
      await new Promise<void>((r) => setTimeout(r, 10))
    })
  )
})

// @spec DEMO-BRIDGE-005 [P]
it('spawns retrieve with shell:false for any ticket ID', async () => {
  await fc.assert(
    fc.asyncProperty(fc.string({ minLength: 1 }), async (ticketId) => {
      jest.clearAllMocks()
      mockLoadConfig.mockReturnValue(baseConfig)
      mockReadTickets.mockReturnValue([{ ...baseTicket, id: ticketId }])
      mockReadNotes.mockReturnValue([])
      mockSpawn
        .mockReturnValueOnce(makeSpawnMock('', 0) as any)
        .mockReturnValueOnce(makeSpawnMock(validPacketJson, 0) as any)
      await runModok(ticketId).catch(() => {})
      const retrieveCall = mockSpawn.mock.calls[1]
      if (retrieveCall) {
        expect((retrieveCall[2] as any)?.shell).toBeFalsy()
        expect(Array.isArray(retrieveCall[1])).toBe(true)
      }
    })
  )
})

// @spec DEMO-BRIDGE-007
it('returns modok_not_found error when spawn throws ENOENT', async () => {
  mockSpawn.mockImplementation(() => { throw Object.assign(new Error(), { code: 'ENOENT' }) })
  const result = await runModok('ACME-1842')
  expect(result.error).toBe('modok_not_found')
  expect(result.status).toBe('failed')
})

// @spec DEMO-BRIDGE-008
it('returns quine_unreachable and does not spawn retrieve when ingest exits 2', async () => {
  mockSpawn.mockReturnValueOnce(makeSpawnMock('', 2) as any)
  const result = await runModok('ACME-1842')
  expect(result.error).toBe('quine_unreachable')
  expect(mockSpawn).toHaveBeenCalledTimes(1)
})

// @spec DEMO-BRIDGE-009
it('proceeds to retrieve and sets ingest_partial when ingest exits 3', async () => {
  mockSpawn
    .mockReturnValueOnce(makeSpawnMock('', 3) as any)
    .mockReturnValueOnce(makeSpawnMock(validPacketJson, 0) as any)
  const result = await runModok('ACME-1842')
  expect(mockSpawn).toHaveBeenCalledTimes(2)
  expect(result.ingest_partial).toBe(true)
})

// @spec DEMO-BRIDGE-010
it('returns issue_not_found when retrieve exits 1', async () => {
  mockSpawn
    .mockReturnValueOnce(makeSpawnMock('', 0) as any)
    .mockReturnValueOnce(makeSpawnMock('', 1) as any)
  const result = await runModok('ACME-1842')
  expect(result.error).toBe('issue_not_found')
  expect(result.status).toBe('failed')
})

// @spec DEMO-BRIDGE-011
it('returns quine_unreachable when retrieve exits 2', async () => {
  mockSpawn
    .mockReturnValueOnce(makeSpawnMock('', 0) as any)
    .mockReturnValueOnce(makeSpawnMock('', 2) as any)
  const result = await runModok('ACME-1842')
  expect(result.error).toBe('quine_unreachable')
})

// @spec DEMO-BRIDGE-012
it('returns parse_error with raw_stdout when retrieve exits 0 with non-JSON', async () => {
  mockSpawn
    .mockReturnValueOnce(makeSpawnMock('', 0) as any)
    .mockReturnValueOnce(makeSpawnMock('not json', 0) as any)
  const result = await runModok('ACME-1842')
  expect(result.error).toBe('parse_error')
  expect(result.raw_stdout).toBe('not json')
})

// @spec DEMO-BRIDGE-015
it('proceeds to retrieve when ingest exits 0', async () => {
  mockSpawn
    .mockReturnValueOnce(makeSpawnMock('', 0) as any)
    .mockReturnValueOnce(makeSpawnMock(validPacketJson, 0) as any)
  await runModok('ACME-1842')
  expect(mockSpawn).toHaveBeenCalledTimes(2)
})

// @spec DEMO-BRIDGE-014
it('returns complete run with parsed debug packet on success', async () => {
  mockSpawn
    .mockReturnValueOnce(makeSpawnMock('', 0) as any)
    .mockReturnValueOnce(makeSpawnMock(validPacketJson, 0) as any)
  const result = await runModok('ACME-1842')
  expect(result.status).toBe('complete')
  expect(result.debug_packet).toBeDefined()
})

// @spec DEMO-BRIDGE-002
it('returns mock run without spawning when MODOK_MOCK=1', async () => {
  mockLoadConfig.mockReturnValue({ ...baseConfig, mockMode: true })
  const result = await runModok('ACME-1842')
  expect(mockSpawn).not.toHaveBeenCalled()
  expect(result.mock).toBe(true)
  expect(result.status).toBe('complete')
})

describe('stale run detection', () => {
  // @spec DEMO-STALE-001
  it('treats a running run older than 5 minutes as failed', () => {
    const staleAt = new Date(Date.now() - 6 * 60 * 1000).toISOString()
    const staleRun = { ticket_id: 'X', status: 'running' as const, ran_at: staleAt }
    const { resolveStaleRuns } = require('@/lib/modok-bridge')
    const runs = { X: staleRun }
    const resolved = resolveStaleRuns(runs)
    expect(resolved.X.status).toBe('failed')
    expect(resolved.X.error).toBe('timeout_or_crash')
  })

  // @spec DEMO-STALE-001
  it('does not modify a running run that is less than 5 minutes old', () => {
    const freshAt = new Date(Date.now() - 2 * 60 * 1000).toISOString()
    const run = { ticket_id: 'X', status: 'running' as const, ran_at: freshAt }
    const { resolveStaleRuns } = require('@/lib/modok-bridge')
    const resolved = resolveStaleRuns({ X: run })
    expect(resolved.X.status).toBe('running')
  })

  // @spec DEMO-STALE-002 [P]
  it('always detects and overwrites stale entries for any runs map containing them', () => {
    const { resolveStaleRuns } = require('@/lib/modok-bridge')
    fc.assert(
      fc.property(
        fc.dictionary(
          fc.string({ minLength: 1 }),
          fc.record({
            ticket_id: fc.string(),
            status: fc.constant('running' as const),
            ran_at: fc.date({ max: new Date(Date.now() - 6 * 60 * 1000) }).map(d => d.toISOString()),
          })
        ),
        (staleRuns) => {
          fc.pre(Object.keys(staleRuns).length > 0)
          const resolved = resolveStaleRuns(staleRuns)
          for (const key of Object.keys(staleRuns)) {
            expect(resolved[key].status).toBe('failed')
          }
        }
      )
    )
  })
})
