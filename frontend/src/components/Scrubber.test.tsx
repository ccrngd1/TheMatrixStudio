// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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

  const renderScrubber = (props: Partial<{ onBranch: (...a: unknown[]) => void }> = {}) =>
    render(
      <Scrubber runId="r1" maxTurn={3} cast={cast} onBranch={props.onBranch ?? (() => {})} />,
    )

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

    expect(onBranch).toHaveBeenCalledWith(2, undefined, undefined)
  })

  // ------------------------------------------------------------------
  // One action, optionally carrying a change
  //
  // Previously two buttons sat side by side — "Branch from here" and
  // "⚡ Intervene" — which read as rival actions. They were not: both called the
  // same fork, and Intervene only toggled a panel open. These lock the collapsed
  // shape so the confusing one cannot come back.
  // ------------------------------------------------------------------

  it('offers exactly one branch action', async () => {
    renderScrubber()
    await waitFor(() => expect(screen.getByLabelText('checkpoint turn')).toBeInTheDocument())
    expect(screen.getAllByRole('button', { name: /Branch/ })).toHaveLength(1)
    // The old toggle is gone, not merely hidden.
    expect(screen.queryByRole('button', { name: /Intervene/i })).not.toBeInTheDocument()
  })

  it('shows the change options without needing a second click', async () => {
    // They used to be behind the Intervene toggle, so an operator could not see that
    // branching could carry a change at all.
    renderScrubber()
    await waitFor(() => expect(screen.getByLabelText('checkpoint turn')).toBeInTheDocument())
    const select = screen.getByRole('combobox', { name: /Change at this turn/ })
    expect(select).toHaveValue('none')
    for (const kind of [
      'inject_message', 'continue', 'edit_goal', 'add_persona', 'remove_persona',
      'adaptive_pressure',
    ]) {
      expect(within(select).getByRole('option', { name: new RegExp(kind.split('_')[0], 'i') })).toBeTruthy()
    }
  })

  it('forks unchanged by default — no mutation sent', async () => {
    // "none" is the default because forking as-is is the safe, common action.
    const onBranch = vi.fn()
    renderScrubber({ onBranch })
    await waitFor(() => expect(screen.getByLabelText('checkpoint turn')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /Branch from here/ }))
    expect(onBranch).toHaveBeenCalledTimes(1)
    expect(onBranch.mock.calls[0][1]).toBeUndefined()
  })

  it('sends the chosen change through the same button', async () => {
    const onBranch = vi.fn()
    renderScrubber({ onBranch })
    await waitFor(() => expect(screen.getByLabelText('checkpoint turn')).toBeInTheDocument())
    fireEvent.change(screen.getByRole('combobox', { name: /Change at this turn/ }), {
      target: { value: 'continue' },
    })
    // The label changes so it is obvious the fork now carries something.
    const button = screen.getByRole('button', { name: /Branch with change/ })
    fireEvent.click(button)
    expect(onBranch.mock.calls[0][1]).toMatchObject({ kind: 'continue' })
  })

  it('explains what branching does, and what a change makes it ask', async () => {
    // The distinction that makes the feature comprehensible, and which no tooltip
    // stated before: "what else might have happened" vs "what if this differed".
    renderScrubber()
    await waitFor(() => expect(screen.getByLabelText('checkpoint turn')).toBeInTheDocument())
    const tips = screen.getAllByRole('tooltip').map((t) => t.textContent ?? '')
    expect(tips.some((t) => /what else might have happened/i.test(t))).toBe(true)
    expect(tips.some((t) => /what if this had been different/i.test(t))).toBe(true)
    // And that the original is never touched, which is the safety property.
    expect(screen.getByText(/this run is never modified/i)).toBeInTheDocument()
  })

  it('explains every change option, including the experimental one', async () => {
    renderScrubber()
    await waitFor(() => expect(screen.getByLabelText('checkpoint turn')).toBeInTheDocument())
    const tips = screen.getAllByRole('tooltip').map((t) => t.textContent ?? '').join(' ')
    expect(tips).toMatch(/Inject message/)
    expect(tips).toMatch(/Continue/)
    expect(tips).toMatch(/Edit goal/)
    expect(tips).toMatch(/Add \/ remove persona/)
    // Adaptive pressure is experimental with a hard agency guard a user cannot guess.
    expect(tips).toMatch(/Adaptive pressure/)
    expect(tips).toMatch(/never a participant's choices/)
  })

  describe('start over with this setup', () => {
    it('offers a separate action that does not depend on the selected turn', async () => {
      const onStartFresh = vi.fn()
      const onBranch = vi.fn()
      render(
        <Scrubber runId="r1" maxTurn={3} cast={cast} onBranch={onBranch}
          onStartFresh={onStartFresh} />,
      )
      await waitFor(() => expect(screen.getByLabelText('checkpoint turn')).toBeInTheDocument())

      // Move the scrubber somewhere other than the end to prove the turn is ignored.
      fireEvent.change(screen.getByLabelText('checkpoint turn'), { target: { value: '1' } })
      fireEvent.click(screen.getByRole('button', { name: /start over with this setup/i }))

      expect(onStartFresh).toHaveBeenCalledTimes(1)
      // No arguments: there is no turn and no mutation to carry.
      expect(onStartFresh.mock.calls[0]).toHaveLength(0)
      // And it is NOT a branch — the two must not be confusable.
      expect(onBranch).not.toHaveBeenCalled()
    })

    it('is hidden when the host does not supply the handler', async () => {
      renderScrubber()
      await waitFor(() => expect(screen.getByLabelText('checkpoint turn')).toBeInTheDocument())
      expect(screen.queryByRole('button', { name: /start over/i })).toBeNull()
    })

    it('is not offered as one more branch mutation kind', async () => {
      // It replays nothing and keeps no transcript, so listing it beside the
      // mutations would misdescribe it as a variation on "fork from turn N".
      render(
        <Scrubber runId="r1" maxTurn={3} cast={cast} onBranch={() => {}}
          onStartFresh={() => {}} />,
      )
      await waitFor(() => expect(screen.getByLabelText('checkpoint turn')).toBeInTheDocument())
      const options = Array.from(
        screen.getByRole('combobox', { name: /change at this turn/i })
          .querySelectorAll('option'),
      ).map((o) => o.textContent ?? '')
      expect(options.some((o) => /start over|fresh/i.test(o))).toBe(false)
    })

    it('says what does and does not carry over', async () => {
      // The whole risk of this control is a user assuming it continues the
      // conversation. The explanation has to rule that out explicitly.
      render(
        <Scrubber runId="r1" maxTurn={3} cast={cast} onBranch={() => {}}
          onStartFresh={() => {}} />,
      )
      await waitFor(() => expect(screen.getByLabelText('checkpoint turn')).toBeInTheDocument())
      const tips = screen.getAllByRole('tooltip').map((t) => t.textContent ?? '').join(' ')
      expect(tips).toMatch(/nothing from the transcript carries over/i)
      expect(tips).toMatch(/turn 0/)
      // What it IS for: editing the premise.
      expect(tips).toMatch(/convictions/i)
      expect(tips).toMatch(/documents/i)
    })
  })
})
