'use client'

import { useState, useRef, useEffect, FormEvent } from 'react'

interface SearchNode {
  id: number
  node_type: string
  name: string
  summary?: string
}

// @spec DEMO-SEARCH-001, DEMO-SEARCH-002, DEMO-SEARCH-003, DEMO-SEARCH-004,
//       DEMO-SEARCH-005, DEMO-SEARCH-008
export function SearchBar() {
  const [query, setQuery] = useState('')
  const [nodes, setNodes] = useState<SearchNode[]>([])
  const [error, setError] = useState<string | null>(null)
  const [open, setOpen] = useState(false)
  const [selected, setSelected] = useState<SearchNode | null>(null)
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') close()
    }
    function onClickOutside(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) close()
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('mousedown', onClickOutside)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('mousedown', onClickOutside)
    }
  }, [])

  function close() {
    setOpen(false)
    setSelected(null)
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    if (!query.trim()) {
      setNodes([])
      setOpen(false)
      return
    }
    setError(null)
    try {
      const res = await fetch(`/api/search?q=${encodeURIComponent(query.trim())}`)
      if (!res.ok) {
        setError('Search unavailable — is Quine running?')
        setOpen(true)
        return
      }
      const data = await res.json()
      setNodes(data.nodes ?? [])
      setOpen(true)
    } catch {
      setError('Search unavailable — is Quine running?')
      setOpen(true)
    }
  }

  return (
    <div ref={containerRef} className="relative">
      <form role="search" onSubmit={onSubmit}>
        <input
          role="searchbox"
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search graph…"
          className="w-full h-8 px-3 text-sm border border-slate-200 rounded-md bg-slate-50 text-slate-900 placeholder:text-slate-400 focus:outline-none focus:ring-1 focus:ring-blue-500"
        />
      </form>

      {open && (
        <div
          data-testid="search-overlay"
          className="absolute top-10 left-0 w-96 bg-white border border-slate-200 rounded-lg shadow-lg z-50 max-h-80 overflow-y-auto"
        >
          {error ? (
            <p className="p-3 text-sm text-red-600">{error}</p>
          ) : nodes.length === 0 ? (
            <p className="p-3 text-sm text-slate-500">No results.</p>
          ) : (
            <ul>
              {nodes.map((node) => (
                <li key={node.id}>
                  <button
                    className="w-full text-left px-3 py-2 hover:bg-slate-50 flex items-start gap-2"
                    onClick={() => { setSelected(node); setOpen(false) }}
                  >
                    <span className="text-xs font-medium px-1.5 py-0.5 rounded bg-blue-50 text-blue-700 flex-shrink-0">
                      {node.node_type}
                    </span>
                    <span className="flex-1 min-w-0">
                      <span className="text-sm font-medium text-slate-900 block truncate">{node.name}</span>
                      {node.summary && (
                        <span className="text-xs text-slate-500 block truncate">{node.summary}</span>
                      )}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {selected && (
        <div className="fixed inset-0 z-40 flex justify-end" onClick={() => setSelected(null)}>
          <div
            className="w-96 bg-white border-l border-slate-200 shadow-xl p-6 overflow-y-auto"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between mb-4">
              <span className="text-xs font-medium px-2 py-1 rounded bg-blue-50 text-blue-700">
                {selected.node_type}
              </span>
              <button onClick={() => setSelected(null)} className="text-slate-400 hover:text-slate-600 text-lg leading-none">&times;</button>
            </div>
            <h3 className="text-base font-semibold text-slate-900 mb-2">{selected.name}</h3>
            {selected.summary && <p className="text-sm text-slate-600">{selected.summary}</p>}
            <p className="text-xs text-slate-400 mt-4">Node ID: {selected.id}</p>
          </div>
        </div>
      )}
    </div>
  )
}
