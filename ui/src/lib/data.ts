import fs from 'fs'
import path from 'path'
import type { Ticket, Note } from '@/types/ticket'
import type { ModokRun, ModokRunsMap } from '@/types/modok-run'
import { resolveStaleRuns } from '@/lib/stale'

const DATA = path.join(process.cwd(), 'data')
const ticketsPath = () => path.join(DATA, 'tickets.json')
const notesPath = () => path.join(DATA, 'notes.json')
const runsPath = () => path.join(DATA, 'modok-runs.json')

// @spec DEMO-DATA-001
export function readTickets(): Ticket[] {
  const raw = fs.readFileSync(ticketsPath(), 'utf-8')
  const tickets: Ticket[] = JSON.parse(raw)
  return tickets.sort((a, b) => {
    const ta = Date.parse(a.created_at)
    const tb = Date.parse(b.created_at)
    if (isNaN(ta) || isNaN(tb)) return b.created_at.localeCompare(a.created_at)
    return tb - ta
  })
}

// @spec DEMO-DATA-002
export function writeTicket(ticket: Ticket): void {
  const tickets = readTickets()
  tickets.unshift(ticket)
  fs.writeFileSync(ticketsPath(), JSON.stringify(tickets, null, 2))
}

// @spec DEMO-DATA-002
export function readNotes(ticketId: string): Note[] {
  const raw = fs.readFileSync(notesPath(), 'utf-8')
  const notes: Note[] = JSON.parse(raw)
  return notes
    .filter((n) => n.ticket_id === ticketId)
    .sort((a, b) => a.created_at.localeCompare(b.created_at))
}

export function writeNote(note: Note): void {
  const raw = fs.readFileSync(notesPath(), 'utf-8')
  const notes: Note[] = JSON.parse(raw)
  notes.push(note)
  fs.writeFileSync(notesPath(), JSON.stringify(notes, null, 2))
}

// @spec DEMO-DATA-003, DEMO-STALE-001, DEMO-STALE-002
export function readRuns(): ModokRunsMap {
  const raw = fs.readFileSync(runsPath(), 'utf-8')
  const runs = JSON.parse(raw) as ModokRunsMap
  const resolved = resolveStaleRuns(runs)
  const hasStale = Object.keys(resolved).some((k) => resolved[k].status !== runs[k]?.status)
  if (hasStale) {
    fs.writeFileSync(runsPath(), JSON.stringify(resolved, null, 2))
  }
  return resolved
}

export function writeRun(run: ModokRun): void {
  const runs = readRuns()
  runs[run.ticket_id] = run
  fs.writeFileSync(runsPath(), JSON.stringify(runs, null, 2))
}

export function deleteRun(ticketId: string): void {
  const runs = readRuns()
  delete runs[ticketId]
  fs.writeFileSync(runsPath(), JSON.stringify(runs, null, 2))
}

// @spec DEMO-DATA-003
export function getRun(ticketId: string): ModokRun {
  const runs = readRuns()
  return Object.prototype.hasOwnProperty.call(runs, ticketId)
    ? runs[ticketId]
    : { ticket_id: ticketId, status: 'not_run' }
}
