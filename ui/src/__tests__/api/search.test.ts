/**
 * @jest-environment node
 */
import { EventEmitter } from 'events'

jest.mock('child_process')
jest.mock('@/lib/config')

import { spawn } from 'child_process'
import { loadConfig } from '@/lib/config'

// Route handler not implemented yet — import will fail until Phase 6.
import { GET as getSearch } from '@/app/api/search/route'

const mockSpawn = spawn as jest.MockedFunction<typeof spawn>
const mockLoadConfig = loadConfig as jest.MockedFunction<typeof loadConfig>

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

const validSearchResult = JSON.stringify({ project: 'stagehand', nodes: [{ id: 1, node_type: 'Feature', name: 'checkout', summary: 'Checkout flow' }] })

beforeEach(() => {
  jest.resetAllMocks()
  mockLoadConfig.mockReturnValue(baseConfig)
})

// @spec DEMO-SEARCH-006
it('spawns modok search with shell:false and returns parsed JSON', async () => {
  mockSpawn.mockReturnValue(makeSpawnMock(validSearchResult, 0) as any)
  const res = await getSearch(new Request('http://localhost/api/search?q=checkout'))
  expect(res.status).toBe(200)
  const data = await res.json()
  expect(data.nodes).toHaveLength(1)
  const call = mockSpawn.mock.calls[0]
  expect((call[2] as any)?.shell).toBeFalsy()
  expect(Array.isArray(call[1])).toBe(true)
  expect(call[1]).toContain('checkout')
})

// @spec DEMO-SEARCH-007
it('returns 400 when q param is absent', async () => {
  const res = await getSearch(new Request('http://localhost/api/search'))
  expect(res.status).toBe(400)
})

// @spec DEMO-SEARCH-007
it('returns 400 when q param is empty string', async () => {
  const res = await getSearch(new Request('http://localhost/api/search?q='))
  expect(res.status).toBe(400)
})

// @spec DEMO-SEARCH-009
it('returns 502 with exit code and stderr when modok search exits non-zero', async () => {
  mockSpawn.mockReturnValue(makeSpawnMock('', 2) as any)
  const res = await getSearch(new Request('http://localhost/api/search?q=checkout'))
  expect(res.status).toBe(502)
  const body = await res.json()
  expect(body.exit_code).toBe(2)
})
