import { redirect } from 'next/navigation'
import { readTickets } from '@/lib/data'

export default function TicketsPage() {
  const tickets = readTickets()
  if (tickets.length > 0) {
    redirect(`/tickets/${tickets[0].id}`)
  }
  return (
    <div className="flex items-center justify-center h-full">
      <p className="text-sm text-slate-400">No tickets yet. Use the New Ticket button to get started.</p>
    </div>
  )
}
