/**
 * @jest-environment node
 */
jest.mock('@/lib/data')
jest.mock('@/lib/config', () => ({
  ...jest.requireActual('@/lib/config'),
  loadConfig: jest.fn(),
}))

import { readTickets, writeTicket, readNotes, writeNote, getRun } from '@/lib/data'
import { loadConfig, ConfigError } from '@/lib/config'

// Route handlers not implemented yet — imports will fail until Phase 6.
import { GET as getTickets, POST as postTicket } from '@/app/api/tickets/route'
import { GET as getTicket } from '@/app/api/tickets/[id]/route'
import { POST as postNote } from '@/app/api/tickets/[id]/notes/route'

const mockReadTickets = readTickets as jest.MockedFunction<typeof readTickets>
const mockWriteTicket = writeTicket as jest.MockedFunction<typeof writeTicket>
const mockReadNotes = readNotes as jest.MockedFunction<typeof readNotes>
const mockWriteNote = writeNote as jest.MockedFunction<typeof writeNote>
const mockGetRun = getRun as jest.MockedFunction<typeof getRun>
const mockLoadConfig = loadConfig as jest.MockedFunction<typeof loadConfig>

const baseTicket = { id: 'ACME-1842', subject: 'S', content: 'C', created_at: '2024-01-15T10:00:00Z' }
const baseConfig = { project_slug: 'stagehand', modok_source: 'demo-crm', mockMode: false }

beforeEach(() => {
  jest.resetAllMocks()
  mockLoadConfig.mockReturnValue(baseConfig)
})

// @spec DEMO-TICK-API-001
it('GET /api/tickets returns all tickets newest-first merged with run status', async () => {
  mockReadTickets.mockReturnValue([baseTicket])
  mockGetRun.mockReturnValue({ ticket_id: 'ACME-1842', status: 'complete' })
  const res = await getTickets(new Request('http://localhost/api/tickets'))
  expect(res.status).toBe(200)
  const data = await res.json()
  expect(data[0].id).toBe('ACME-1842')
  expect(data[0].modok_status).toBe('complete')
})

// @spec DEMO-TICK-API-002
it('POST /api/tickets creates a ticket and returns it', async () => {
  mockReadTickets.mockReturnValue([])
  mockWriteTicket.mockImplementation(() => {})
  const req = new Request('http://localhost/api/tickets', {
    method: 'POST',
    body: JSON.stringify({ subject: 'New issue', content: 'Details' }),
  })
  const res = await postTicket(req)
  expect(res.status).toBe(201)
  const data = await res.json()
  expect(data.subject).toBe('New issue')
  expect(data.id).toBeDefined()
  expect(data.created_at).toBeDefined()
})

// @spec DEMO-TICK-API-003
it('GET /api/tickets/[id] returns ticket, notes, and run', async () => {
  mockReadTickets.mockReturnValue([baseTicket])
  mockReadNotes.mockReturnValue([])
  mockGetRun.mockReturnValue({ ticket_id: 'ACME-1842', status: 'not_run' })
  const res = await getTicket(new Request('http://localhost/api/tickets/ACME-1842'), { params: { id: 'ACME-1842' } })
  expect(res.status).toBe(200)
  const data = await res.json()
  expect(data.ticket.id).toBe('ACME-1842')
  expect(data.notes).toEqual([])
  expect(data.run.status).toBe('not_run')
})

// @spec DEMO-TICK-API-004
it('GET /api/tickets/[id] returns 404 when ticket does not exist', async () => {
  mockReadTickets.mockReturnValue([])
  const res = await getTicket(new Request('http://localhost/api/tickets/MISSING'), { params: { id: 'MISSING' } })
  expect(res.status).toBe(404)
})

// @spec DEMO-NOTE-005
it('POST /api/tickets/[id]/notes appends note and returns it', async () => {
  mockReadTickets.mockReturnValue([baseTicket])
  mockWriteNote.mockImplementation(() => {})
  const req = new Request('http://localhost/api/tickets/ACME-1842/notes', {
    method: 'POST',
    body: JSON.stringify({ author: 'Alice', body: 'Confirmed on staging' }),
  })
  const res = await postNote(req, { params: { id: 'ACME-1842' } })
  expect(res.status).toBe(201)
  const note = await res.json()
  expect(note.author).toBe('Alice')
  expect(note.body).toBe('Confirmed on staging')
  expect(note.ticket_id).toBe('ACME-1842')
})

// @spec DEMO-NOTE-006
it('POST /api/tickets/[id]/notes returns 400 when body is empty', async () => {
  const req = new Request('http://localhost/api/tickets/ACME-1842/notes', {
    method: 'POST',
    body: JSON.stringify({ author: 'Alice', body: '' }),
  })
  const res = await postNote(req, { params: { id: 'ACME-1842' } })
  expect(res.status).toBe(400)
})

// @spec DEMO-BRIDGE-001
it('all routes return 503 when config is missing', async () => {
  mockLoadConfig.mockImplementation(() => { throw new ConfigError('Demo not configured — create ui/config.json') })
  const res = await getTickets(new Request('http://localhost/api/tickets'))
  expect(res.status).toBe(503)
  const body = await res.json()
  expect(body.message).toContain('config.json')
})
