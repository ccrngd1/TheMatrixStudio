// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from 'vitest'
import { deriveState, initialState } from './simState'
import type { SimEvent } from '../types'

const cast = [
  { name: 'Ada', persona: 'ethicist', goals: ['ask'] },
  { name: 'Ben', persona: 'engineer', goals: ['build'] },
]

function ev(seq: number, type: SimEvent['event_type'], payload: any, agent: string | null = null): SimEvent {
  return { run_id: 'r', turn: seq, seq, event_type: type, agent_name: agent, payload }
}

describe('simState reducer', () => {
  it('accumulates cost, tokens, and messages from agent.response', () => {
    const events: SimEvent[] = [
      ev(0, 'sim.started', { topic: 'AI ethics', agent_count: 2 }),
      ev(1, 'speaker.selected', { speaker: 'Ada' }, 'Ada'),
      ev(2, 'agent.response', { speaker: 'Ada', message: 'Hi', tokens_in: 10, tokens_out: 5, cost_usd: 0.001 }, 'Ada'),
      ev(3, 'sim.completed', { total_turns: 1 }),
    ]
    const s = deriveState(initialState(cast), events)
    expect(s.status).toBe('complete')
    expect(s.feed).toHaveLength(1)
    expect(s.feed[0].content).toBe('Hi')
    expect(s.totalCost).toBeCloseTo(0.001)
    expect(s.totalTokensIn).toBe(10)
    expect(s.agents.Ada.messageCount).toBe(1)
  })

  it('reflects avatar.ready portrait and placeholder fallback', () => {
    const withPortrait = deriveState(initialState(cast), [
      ev(0, 'avatar.ready', { agent_name: 'Ada', portrait_b64: 'IMG' }, 'Ada'),
      ev(1, 'avatar.ready', { agent_name: 'Ben', portrait_b64: null }, 'Ben'),
    ])
    expect(withPortrait.agents.Ada.portrait).toBe('IMG')
    expect(withPortrait.agents.Ada.avatarResolved).toBe(true)
    // Null portrait still marks resolved (UI shows placeholder, no crash).
    expect(withPortrait.agents.Ben.portrait).toBeNull()
    expect(withPortrait.agents.Ben.avatarResolved).toBe(true)
  })

  it('tracks active speaker and thinking state', () => {
    const thinking = deriveState(initialState(cast), [
      ev(1, 'speaker.selected', { speaker: 'Ada' }, 'Ada'),
    ])
    expect(thinking.activeSpeaker).toBe('Ada')
    expect(thinking.thinking).toBe(true)

    const spoke = deriveState(initialState(cast), [
      ev(1, 'speaker.selected', { speaker: 'Ada' }, 'Ada'),
      ev(2, 'agent.response', { speaker: 'Ada', message: 'x', cost_usd: 0 }, 'Ada'),
    ])
    expect(spoke.thinking).toBe(false)
    expect(spoke.activeSpeaker).toBeNull()
  })

  it('partial reveal (playback) shows only revealed events', () => {
    const all: SimEvent[] = [
      ev(0, 'sim.started', { topic: 't' }),
      ev(1, 'speaker.selected', { speaker: 'Ada' }, 'Ada'),
      ev(2, 'agent.response', { speaker: 'Ada', message: 'one', cost_usd: 0 }, 'Ada'),
      ev(3, 'agent.response', { speaker: 'Ben', message: 'two', cost_usd: 0 }, 'Ben'),
    ]
    // Reveal only up to seq 2 → only one message visible even though more buffered.
    const partial = deriveState(initialState(cast), all.slice(0, 3))
    expect(partial.feed).toHaveLength(1)
    expect(partial.status).toBe('running')
  })
})
