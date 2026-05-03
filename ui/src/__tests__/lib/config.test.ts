import path from 'path'

jest.mock('fs')
import fs from 'fs'
const mockFs = fs as jest.Mocked<typeof fs>

// Module is not implemented yet — these tests will fail until Phase 6.
import { loadConfig, ConfigError } from '@/lib/config'

describe('loadConfig', () => {
  beforeEach(() => jest.resetAllMocks())

  // @spec DEMO-CFG-001
  it('throws ConfigError when config.json is absent', () => {
    mockFs.readFileSync.mockImplementation(() => { throw new Error('ENOENT') })
    expect(() => loadConfig()).toThrow(ConfigError)
  })

  // @spec DEMO-CFG-001
  it('throws ConfigError when project_slug is missing', () => {
    mockFs.readFileSync.mockReturnValue(JSON.stringify({ modok_source: 'demo-crm' }))
    expect(() => loadConfig()).toThrow(ConfigError)
  })

  // @spec DEMO-CFG-001
  it('throws ConfigError when modok_source is missing', () => {
    mockFs.readFileSync.mockReturnValue(JSON.stringify({ project_slug: 'stagehand' }))
    expect(() => loadConfig()).toThrow(ConfigError)
  })

  // @spec DEMO-CFG-001
  it('throws ConfigError when project_slug is an empty string', () => {
    mockFs.readFileSync.mockReturnValue(JSON.stringify({ project_slug: '', modok_source: 'demo-crm' }))
    expect(() => loadConfig()).toThrow(ConfigError)
  })

  // @spec DEMO-CFG-001
  it('throws ConfigError when config.json is not valid JSON', () => {
    mockFs.readFileSync.mockReturnValue('not json {{{')
    expect(() => loadConfig()).toThrow(ConfigError)
  })

  // @spec DEMO-CFG-003
  it('returns mock mode true when MODOK_MOCK=1 regardless of config contents', () => {
    process.env.MODOK_MOCK = '1'
    mockFs.readFileSync.mockReturnValue(JSON.stringify({ project_slug: 'stagehand', modok_source: 'demo-crm' }))
    const config = loadConfig()
    expect(config.mockMode).toBe(true)
    delete process.env.MODOK_MOCK
  })

  // @spec DEMO-CFG-003
  it('returns mock mode false when MODOK_MOCK is not set', () => {
    delete process.env.MODOK_MOCK
    mockFs.readFileSync.mockReturnValue(JSON.stringify({ project_slug: 'stagehand', modok_source: 'demo-crm' }))
    const config = loadConfig()
    expect(config.mockMode).toBe(false)
  })
})
