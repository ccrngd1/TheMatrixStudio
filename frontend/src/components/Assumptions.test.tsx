// SPDX-License-Identifier: Apache-2.0
// Working assumptions: folded out of the event stream, shown where they were made, never as speech.
import { describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
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
})
