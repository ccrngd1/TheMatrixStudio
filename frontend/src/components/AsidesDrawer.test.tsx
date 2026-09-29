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
    branchRun: vi.fn().mockResolvedValue({ run_id: 'branch-1' }),
  },
}))

const cast: Persona[] = [
  { name: 'Dr. Emily Chen', persona: 'A cautious provider', goals: [] },
  { name: 'Dr. Marcus Webb', persona: 'A liability-focused lawyer', goals: [] },
]

describe('AsidesDrawer', () => {
  beforeEach(() => vi.clearAllMocks())

  it('states the canon boundary and offers analyst/persona/room targets', async () => {
    render(<AsidesDrawer runId="r1" cast={cast} turnCount={4} onClose={() => {}} />)

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
      messages: [
        { id: 1, thread_id: 't1', role: 'target' as const, speaker: 'Analyst',
          content: 'Great insight.', tokens_in: 0, tokens_out: 0, cost_usd: 0, created_at: 0 },
      ],
      total_cost_usd: 0,
    })

    render(<AsidesDrawer runId="r1" cast={cast} turnCount={4} onBranch={() => {}} onClose={() => {}} />)
    screen.getByText('Start').click()

    await waitFor(() => {
      const btn = screen.getByRole('button', { name: /bring into conversation/i })
      expect(btn).not.toBeDisabled()
      expect(btn).toHaveTextContent(/bring into conversation/i)
    })
  })
})

describe('asideError', () => {
  it('explains a bare gateway timeout and leaves an explained error alone', async () => {
    const { asideError } = await import('./AsidesDrawer')
    expect(asideError('504: Gateway Timeout')).toMatch(/took too long.*nothing was saved/)
    expect(asideError('504: The reply took longer than 24 s')).toBe('504: The reply took longer than 24 s')
    expect(asideError('404: Thread not found')).toBe('404: Thread not found')
  })
})

describe('waitForReply', () => {
  it('polls until the reply (or a recorded failure) follows the question', async () => {
    const { api } = await import('../api')
    const { waitForReply } = await import('./AsidesDrawer')
    const q = { id: 1, role: 'user', content: 'Why?' }
    ;(api.getThread as any)
      .mockResolvedValueOnce({ id: 't', messages: [q] })
      .mockResolvedValueOnce({ id: 't', messages: [q, { id: 2, role: 'error', content: 'failed' }] })
    const seen: unknown[] = []
    expect(await waitForReply('t', (t) => seen.push(t), { everyMs: 1, forMs: 1000 })).toBe(true)
    expect(seen).toHaveLength(2)
  })

  it('gives up when nothing arrives in time', async () => {
    const { api } = await import('../api')
    const { waitForReply } = await import('./AsidesDrawer')
    ;(api.getThread as any).mockResolvedValue({ id: 't', messages: [{ id: 1, role: 'user', content: 'Why?' }] })
    expect(await waitForReply('t', () => {}, { everyMs: 1, forMs: 20 })).toBe(false)
  })
})

describe('AsidesDrawer consultants', () => {
  it('offers a consultant as a target only when the run has one', async () => {
    const { rerender } = render(<AsidesDrawer runId="r1" cast={cast} turnCount={4} onClose={() => {}} />)
    expect(screen.queryByText(/A consultant \(answers from its sources\)/)).not.toBeInTheDocument()
    rerender(<AsidesDrawer runId="r1" cast={cast} turnCount={4} consultants={['Ada']} onClose={() => {}} />)
    expect(screen.getByText(/A consultant \(answers from its sources\)/)).toBeInTheDocument()
  })
})
