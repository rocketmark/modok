'use client'

import type { Ticket } from '@/types/ticket'
import type { ModokRun } from '@/types/modok-run'
import { TicketCard } from './TicketCard'

interface Props {
  tickets: Ticket[]
  runs: Record<string, ModokRun>
  selectedId: string | null
  onSelect: (id: string) => void
  onNewTicket?: () => void
}

// @spec DEMO-LIST-001, DEMO-LIST-002, DEMO-LIST-003, DEMO-LIST-004,
//       DEMO-LIST-005, DEMO-LIST-006, DEMO-NAV-004
export function TicketList({ tickets, runs, selectedId, onSelect, onNewTicket }: Props) {
  return (
    <div
      data-testid="ticket-list-panel"
      className="flex flex-col h-full"
    >
      <ul className="flex-1 overflow-y-auto">
        {tickets.map((t) => (
          <TicketCard
            key={t.id}
            ticket={t}
            run={runs[t.id] ?? { ticket_id: t.id, status: 'not_run' }}
            selected={t.id === selectedId}
            onClick={() => onSelect(t.id)}
          />
        ))}
      </ul>
      <div className="p-3 border-t border-slate-100">
        <button
          onClick={onNewTicket}
          className="w-full text-sm text-slate-500 hover:text-blue-600 py-1 rounded border border-dashed border-slate-200 hover:border-blue-300 transition-colors"
        >
          + New Ticket
        </button>
      </div>
    </div>
  )
}
