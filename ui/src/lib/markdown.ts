import type { Ticket, Note } from '@/types/ticket'

export function renderTicketMarkdown(ticket: Ticket, notes: Note[], source: string): string {
  const lines: string[] = [
    `# Ticket: ${ticket.id}`,
    '',
    `**Source:** ${source}`,
    `**Created:** ${ticket.created_at}`,
    '',
    '## Subject',
    '',
    ticket.subject,
    '',
    '## Content',
    '',
    ticket.content,
  ]

  if (notes.length > 0) {
    lines.push('', '## Notes', '')
    for (const note of notes) {
      lines.push(`**${note.created_at} — ${note.author}**`)
      lines.push(note.body)
      lines.push('')
    }
  }

  return lines.join('\n')
}
