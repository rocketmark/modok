import type { Note } from '@/types/ticket'

interface Props {
  notes: Note[]
}

export function NotesTimeline({ notes }: Props) {
  if (notes.length === 0) return null

  return (
    <div className="space-y-2">
      {notes.map((note) => (
        <div key={note.id} className="rounded border border-slate-100 p-3 bg-white">
          <p className="text-xs text-slate-400 mb-1">
            {note.author} · {new Date(note.created_at).toLocaleString('en-US', {
              month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
            })}
          </p>
          <p className="text-sm text-slate-700 whitespace-pre-wrap">{note.body}</p>
        </div>
      ))}
    </div>
  )
}
