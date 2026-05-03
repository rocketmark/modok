import React from 'react'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import type { ModokRun } from '@/types/modok-run'
import type { DebugPacket } from '@/types/debug-packet'

// Component not implemented yet — import will fail until Phase 6.
import { ModokPanel } from '@/components/modok/ModokPanel'

const mockPacket: DebugPacket = {
  issue_summary: 'Checkout fails after retry',
  anchors: { feature_slugs: ['checkout'], module_slugs: [], error_signatures: ['PaymentRetryError'], symptoms: [] },
  anchor_count: 2,
  relevant_files: [{ repo_path: 'src/checkout/retry.py', match_count: 2 }],
  known_issues: [{ known_issue_id: 'ki:1', summary: 'Retry count not reset', status: 'open', match_count: 2 }],
  recent_fixes: [{ fix_id: 'fix:1', summary: 'Reset retry counter', kind: 'code-change', match_count: 2 }],
  recent_commits: [],
  evidence: [],
  confidence: 0.8,
}

const completeRun: ModokRun = { ticket_id: 'ACME-1842', status: 'complete', debug_packet: mockPacket }
const failedRun: ModokRun = { ticket_id: 'ACME-1842', status: 'failed', error: 'quine_unreachable' }
const notRun: ModokRun = { ticket_id: 'ACME-1842', status: 'not_run' }

// @spec DEMO-MODOK-001
it('shows Build Debug Packet button when status is not_run', () => {
  render(<ModokPanel ticketId="ACME-1842" run={notRun} onRun={jest.fn()} onClear={jest.fn()} />)
  expect(screen.getByRole('button', { name: /build debug packet/i })).toBeInTheDocument()
})

// @spec DEMO-MODOK-001
it('shows Build Debug Packet button when last run failed', () => {
  render(<ModokPanel ticketId="ACME-1842" run={failedRun} onRun={jest.fn()} onClear={jest.fn()} />)
  expect(screen.getByRole('button', { name: /build debug packet/i })).toBeInTheDocument()
})

// @spec DEMO-MODOK-002, DEMO-MODOK-003
it('shows spinner and disables button while POST is in flight', () => {
  render(<ModokPanel ticketId="ACME-1842" run={notRun} onRun={jest.fn()} onClear={jest.fn()} isRunning={true} />)
  expect(screen.queryByRole('button', { name: /build debug packet/i })).not.toBeInTheDocument()
  expect(screen.getByRole('status')).toBeInTheDocument() // spinner
})

// @spec DEMO-MODOK-004
it('renders debug packet when POST completes successfully', async () => {
  const onRun = jest.fn().mockResolvedValue(completeRun)
  const { rerender } = render(<ModokPanel ticketId="ACME-1842" run={notRun} onRun={onRun} onClear={jest.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: /build debug packet/i }))
  rerender(<ModokPanel ticketId="ACME-1842" run={completeRun} onRun={onRun} onClear={jest.fn()} />)
  await waitFor(() => {
    expect(screen.getByText(/checkout fails after retry/i)).toBeInTheDocument()
  })
})

// @spec DEMO-MODOK-011
it('displays debug packet on initial load when status is already complete', () => {
  render(<ModokPanel ticketId="ACME-1842" run={completeRun} onRun={jest.fn()} onClear={jest.fn()} />)
  expect(screen.getByText(/checkout fails after retry/i)).toBeInTheDocument()
})

// @spec DEMO-MODOK-005
it('omits sections with no items', () => {
  const sparsePacket: DebugPacket = { ...mockPacket, known_issues: [], recent_fixes: [] }
  const sparseRun: ModokRun = { ticket_id: 'X', status: 'complete', debug_packet: sparsePacket }
  render(<ModokPanel ticketId="X" run={sparseRun} onRun={jest.fn()} onClear={jest.fn()} />)
  expect(screen.queryByText(/known issues/i)).not.toBeInTheDocument()
  expect(screen.queryByText(/prior fixes/i)).not.toBeInTheDocument()
})

// @spec DEMO-MODOK-006
it('renders Raw JSON section collapsed by default', () => {
  render(<ModokPanel ticketId="ACME-1842" run={completeRun} onRun={jest.fn()} onClear={jest.fn()} />)
  // collapsed: pre/code block is not rendered until toggled
  expect(screen.queryByRole('code')).not.toBeInTheDocument()
})

// @spec DEMO-MODOK-007
it('shows quine_unreachable error message when run failed with that error', () => {
  render(<ModokPanel ticketId="ACME-1842" run={failedRun} onRun={jest.fn()} onClear={jest.fn()} />)
  expect(screen.getByText(/quine/i)).toBeInTheDocument()
})

// @spec DEMO-MODOK-008
it('shows error message for issue_not_found', () => {
  const run: ModokRun = { ticket_id: 'X', status: 'failed', error: 'issue_not_found' }
  render(<ModokPanel ticketId="X" run={run} onRun={jest.fn()} onClear={jest.fn()} />)
  expect(screen.getByText(/could not find this issue/i)).toBeInTheDocument()
})

// @spec DEMO-MODOK-009
it('shows partial ingest warning banner alongside the packet', () => {
  const partialRun: ModokRun = { ...completeRun, ingest_partial: true }
  render(<ModokPanel ticketId="ACME-1842" run={partialRun} onRun={jest.fn()} onClear={jest.fn()} />)
  expect(screen.getByText(/ingestion completed with errors/i)).toBeInTheDocument()
  expect(screen.getByText(/checkout fails after retry/i)).toBeInTheDocument()
})

// @spec DEMO-MODOK-010
it('shows raw stdout in collapsible when error is parse_error', () => {
  const parseErrRun: ModokRun = { ticket_id: 'X', status: 'failed', error: 'parse_error', raw_stdout: 'unexpected output here' }
  render(<ModokPanel ticketId="X" run={parseErrRun} onRun={jest.fn()} onClear={jest.fn()} />)
  expect(screen.getByText(/unexpected output here/i)).toBeInTheDocument()
})
