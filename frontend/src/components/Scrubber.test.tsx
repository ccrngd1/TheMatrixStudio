// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { Scrubber } from './Scrubber'
import type { Persona, SimEvent } from '../types'

// Mock the REST client so the scrubber smoke test is hermetic (no network).
vi.mock('../api', () => ({
  api: {
    getEvents: vi.fn(),
  },
}))

const cast: Persona[] = [
  { name: 'Ada', persona: 'An ethicist', goals: [] },
  { name: 'Ben', persona: 'An engineer', goals: [] },
]

// A tiny 3-turn event log: Ada (t1), Ben (t2), Ada (t3).
const events: SimEvent[] = [
  { run_id: 'r1', turn: 0, seq: 0, event_type: 'sim.started', agent_name: null, payload: { topic: 'x' } },
  { run_id: 'r1', turn: 1, seq: 1, event_type: 'agent.response', agent_name: 'Ada', payload: { speaker: 'Ada', message: 'first from Ada', cost_usd: 0.001 } },
  { run_id: 'r1', turn: 2, seq: 2, event_type: 'agent.response', agent_name: 'Ben', payload: { speaker: 'Ben', message: 'second from Ben', cost_usd: 0.001 } },
  { run_id: 'r1', turn: 3, seq: 3, event_type: 'agent.response', agent_name: 'Ada', payload: { speaker: 'Ada', message: 'third from Ada', cost_usd: 0.001 } },
]

describe('Scrubber', () => {
  beforeEach(() => vi.clearAllMocks())

  it('renders the feed as of the current turn (read-only)', async () => {
    const { api } = await import('../api')
    ;(api.getEvents as any).mockResolvedValue(events)

    render(<Scrubber runId="r1" maxTurn={3} cast={cast} onBranch={() => {}} />)

    // Read-only badge + branch action are present.
    expect(screen.getByText(/read-only/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /branch from here/i })).toBeInTheDocument()

    // At max turn (3), all three messages are shown.
    await waitFor(() =>
      expect(screen.getByText('third from Ada')).toBeInTheDocument(),
    )
    expect(screen.getByText('first from Ada')).toBeInTheDocument()
    expect(screen.getByText('second from Ben')).toBeInTheDocument()
  })

  it('scrubbing to an earlier turn shows only state up to that turn', async () => {
    const { api } = await import('../api')
    ;(api.getEvents as any).mockResolvedValue(events)

    render(<Scrubber runId="r1" maxTurn={3} cast={cast} onBranch={() => {}} />)
    await waitFor(() => expect(screen.getByText('third from Ada')).toBeInTheDocument())

    // Move the slider back to turn 1 — only Ada's first message should remain.
    const slider = screen.getByLabelText(/checkpoint turn/i) as HTMLInputElement
    fireEvent.change(slider, { target: { value: '1' } })

    expect(screen.getByText('first from Ada')).toBeInTheDocument()
    expect(screen.queryByText('second from Ben')).not.toBeInTheDocument()
    expect(screen.queryByText('third from Ada')).not.toBeInTheDocument()
    expect(screen.getByText(/turn 1 \/ 3/i)).toBeInTheDocument()
  })

  it('"Branch from here" fires onBranch with the current scrubber turn', async () => {
    const { api } = await import('../api')
    ;(api.getEvents as any).mockResolvedValue(events)
    const onBranch = vi.fn()

    render(<Scrubber runId="r1" maxTurn={3} cast={cast} onBranch={onBranch} />)
    await waitFor(() => expect(screen.getByText('third from Ada')).toBeInTheDocument())

    const slider = screen.getByLabelText(/checkpoint turn/i) as HTMLInputElement
    fireEvent.change(slider, { target: { value: '2' } })
    fireEvent.click(screen.getByRole('button', { name: /branch from here/i }))

    expect(onBranch).toHaveBeenCalledWith(2, undefined)
  })
})
