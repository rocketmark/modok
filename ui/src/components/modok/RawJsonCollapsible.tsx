'use client'

import { useState } from 'react'

interface Props {
  data: unknown
  defaultOpen?: boolean
}

// @spec DEMO-MODOK-006
export function RawJsonCollapsible({ data, defaultOpen = false }: Props) {
  const [open, setOpen] = useState(defaultOpen)

  return (
    <div className="mt-2">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1 text-xs text-slate-500 hover:text-slate-700 transition-colors"
      >
        <span className={`inline-block transition-transform ${open ? 'rotate-90' : ''}`}>▶</span>
        Raw JSON
      </button>
      {open && (
        <pre className="mt-2 p-3 bg-slate-50 border border-slate-200 rounded text-xs text-slate-600 overflow-x-auto">
          <code>{JSON.stringify(data, null, 2)}</code>
        </pre>
      )}
    </div>
  )
}
