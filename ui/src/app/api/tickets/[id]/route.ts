import { NextResponse } from 'next/server'
import { loadConfig, ConfigError } from '@/lib/config'
import { readTickets, readNotes, getRun } from '@/lib/data'

// @spec DEMO-TICK-API-003, DEMO-TICK-API-004
export async function GET(_request: Request, { params }: { params: { id: string } }) {
  try {
    loadConfig()
  } catch (err) {
    if (err instanceof ConfigError) {
      return NextResponse.json({ message: err.message }, { status: 503 })
    }
    throw err
  }

  const tickets = readTickets()
  const ticket = tickets.find((t) => t.id === params.id)
  if (!ticket) return NextResponse.json({ message: 'Ticket not found' }, { status: 404 })

  const notes = readNotes(params.id)
  const run = getRun(params.id)
  return NextResponse.json({ ticket, notes, run })
}
