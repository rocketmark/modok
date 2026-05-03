import { NextResponse } from 'next/server'
import { loadConfig, ConfigError } from '@/lib/config'
import { readTickets, writeTicket, getRun } from '@/lib/data'

function configGuard() {
  try {
    loadConfig()
    return null
  } catch (err) {
    if (err instanceof ConfigError) {
      return NextResponse.json({ message: err.message }, { status: 503 })
    }
    throw err
  }
}

// @spec DEMO-TICK-API-001
export async function GET() {
  const guard = configGuard()
  if (guard) return guard

  const tickets = readTickets()
  const withStatus = tickets.map((t) => ({ ...t, modok_status: getRun(t.id).status }))
  return NextResponse.json(withStatus)
}

// @spec DEMO-TICK-API-002
export async function POST(request: Request) {
  const guard = configGuard()
  if (guard) return guard

  const body = await request.json()
  const { subject, content = '' } = body as { subject?: string; content?: string }

  if (!subject || subject.trim() === '') {
    return NextResponse.json({ message: 'subject is required' }, { status: 400 })
  }

  const tickets = readTickets()
  const maxN = tickets.reduce((max, t) => {
    const m = /^STAGEHAND-(\d+)$/i.exec(t.id)
    return m ? Math.max(max, parseInt(m[1], 10)) : max
  }, 0)

  const ticket = {
    id: `STAGEHAND-${maxN + 1}`,
    subject: subject.trim(),
    content: content.trim(),
    created_at: new Date().toISOString(),
  }

  writeTicket(ticket)
  return NextResponse.json(ticket, { status: 201 })
}
