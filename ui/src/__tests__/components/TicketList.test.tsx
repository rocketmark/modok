import React from 'react'
import { render, screen, fireEvent } from '@testing-library/react'
import type { Ticket } from '@/types/ticket'

// Component not implemented yet — import will fail until Phase 6.
import { TicketList } from '@/components/tickets/TicketList'

const tickets: Ticket[] = [
  { id: 'ACME-1842', subject: 'Checkout fails', content: '', created_at: '2024-01-15T10:00:00Z' },
  { id: 'GLOBEX-991', subject: 'Duplicate notification', content: '', created_at: '2024-01-14T10:00:00Z' },
]

const runs = {
  'ACME-1842': { ticket_id: 'ACME-1842', status: 'complete' as const },
  'GLOBEX-991': { ticket_id: 'GLOBEX-991', status: 'not_run' as const },
}

// @spec DEMO-LIST-001
it('renders all tickets sorted newest-first', () => {
  render(<TicketList tickets={tickets} runs={runs} selectedId="ACME-1842" onSelect={jest.fn()} />)
  const cards = screen.getAllByRole('listitem')
  expect(cards[0]).toHaveTextContent('ACME-1842')
  expect(cards[1]).toHaveTextContent('GLOBEX-991')
})

// @spec DEMO-LIST-002
it('each card shows ticket ID, subject, and date', () => {
  render(<TicketList tickets={tickets} runs={runs} selectedId={null} onSelect={jest.fn()} />)
  expect(screen.getByText('ACME-1842')).toBeInTheDocument()
  expect(screen.getByText(/checkout fails/i)).toBeInTheDocument()
  expect(screen.getByText(/jan 15/i)).toBeInTheDocument()
})

// @spec DEMO-LIST-003
it('each card shows MODOK status indicator', () => {
  render(<TicketList tickets={tickets} runs={runs} selectedId={null} onSelect={jest.fn()} />)
  expect(screen.getByText(/complete/i)).toBeInTheDocument()
  expect(screen.getByText(/not run/i)).toBeInTheDocument()
})

// @spec DEMO-LIST-004
it('highlights the selected ticket card', () => {
  render(<TicketList tickets={tickets} runs={runs} selectedId="ACME-1842" onSelect={jest.fn()} />)
  const selected = screen.getAllByRole('listitem')[0]
  expect(selected).toHaveAttribute('aria-selected', 'true')
})

// @spec DEMO-LIST-005
it('renders a New Ticket button', () => {
  render(<TicketList tickets={tickets} runs={runs} selectedId={null} onSelect={jest.fn()} />)
  expect(screen.getByRole('button', { name: /new ticket/i })).toBeInTheDocument()
})

// @spec DEMO-LIST-006
it('calls onNewTicket when New Ticket button is clicked', () => {
  const onNewTicket = jest.fn()
  render(<TicketList tickets={tickets} runs={runs} selectedId={null} onSelect={jest.fn()} onNewTicket={onNewTicket} />)
  fireEvent.click(screen.getByRole('button', { name: /new ticket/i }))
  expect(onNewTicket).toHaveBeenCalledTimes(1)
})

// @spec DEMO-NAV-004
it('calls onSelect with ticket id when a card is clicked', () => {
  const onSelect = jest.fn()
  render(<TicketList tickets={tickets} runs={runs} selectedId={null} onSelect={onSelect} />)
  fireEvent.click(screen.getByText('GLOBEX-991'))
  expect(onSelect).toHaveBeenCalledWith('GLOBEX-991')
})
