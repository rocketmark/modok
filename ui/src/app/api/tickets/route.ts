import { NextResponse } from 'next/server'
import { randomUUID } from 'crypto'
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

  const ticket = {
    id: `TICKET-${Date.now()}`,
    subject: subject.trim(),
    content: content.trim(),
    created_at: new Date().toISOString(),
    _uuid: randomUUID(),
  }

  writeTicket(ticket)
  return NextResponse.json(ticket, { status: 201 })
}
