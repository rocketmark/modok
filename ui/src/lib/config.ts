import fs from 'fs'
import path from 'path'

export class ConfigError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'ConfigError'
  }
}

export interface DemoConfig {
  project_slug: string
  modok_source: string
  mockMode: boolean
}

// @spec DEMO-CFG-001, DEMO-CFG-003
export function loadConfig(): DemoConfig {
  const configPath = path.join(process.cwd(), 'config.json')
  let raw: string
  try {
    raw = fs.readFileSync(configPath, 'utf-8')
  } catch {
    throw new ConfigError(
      'Demo not configured — create ui/config.json with project_slug and modok_source.'
    )
  }

  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    throw new ConfigError('config.json is not valid JSON.')
  }

  const cfg = parsed as Record<string, unknown>

  if (!cfg.project_slug || typeof cfg.project_slug !== 'string' || cfg.project_slug.trim() === '') {
    throw new ConfigError('config.json missing required field: project_slug.')
  }
  if (!cfg.modok_source || typeof cfg.modok_source !== 'string' || cfg.modok_source.trim() === '') {
    throw new ConfigError('config.json missing required field: modok_source.')
  }

  return {
    project_slug: cfg.project_slug as string,
    modok_source: cfg.modok_source as string,
    mockMode: process.env.MODOK_MOCK === '1',
  }
}
