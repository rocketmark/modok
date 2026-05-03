'use client'

import { useState } from 'react'

interface Props {
  onAdd: (body: string) => void
  isDisabled: boolean
}

// @spec DEMO-NOTE-001, DEMO-NOTE-002, DEMO-NOTE-003, DEMO-NOTE-004
export function NoteInput({ onAdd, isDisabled }: Props) {
  const [body, setBody] = useState('')

  function handleAdd() {
    if (!body.trim()) return
    onAdd(body.trim())
    setBody('')
  }

  return (
    <div className="flex gap-2">
      <input
        type="text"
        value={body}
        onChange={(e) => setBody(e.target.value)}
        placeholder="Add a note…"
        disabled={isDisabled}
        className="flex-1 h-8 px-3 text-sm border border-slate-200 rounded bg-slate-50 text-slate-900 placeholder:text-slate-400 focus:outline-none focus:ring-1 focus:ring-blue-500 disabled:opacity-50"
      />
      <button
        onClick={handleAdd}
        disabled={isDisabled || !body.trim()}
        className="px-3 h-8 text-sm bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
      >
        Add
      </button>
    </div>
  )
}
