import React from 'react'
import { render, screen } from '@testing-library/react'

// Component not implemented yet — import will fail until Phase 6.
import { TopNav } from '@/components/nav/TopNav'

// @spec DEMO-LAYOUT-001
it('renders MODOK wordmark', () => {
  render(<TopNav />)
  expect(screen.getByText(/modok/i)).toBeInTheDocument()
})

// @spec DEMO-LAYOUT-001
it('renders a search input', () => {
  render(<TopNav />)
  expect(screen.getByRole('searchbox')).toBeInTheDocument()
})

// @spec DEMO-LAYOUT-002
it('contains no navigation links, tabs, or workflow stage indicators', () => {
  render(<TopNav />)
  expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
  expect(screen.queryByRole('tab')).not.toBeInTheDocument()
  // Stage tracker from the original mockup should not be present
  expect(screen.queryByText(/ingest/i)).not.toBeInTheDocument()
  expect(screen.queryByText(/analyze/i)).not.toBeInTheDocument()
  expect(screen.queryByText(/resolve/i)).not.toBeInTheDocument()
})
