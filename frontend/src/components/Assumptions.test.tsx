// SPDX-License-Identifier: Apache-2.0
// Working assumptions: folded out of the event stream, shown where they were made, never as speech.
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { ConversationFeed } from './ConversationFeed'
import { assumptionsConfig } from './AssumptionsEditor'
import { deriveState, initialState } from '../lib/simState'
import type { AgentView, FeedMessage, SimEvent } from '../types'

vi.mock('../api', () => ({ loadAvatar: vi.fn().mockResolvedValue(null), avatarUrl: () => null }))

const agent = (name: string): AgentView => ({
  name, persona: 'p', goals: [], portrait: null, portraitKey: null, portraitUrl: null,
  avatarResolved: true, messageCount: 1, tokensIn: 0, tokensOut: 0, costUsd: 0,
})

describe('working assumptions', () => {
  it('are folded into state, not into the feed', () => {
    const ev = (seq: number, turn: number, payload: any): SimEvent =>
      ({ seq, turn, event_type: 'assumption.made', agent_name: null, payload, timestamp: '' }) as any
    const s = deriveState(initialState([]), [
      ev(1, 0, { id: 'A1', statement: 'Churn is 7%', basis: 'cohort', source: 'operator', turn: 0 }),
      ev(2, 0, { id: 'A2', statement: '', source: 'operator' }),
    ])
    expect(s.feed).toEqual([])
    expect(s.assumptions).toEqual([
      { id: 'A1', statement: 'Churn is 7%', basis: 'cohort', source: 'operator', turn: 0 },
    ])
  })

  it('show above the first message when set before the run, and after a turn when made at it', () => {
    const feed: FeedMessage[] = [
      { turn: 1, seq: 2, speaker: 'Ada', content: 'first' },
      { turn: 2, seq: 4, speaker: 'Bo', content: 'second' },
    ]
    const { container } = render(
      <ConversationFeed
        feed={feed}
        agents={{ Ada: agent('Ada'), Bo: agent('Bo') }}
        activeSpeaker={null}
        thinking={false}
        assumptions={[
          { id: 'A1', statement: 'Churn is 7%', basis: '', source: 'operator', turn: 0 },
          { id: 'A2', statement: 'Launch is fixed', basis: 'two guesses', source: 'moderator', turn: 1 },
        ]}
      />,
    )
    const text = container.textContent ?? ''
    expect(text.indexOf('Churn is 7%')).toBeLessThan(text.indexOf('first'))
    expect(text.indexOf('first')).toBeLessThan(text.indexOf('Launch is fixed'))
    expect(text.indexOf('Launch is fixed')).toBeLessThan(text.indexOf('second'))
    expect(screen.getByText(/set before the run/)).toBeInTheDocument()
    expect(within(container).getByText(/made at turn 1 by the moderator; basis: two guesses/)).toBeInTheDocument()
  })

  it('send only stated ones, basis only when given', () => {
    expect(assumptionsConfig([
      { statement: ' Churn is 7% ', basis: '' },
      { statement: '  ', basis: 'x' },
      { statement: 'Fixed date', basis: 'plan' },
    ])).toEqual([{ statement: 'Churn is 7%' }, { statement: 'Fixed date', basis: 'plan' }])
  })

  it('fork from the assumption\'s own turn with the new value, or withdrawn', async () => {
    const onFork = vi.fn().mockResolvedValue(undefined)
    render(
      <ConversationFeed
        feed={[{ turn: 1, seq: 2, speaker: 'Ada', content: 'first' }]}
        agents={{ Ada: agent('Ada') }}
        activeSpeaker={null}
        thinking={false}
        assumptions={[{ id: 'A1', statement: 'Churn is 7%', basis: '', source: 'operator', turn: 0 }]}
        onForkAssumption={onFork}
      />,
    )
    fireEvent.click(screen.getByText('Fork with a different assumption'))
    fireEvent.change(screen.getByLabelText('New value for A1'), { target: { value: 'Churn is 12%' } })
    fireEvent.click(screen.getByRole('button', { name: 'Fork from turn 0' }))
    await waitFor(() => expect(onFork).toHaveBeenCalledWith(expect.objectContaining({ id: 'A1' }), 'Churn is 12%'))
    // Once forked the card stays busy: the view is about to move to the new run.
    expect(screen.getByText('Withdraw it instead')).toBeDisabled()
  })

  it('withdraw forks with no value', async () => {
    const onFork = vi.fn().mockResolvedValue(undefined)
    render(
      <ConversationFeed feed={[]} agents={{}} activeSpeaker={null} thinking={false} onForkAssumption={onFork}
        assumptions={[{ id: 'A1', statement: 'Churn is 7%', basis: '', source: 'operator', turn: 0 }]} />,
    )
    fireEvent.click(screen.getByText('Fork with a different assumption'))
    fireEvent.click(screen.getByText('Withdraw it instead'))
    await waitFor(() => expect(onFork).toHaveBeenCalledWith(expect.objectContaining({ id: 'A1' }), null))
  })

  it('offer no fork while the run is live', () => {
    render(
      <ConversationFeed feed={[]} agents={{}} activeSpeaker={null} thinking={false}
        assumptions={[{ id: 'A1', statement: 'Churn is 7%', basis: '', source: 'operator', turn: 0 }]} />,
    )
    expect(screen.queryByText('Fork with a different assumption')).not.toBeInTheDocument()
  })

  it('a shift flag sits on the message it was found in, and says when no stated condition is named', () => {
    const ev = (seq: number, turn: number, type: SimEvent['event_type'], payload: any, agent: string | null = null): SimEvent =>
      ({ run_id: 'r', seq, turn, event_type: type, agent_name: agent, payload }) as any
    const s = deriveState(initialState([]), [
      ev(1, 1, 'agent.response', { speaker: 'Theo', message: "Mina drew the line, and I'll give ground on it." }, 'Theo'),
      ev(2, 1, 'position.shift', { speaker: 'Theo', sentences: ['x'], credits: [{ kind: 'persona', name: 'Mina' }],
        conditions: [{ position: 'p', firmness: 'firm', condition: 'a regulation' }],
        matched_conditions: [], no_listed_condition: true }, 'Theo'),
    ])
    expect(s.feed[0].shift?.no_listed_condition).toBe(true)
    render(<ConversationFeed feed={s.feed} agents={{ Theo: agent('Theo') }} activeSpeaker={null} thinking={false} />)
    // Both names are personas, so both carry the simulated-persona marker; the words are around them.
    const flag = screen.getByText(/says their position moved/).closest('.cc-shift') as HTMLElement
    expect(flag).toHaveTextContent(/Theo says their position moved/)
    expect(flag).toHaveTextContent(/credits Mina \(persona\)/)
    expect(within(flag).getAllByRole('img', { name: 'simulated persona' })).toHaveLength(2)
    expect(screen.getByText('a regulation')).toBeInTheDocument()
  })
})
