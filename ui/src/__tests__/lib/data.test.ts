import * as fc from 'fast-check'

jest.mock('fs')
import fs from 'fs'
const mockFs = fs as jest.Mocked<typeof fs>

// Module is not implemented yet — these tests will fail until Phase 6.
import { readTickets, writeTicket, readNotes, writeNote, readRuns, writeRun, getRun } from '@/lib/data'
import type { Ticket, Note } from '@/types/ticket'
import type { ModokRun } from '@/types/modok-run'

const makeTicket = (overrides: Partial<Ticket> = {}): Ticket => ({
  id: 'TEST-001',
  subject: 'Test subject',
  content: 'Test content',
  created_at: new Date().toISOString(),
  ...overrides,
})

describe('readTickets / writeTicket', () => {
  beforeEach(() => jest.resetAllMocks())

  // @spec DEMO-DATA-001
  it('returns tickets sorted newest-first', () => {
    const tickets: Ticket[] = [
      makeTicket({ id: 'A', created_at: '2024-01-10T00:00:00Z' }),
      makeTicket({ id: 'B', created_at: '2024-01-15T00:00:00Z' }),
      makeTicket({ id: 'C', created_at: '2024-01-12T00:00:00Z' }),
    ]
    mockFs.readFileSync.mockReturnValue(JSON.stringify(tickets))
    const result = readTickets()
    expect(result.map(t => t.id)).toEqual(['B', 'C', 'A'])
  })

  // @spec DEMO-DATA-001 [P]
  it('preserves newest-first order for any sequence of creates', () => {
    fc.assert(
      fc.property(
        fc.array(
          fc.record({
            id: fc.string({ minLength: 1 }),
            subject: fc.string(),
            content: fc.string(),
            created_at: fc.date().map(d => d.toISOString()),
          }),
          { minLength: 1, maxLength: 20 }
        ),
        (tickets) => {
          mockFs.readFileSync.mockReturnValue(JSON.stringify(tickets))
          const result = readTickets()
          for (let i = 0; i < result.length - 1; i++) {
            expect(Date.parse(result[i].created_at) >= Date.parse(result[i + 1].created_at)).toBe(true)
          }
        }
      )
    )
  })

  // @spec DEMO-DATA-002 (prepend)
  it('prepends new ticket to the array so it appears first', () => {
    const existing: Ticket[] = [makeTicket({ id: 'OLD', created_at: '2024-01-01T00:00:00Z' })]
    mockFs.readFileSync.mockReturnValue(JSON.stringify(existing))
    let written: string | undefined
    mockFs.writeFileSync.mockImplementation((_, data) => { written = data as string })

    const newTicket = makeTicket({ id: 'NEW', created_at: '2024-01-15T00:00:00Z' })
    writeTicket(newTicket)

    const saved = JSON.parse(written!)
    expect(saved[0].id).toBe('NEW')
  })
})

describe('readNotes', () => {
  beforeEach(() => jest.resetAllMocks())

  // @spec DEMO-DATA-002
  it('returns notes for a ticket filtered by ticket_id, oldest-first', () => {
    const notes: Note[] = [
      { id: '1', ticket_id: 'A', author: 'alice', body: 'first', created_at: '2024-01-10T00:00:00Z' },
      { id: '2', ticket_id: 'B', author: 'bob',   body: 'other', created_at: '2024-01-11T00:00:00Z' },
      { id: '3', ticket_id: 'A', author: 'carol', body: 'second', created_at: '2024-01-12T00:00:00Z' },
    ]
    mockFs.readFileSync.mockReturnValue(JSON.stringify(notes))
    const result = readNotes('A')
    expect(result).toHaveLength(2)
    expect(result[0].id).toBe('1')
    expect(result[1].id).toBe('3')
  })
})

describe('getRun / writeRun', () => {
  beforeEach(() => jest.resetAllMocks())

  // @spec DEMO-DATA-003 [P]
  it('returns { status: "not_run" } for any ticket_id not in the map', () => {
    fc.assert(
      fc.property(
        fc.string({ minLength: 1 }),
        (ticketId) => {
          mockFs.readFileSync.mockReturnValue(JSON.stringify({}))
          const run = getRun(ticketId)
          expect(run.status).toBe('not_run')
        }
      )
    )
  })

  // @spec DEMO-DATA-003
  it('returns existing run when ticket_id is present', () => {
    const run: ModokRun = { ticket_id: 'X', status: 'complete' }
    mockFs.readFileSync.mockReturnValue(JSON.stringify({ X: run }))
    expect(getRun('X').status).toBe('complete')
  })
})
