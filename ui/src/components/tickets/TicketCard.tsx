import type { Ticket } from '@/types/ticket'
import type { ModokRun } from '@/types/modok-run'

const STATUS_LABEL: Record<string, string> = {
  not_run: 'Not run',
  running: 'Running',
  complete: 'Complete',
  failed: 'Failed',
}

const STATUS_CLASS: Record<string, string> = {
  not_run: 'text-slate-400',
  running: 'text-blue-500',
  complete: 'text-emerald-600',
  failed: 'text-red-500',
}

function formatDate(iso: string) {
  return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

interface Props {
  ticket: Ticket
  run: ModokRun
  selected: boolean
  onClick: () => void
  onDelete?: () => void
}

export function TicketCard({ ticket, run, selected, onClick, onDelete }: Props) {
  return (
    <li
      role="listitem"
      aria-selected={selected}
      data-testid={`ticket-card-${ticket.id}`}
      onClick={onClick}
      className={`relative px-3 py-2.5 cursor-pointer border-b border-slate-100 hover:bg-slate-50 ${selected ? 'bg-blue-50 border-l-2 border-l-blue-500' : ''}`}
    >
      <div className="flex items-center justify-between gap-1 mb-0.5">
        <span className="text-xs font-mono font-medium text-slate-500">{ticket.id}</span>
        <span className={`text-xs ${STATUS_CLASS[run.status] ?? 'text-slate-400'}`}>
          {STATUS_LABEL[run.status] ?? 'Not run'}
        </span>
      </div>
      <p className="text-sm text-slate-800 truncate leading-snug">{ticket.subject}</p>
      <p className="text-xs text-slate-400 mt-0.5">{formatDate(ticket.created_at)}</p>
      {ticket.user_created && onDelete && (
        <button
          data-testid={`delete-ticket-${ticket.id}`}
          onClick={(e) => { e.stopPropagation(); onDelete() }}
          className="absolute bottom-1.5 right-1.5 w-4 h-4 flex items-center justify-center rounded-full bg-red-500 hover:bg-red-600 text-white text-[9px] leading-none"
          aria-label={`Delete ticket ${ticket.id}`}
        >
          ✕
        </button>
      )}
    </li>
  )
}
