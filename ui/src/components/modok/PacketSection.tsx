import type { ReactNode } from 'react'

interface Props {
  title: string
  children: ReactNode
}

export function PacketSection({ title, children }: Props) {
  return (
    <div className="mb-5">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500 mb-2">{title}</h3>
      {children}
    </div>
  )
}
