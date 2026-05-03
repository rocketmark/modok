import { NextResponse } from 'next/server'
import { loadConfig, ConfigError } from '@/lib/config'
import { deleteRun } from '@/lib/data'
import { runModok } from '@/lib/modok-bridge'

// @spec DEMO-BRIDGE-001 through DEMO-BRIDGE-015
export async function POST(_request: Request, { params }: { params: { id: string } }) {
  try {
    const run = await runModok(params.id)
    return NextResponse.json(run)
  } catch (err) {
    if (err instanceof ConfigError) {
      return NextResponse.json({ message: err.message }, { status: 503 })
    }
    throw err
  }
}

export async function DELETE(_request: Request, { params }: { params: { id: string } }) {
  try {
    loadConfig()
  } catch (err) {
    if (err instanceof ConfigError) {
      return NextResponse.json({ message: err.message }, { status: 503 })
    }
    throw err
  }
  deleteRun(params.id)
  return new NextResponse(null, { status: 204 })
}
