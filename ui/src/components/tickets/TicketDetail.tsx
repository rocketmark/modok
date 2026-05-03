'use client'

import type { Ticket, Note } from '@/types/ticket'
import { NotesTimeline } from './NotesTimeline'
import { NoteInput } from './NoteInput'

interface Props {
  ticket: Ticket
  notes: Note[]
  isRunning: boolean
  onAddNote: (body: string) => void
}

export function TicketDetail({ ticket, notes, isRunning, onAddNote }: Props) {
  return (
    <div className="flex flex-col h-full overflow-hidden">
      <div className="flex-1 overflow-y-auto p-5">
        <div className="flex items-center justify-between gap-2 mb-1">
          <span className="text-xs font-mono font-medium text-slate-400">{ticket.id}</span>
          <span className="text-xs text-slate-400">
            {new Date(ticket.created_at).toLocaleString('en-US', {
              month: 'short',
              day: 'numeric',
              year: 'numeric',
              hour: 'numeric',
              minute: '2-digit',
            })}
          </span>
        </div>
        <h1 className="text-lg font-semibold text-slate-900 mb-4">{ticket.subject}</h1>
        <p className="text-sm text-slate-700 leading-relaxed whitespace-pre-wrap mb-6">
          {ticket.content}
        </p>

        {notes.length > 0 && (
          <div className="mb-4">
            <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-500 mb-2">
              Notes
            </h2>
            <NotesTimeline notes={notes} />
          </div>
        )}
      </div>
      <div className="p-4 border-t border-slate-100 flex-shrink-0">
        <NoteInput onAdd={onAddNote} isDisabled={isRunning} />
      </div>
    </div>
  )
}
