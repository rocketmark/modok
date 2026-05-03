'use client'

import { useState, useEffect, useCallback } from 'react'
import { useRouter, useParams } from 'next/navigation'
import type { Ticket, Note } from '@/types/ticket'
import type { ModokRun, ModokRunStatus } from '@/types/modok-run'
import { TicketList } from '@/components/tickets/TicketList'
import { TicketDetail } from '@/components/tickets/TicketDetail'
import { ModokPanel } from '@/components/modok/ModokPanel'
import { NewTicketModal } from '@/components/tickets/NewTicketModal'

type TicketWithStatus = Ticket & { modok_status: ModokRunStatus }

// @spec DEMO-LAYOUT-003, DEMO-LIST-001, DEMO-LIST-002,
//       DEMO-LIST-003, DEMO-LIST-004, DEMO-LIST-005, DEMO-LIST-006, DEMO-NAV-004
export default function TicketPage() {
  const router = useRouter()
  const params = useParams<{ id: string }>()
  const ticketId = params.id

  const [tickets, setTickets] = useState<Ticket[]>([])
  const [runs, setRuns] = useState<Record<string, ModokRun>>({})
  const [notes, setNotes] = useState<Note[]>([])
  const [currentRun, setCurrentRun] = useState<ModokRun | null>(null)
  const [isRunning, setIsRunning] = useState(false)
  const [showNewTicket, setShowNewTicket] = useState(false)

  const loadTicketList = useCallback(() => {
    fetch('/api/tickets')
      .then((r) => r.json())
      .then((data: TicketWithStatus[]) => {
        setTickets(data)
        const runsMap: Record<string, ModokRun> = {}
        for (const t of data) {
          runsMap[t.id] = { ticket_id: t.id, status: t.modok_status }
        }
        setRuns(runsMap)
      })
      .catch(() => {})
  }, [])

  useEffect(() => {
    loadTicketList()
  }, [loadTicketList])

  useEffect(() => {
    if (!ticketId) return
    fetch(`/api/tickets/${ticketId}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (!data) return
        setNotes(data.notes ?? [])
        setCurrentRun(data.run ?? null)
      })
      .catch(() => {})
  }, [ticketId])

  const handleAddNote = useCallback(
    async (body: string) => {
      await fetch(`/api/tickets/${ticketId}/notes`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ author: 'Demo User', body }),
      })
      const res = await fetch(`/api/tickets/${ticketId}`)
      if (res.ok) {
        const data = await res.json()
        setNotes(data.notes ?? [])
      }
    },
    [ticketId],
  )

  const handleRun = useCallback(async () => {
    setIsRunning(true)
    try {
      const res = await fetch(`/api/tickets/${ticketId}/modok`, { method: 'POST' })
      const run: ModokRun = await res.json()
      setCurrentRun(run)
      setRuns((prev) => ({ ...prev, [ticketId]: run }))
    } finally {
      setIsRunning(false)
    }
  }, [ticketId])

  const handleClear = useCallback(async () => {
    await fetch(`/api/tickets/${ticketId}/modok`, { method: 'DELETE' })
    const cleared: ModokRun = { ticket_id: ticketId, status: 'not_run' }
    setCurrentRun(cleared)
    setRuns((prev) => ({ ...prev, [ticketId]: cleared }))
  }, [ticketId])

  const handleCreateTicket = useCallback(
    async (subject: string, content: string) => {
      const res = await fetch('/api/tickets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ subject, content }),
      })
      if (res.ok) {
        const newTicket: Ticket = await res.json()
        setShowNewTicket(false)
        loadTicketList()
        router.push(`/tickets/${newTicket.id}`)
      }
    },
    [router, loadTicketList],
  )

  const run: ModokRun = currentRun ?? { ticket_id: ticketId, status: 'not_run' }
  const ticket = tickets.find((t) => t.id === ticketId)

  return (
    <div className="flex h-full">
      <div className="w-52 border-r border-slate-200 bg-white flex-shrink-0">
        <TicketList
          tickets={tickets}
          runs={runs}
          selectedId={ticketId}
          onSelect={(id) => router.push(`/tickets/${id}`)}
          onNewTicket={() => setShowNewTicket(true)}
        />
      </div>

      <div className="flex-1 border-r border-slate-200 bg-white min-w-0">
        {ticket ? (
          <TicketDetail
            ticket={ticket}
            notes={notes}
            isRunning={isRunning}
            onAddNote={handleAddNote}
          />
        ) : (
          <div className="flex items-center justify-center h-full">
            <p className="text-sm text-slate-400">Loading…</p>
          </div>
        )}
      </div>

      <div className="flex-1 bg-white min-w-0 border-l border-slate-200">
        <ModokPanel
          ticketId={ticketId}
          run={run}
          onRun={handleRun}
          onClear={handleClear}
          isRunning={isRunning}
        />
      </div>

      {showNewTicket && (
        <NewTicketModal
          onClose={() => setShowNewTicket(false)}
          onCreate={handleCreateTicket}
        />
      )}
    </div>
  )
}
