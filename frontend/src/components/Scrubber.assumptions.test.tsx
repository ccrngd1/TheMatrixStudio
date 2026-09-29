// SPDX-License-Identifier: Apache-2.0
// Working assumptions in the scrubber: offered only when in force at the selected turn, and sent
// through the same Branch button as every other change.
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { Scrubber } from './Scrubber'
import type { Persona, SimEvent } from '../types'

vi.mock('../api', () => ({ api: { getEvents: vi.fn() }, loadAvatar: vi.fn().mockResolvedValue(null), avatarUrl: () => null }))

const cast: Persona[] = [{ name: 'Ada', persona: 'p', goals: [] }, { name: 'Ben', persona: 'p', goals: [] }]
const ev = (seq: number, turn: number, type: SimEvent['event_type'], payload: any, agent: string | null = null): SimEvent =>
  ({ run_id: 'r1', turn, seq, event_type: type, agent_name: agent, payload })
const events: SimEvent[] = [
  ev(0, 0, 'sim.started', { topic: 'x' }),
  ev(1, 0, 'assumption.made', { id: 'A1', statement: 'Churn is 7%', source: 'operator', turn: 0 }),
  ev(2, 1, 'agent.response', { speaker: 'Ada', message: 'first' }, 'Ada'),
  ev(3, 1, 'assumption.made', { id: 'A2', statement: 'Launch in May', source: 'moderator', turn: 1 }),
  ev(4, 2, 'agent.response', { speaker: 'Ben', message: 'second' }, 'Ben'),
]

async function renderAt(onBranch = vi.fn()) {
  const { api } = await import('../api')
  ;(api.getEvents as any).mockResolvedValue(events)
  render(<Scrubber runId="r1" maxTurn={2} cast={cast} onBranch={onBranch} />)
  await waitFor(() => expect(screen.getByText('second')).toBeInTheDocument())
  return onBranch
}

describe('Scrubber and working assumptions', () => {
  it('shows the assumption cards while scrubbing', async () => {
    await renderAt()
    expect(screen.getByText('Churn is 7%')).toBeInTheDocument()
    expect(screen.getByText('Launch in May')).toBeInTheDocument()
  })

  it('offers only the assumptions in force at the selected turn', async () => {
    await renderAt()
    fireEvent.change(screen.getByLabelText(/checkpoint turn/), { target: { value: '0' } })
    fireEvent.change(screen.getByRole('combobox', { name: /change/i }), { target: { value: 'replace_assumption' } })
    const pick = screen.getByLabelText('Assumption to change')
    expect(within(pick).getAllByRole('option').map((o) => o.textContent)).toEqual(['A1: Churn is 7%'])
  })

  it('sends a replacement through the Branch button, and refuses an unchanged one', async () => {
    const onBranch = await renderAt()
    fireEvent.change(screen.getByRole('combobox', { name: /change/i }), { target: { value: 'replace_assumption' } })
    fireEvent.change(screen.getByLabelText('Assumption to change'), { target: { value: 'A2' } })
    const button = screen.getByRole('button', { name: /Branch with change/ })
    expect(button).toBeDisabled()
    fireEvent.change(screen.getByLabelText('New value'), { target: { value: 'Launch in September' } })
    fireEvent.click(button)
    expect(onBranch).toHaveBeenCalledWith(2, {
      kind: 'replace_assumption', assumption_id: 'A2', statement: 'Launch in September',
    }, undefined)
  })

  it('sends a withdrawal', async () => {
    const onBranch = await renderAt()
    fireEvent.change(screen.getByRole('combobox', { name: /change/i }), { target: { value: 'withdraw_assumption' } })
    fireEvent.click(screen.getByRole('button', { name: /Branch with change/ }))
    expect(onBranch).toHaveBeenCalledWith(2, { kind: 'withdraw_assumption', assumption_id: 'A1' }, undefined)
  })

  it('does not offer the assumption changes when none are in force', async () => {
    const { api } = await import('../api')
    ;(api.getEvents as any).mockResolvedValue(events.filter((e) => e.event_type !== 'assumption.made'))
    render(<Scrubber runId="r1" maxTurn={2} cast={cast} onBranch={() => {}} />)
    await waitFor(() => expect(screen.getByText('second')).toBeInTheDocument())
    const select = screen.getByRole('combobox', { name: /change/i })
    expect(within(select).queryByRole('option', { name: /assumption/i })).toBeNull()
  })
})
