'use client'

// @spec DEMO-LAYOUT-001, DEMO-LAYOUT-002
import { SearchBar } from './SearchBar'

export function TopNav() {
  return (
    <header className="h-14 border-b border-slate-200 bg-white flex items-center px-4 gap-4 flex-shrink-0">
      <span className="text-slate-900 font-semibold tracking-tight text-base select-none">
        MODOK
      </span>
      <div className="flex-1 max-w-xl">
        <SearchBar />
      </div>
    </header>
  )
}
