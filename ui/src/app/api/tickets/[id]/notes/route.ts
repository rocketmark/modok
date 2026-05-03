import { NextResponse } from 'next/server'
import { randomUUID } from 'crypto'
import { loadConfig, ConfigError } from '@/lib/config'
import { readTickets, writeNote } from '@/lib/data'

// @spec DEMO-NOTE-005, DEMO-NOTE-006
export async function POST(request: Request, { params }: { params: { id: string } }) {
  try {
    loadConfig()
  } catch (err) {
    if (err instanceof ConfigError) {
      return NextResponse.json({ message: err.message }, { status: 503 })
    }
    throw err
  }

  const body = await request.json()
  const { author = 'Engineer', body: noteBody } = body as { author?: string; body?: string }

  if (!noteBody || noteBody.trim() === '') {
    return NextResponse.json({ message: 'body is required' }, { status: 400 })
  }

  const tickets = readTickets()
  if (!tickets.find((t) => t.id === params.id)) {
    return NextResponse.json({ message: 'Ticket not found' }, { status: 404 })
  }

  const note = {
    id: randomUUID(),
    ticket_id: params.id,
    author: author.trim() || 'Engineer',
    body: noteBody.trim(),
    created_at: new Date().toISOString(),
  }

  writeNote(note)
  return NextResponse.json(note, { status: 201 })
}
