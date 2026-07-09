// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { AsidesDrawer } from './AsidesDrawer'
import type { Persona } from '../types'

// Mock the REST client so the drawer smoke test is hermetic (no LLM/network).
vi.mock('../api', () => ({
  api: {
    listThreads: vi.fn().mockResolvedValue([]),
    getThread: vi.fn(),
    createThread: vi.fn(),
    postThreadMessage: vi.fn(),
  },
}))

const cast: Persona[] = [
  { name: 'Dr. Emily Chen', persona: 'A cautious veterinarian', goals: [] },
  { name: 'Dr. Marcus Webb', persona: 'A liability-focused lawyer', goals: [] },
]

describe('AsidesDrawer', () => {
  beforeEach(() => vi.clearAllMocks())

  it('states the canon boundary and offers analyst/persona/room targets', async () => {
    render(<AsidesDrawer runId="r1" cast={cast} onClose={() => {}} />)

    // Canon boundary is explicit — asides are never part of the conversation.
    expect(
      screen.getByText(/Aside — not part of the conversation/i),
    ).toBeInTheDocument()

    // All three targets are selectable, plus a specific persona from the cast.
    expect(screen.getByText(/Analyst \(about the whole run\)/i)).toBeInTheDocument()
    expect(screen.getByText(/A persona \(in character\)/i)).toBeInTheDocument()
    expect(screen.getByText(/The room \(all personas\)/i)).toBeInTheDocument()

    await waitFor(() => expect(screen.getByText(/Threads \(0\)/)).toBeInTheDocument())
  })

  it('shows the disabled Phase 2 "bring into conversation" affordance in a thread', async () => {
    const { api } = await import('../api')
    ;(api.createThread as any).mockResolvedValue({
      id: 't1',
      run_id: 'r1',
      target: 'analyst',
      persona_name: null,
      mode: 'aside',
      created_at: 0,
      message_count: 0,
      total_cost_usd: 0,
    })
    ;(api.getThread as any).mockResolvedValue({
      id: 't1',
      run_id: 'r1',
      target: 'analyst',
      persona_name: null,
      mode: 'aside',
      created_at: 0,
      messages: [],
      total_cost_usd: 0,
    })

    render(<AsidesDrawer runId="r1" cast={cast} onClose={() => {}} />)
    screen.getByText('Start').click()

    await waitFor(() => {
      const btn = screen.getByTitle(/available in a later version/i)
      expect(btn).toBeDisabled()
      expect(btn).toHaveTextContent(/bring into conversation/i)
    })
  })
})
