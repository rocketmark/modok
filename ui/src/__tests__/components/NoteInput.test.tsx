import React from 'react'
import { render, screen, fireEvent } from '@testing-library/react'

// Component not implemented yet — import will fail until Phase 6.
import { NoteInput } from '@/components/tickets/NoteInput'

// @spec DEMO-NOTE-001
it('renders an input field and Add button', () => {
  render(<NoteInput onAdd={jest.fn()} isDisabled={false} />)
  expect(screen.getByRole('textbox')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /add/i })).toBeInTheDocument()
})

// @spec DEMO-NOTE-002
it('Add button is disabled while the body field is empty', () => {
  render(<NoteInput onAdd={jest.fn()} isDisabled={false} />)
  expect(screen.getByRole('button', { name: /add/i })).toBeDisabled()
})

// @spec DEMO-NOTE-002
it('Add button is enabled once the body field has text', () => {
  render(<NoteInput onAdd={jest.fn()} isDisabled={false} />)
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'A note' } })
  expect(screen.getByRole('button', { name: /add/i })).toBeEnabled()
})

// @spec DEMO-NOTE-003
it('calls onAdd with the body text when Add is clicked', () => {
  const onAdd = jest.fn()
  render(<NoteInput onAdd={onAdd} isDisabled={false} />)
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Confirmed on staging' } })
  fireEvent.click(screen.getByRole('button', { name: /add/i }))
  expect(onAdd).toHaveBeenCalledWith('Confirmed on staging')
})

// @spec DEMO-NOTE-004
it('disables input and Add button while isDisabled is true', () => {
  render(<NoteInput onAdd={jest.fn()} isDisabled={true} />)
  expect(screen.getByRole('textbox')).toBeDisabled()
  expect(screen.getByRole('button', { name: /add/i })).toBeDisabled()
})
