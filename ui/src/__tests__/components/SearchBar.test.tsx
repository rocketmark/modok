import React from 'react'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

// Components not implemented yet — imports will fail until Phase 6.
import { SearchBar } from '@/components/nav/SearchBar'

global.fetch = jest.fn()
const mockFetch = global.fetch as jest.MockedFunction<typeof fetch>

const validNodes = [{ id: 1, node_type: 'Feature', name: 'checkout', summary: 'Checkout feature' }]

beforeEach(() => jest.resetAllMocks())

// @spec DEMO-SEARCH-001
it('calls /api/search when the form is submitted with a non-empty query', async () => {
  mockFetch.mockResolvedValue({ ok: true, json: async () => ({ nodes: validNodes }) } as any)
  render(<SearchBar />)
  await userEvent.type(screen.getByRole('searchbox'), 'checkout')
  fireEvent.submit(screen.getByRole('search'))
  await waitFor(() => expect(mockFetch).toHaveBeenCalledWith(expect.stringContaining('q=checkout')))
})

// @spec DEMO-SEARCH-001
it('shows results overlay after a successful search', async () => {
  mockFetch.mockResolvedValue({ ok: true, json: async () => ({ nodes: validNodes }) } as any)
  render(<SearchBar />)
  await userEvent.type(screen.getByRole('searchbox'), 'checkout')
  fireEvent.submit(screen.getByRole('search'))
  await waitFor(() => expect(screen.getByText('checkout')).toBeInTheDocument())
})

// @spec DEMO-SEARCH-002
it('shows type badge and truncated summary in each result', async () => {
  mockFetch.mockResolvedValue({ ok: true, json: async () => ({ nodes: validNodes }) } as any)
  render(<SearchBar />)
  await userEvent.type(screen.getByRole('searchbox'), 'checkout')
  fireEvent.submit(screen.getByRole('search'))
  await waitFor(() => {
    expect(screen.getByText('Feature')).toBeInTheDocument()
    expect(screen.getByText(/checkout feature/i)).toBeInTheDocument()
  })
})

// @spec DEMO-SEARCH-003
it('closes the overlay on Escape', async () => {
  mockFetch.mockResolvedValue({ ok: true, json: async () => ({ nodes: validNodes }) } as any)
  render(<SearchBar />)
  await userEvent.type(screen.getByRole('searchbox'), 'checkout')
  fireEvent.submit(screen.getByRole('search'))
  await waitFor(() => screen.getByText('checkout'))
  fireEvent.keyDown(document, { key: 'Escape' })
  await waitFor(() => expect(screen.queryByText('checkout')).not.toBeInTheDocument())
})

// @spec DEMO-SEARCH-004
it('makes no request and clears results when query is empty', async () => {
  render(<SearchBar />)
  fireEvent.submit(screen.getByRole('search'))
  expect(mockFetch).not.toHaveBeenCalled()
})

// @spec DEMO-SEARCH-008
it('shows error message in overlay when search returns non-200', async () => {
  mockFetch.mockResolvedValue({ ok: false, status: 502 } as any)
  render(<SearchBar />)
  await userEvent.type(screen.getByRole('searchbox'), 'checkout')
  fireEvent.submit(screen.getByRole('search'))
  await waitFor(() => expect(screen.getByText(/search unavailable/i)).toBeInTheDocument())
})
