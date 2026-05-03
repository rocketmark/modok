import { NextResponse } from 'next/server'
import { spawn } from 'child_process'
import { loadConfig, ConfigError } from '@/lib/config'

// @spec DEMO-SEARCH-006, DEMO-SEARCH-007, DEMO-SEARCH-009
export async function GET(request: Request) {
  try {
    loadConfig()
  } catch (err) {
    if (err instanceof ConfigError) {
      return NextResponse.json({ message: err.message }, { status: 503 })
    }
    throw err
  }

  const { searchParams } = new URL(request.url)
  const q = searchParams.get('q')

  if (!q || q.trim() === '') {
    return NextResponse.json({ message: 'q is required' }, { status: 400 })
  }

  const config = loadConfig()

  const result = await new Promise<{ stdout: string; stderr: string; exitCode: number }>(
    (resolve, reject) => {
      const proc = spawn('modok', ['search', '--project', config.project_slug, q.trim(), '--json'], {
        shell: false,
      })
      let stdout = ''
      let stderr = ''
      proc.stdout.on('data', (d: Buffer) => { stdout += d.toString() })
      proc.stderr.on('data', (d: Buffer) => { stderr += d.toString() })
      proc.on('close', (code) => resolve({ stdout, stderr, exitCode: code ?? -1 }))
      proc.on('error', reject)
    }
  )

  if (result.exitCode !== 0) {
    return NextResponse.json({ message: 'Search failed', exit_code: result.exitCode, stderr: result.stderr }, { status: 502 })
  }

  try {
    const data = JSON.parse(result.stdout)
    return NextResponse.json(data)
  } catch {
    return NextResponse.json({ message: 'Search returned unexpected output', raw: result.stdout }, { status: 502 })
  }
}
