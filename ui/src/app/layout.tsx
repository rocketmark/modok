import type { Metadata } from 'next'
import './globals.css'
import { TopNav } from '@/components/nav/TopNav'

export const metadata: Metadata = {
  title: 'MODOK',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="h-screen flex flex-col bg-white">
        <TopNav />
        <main className="flex-1 overflow-hidden">
          {children}
        </main>
      </body>
    </html>
  )
}
