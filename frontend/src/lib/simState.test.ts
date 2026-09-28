// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from 'vitest'
import { applyEvent, deriveState, initialState } from './simState'
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

describe('cost counts every model call', () => {
  it('adds costs recorded on events other than agent.response', () => {
    // Selection, validation, rejected attempts, reflections and avatars record their cost on
    // their own events; counting replies alone under-reported a real run by ~18%.
    let s = initialState([])
    const ev = (event_type: string, payload: Record<string, unknown>, seq: number) =>
      ({ run_id: 'r', turn: 1, seq, event_type, agent_name: 'Ada', payload }) as never
    s = applyEvent(s, ev('avatar.ready', { agent_name: 'Ada', portrait_key: 'k', cost_usd: 0.08 }, 1))
    s = applyEvent(s, ev('speaker.selected', { speaker: 'Ada', cost_usd: 0.007 }, 2))
    s = applyEvent(s, ev('validation.checked', { speaker: 'Ada', passed: false, cost_usd: 0.1 }, 3))
    s = applyEvent(s, ev('agent.response', { speaker: 'Ada', message: 'hi', cost_usd: 0.1 }, 4))
    expect(s.totalCost).toBeCloseTo(0.287, 10)
  })
})

describe('document.retrieved folds onto the message it fed', () => {
  const ev = (seq: number, event_type: string, payload: Record<string, unknown>, turn = 1) =>
    ({ run_id: 'r1', turn, seq, event_type, agent_name: 'Ada', payload }) as never
  const passage = (chunk_id: number, ordinal: number) =>
    ({ chunk_id, document_id: 'd1', title: 'spec.md', ordinal, score: 1, chars: 10 })

  it("attaches the turn's passages, narrowed to document_refs, and indexes them for the run", () => {
    const s = deriveState(initialState([]), [
      ev(1, 'document.retrieved', { speaker: 'Ada', passages: [passage(10, 1), passage(11, 2)] }),
      ev(2, 'agent.response', {
        speaker: 'Ada', content: 'per spec.md #1', document_refs: [10],
        citation_provenance: [{ label: 'spec.md #1', title: 'spec.md', kind: 'firsthand', attributive: true }],
      }),
    ])
    expect(s.feed[0].sources?.map((p) => p.chunk_id)).toEqual([10])
    expect(s.feed[0].citations?.[0].kind).toBe('firsthand')
    expect(Object.keys(s.sourceIndex).sort()).toEqual(['spec.md #1', 'spec.md #2'])
    expect(s.retrieved).toEqual({})
  })

  it('leaves a message with no retrieval exactly as before', () => {
    const s = deriveState(initialState([]), [ev(1, 'agent.response', { speaker: 'Ada', content: 'hi' })])
    expect(s.feed[0]).toEqual({ turn: 1, seq: 1, speaker: 'Ada', content: 'hi' })
  })
})

describe('a consultant answer joins the feed but is not a participant', () => {
  const ev = (seq: number, event_type: string, payload: Record<string, unknown>, agent = 'Ada') =>
    ({ run_id: 'r1', turn: 2, seq, event_type, agent_name: agent, payload }) as never

  it('adds the answer with its question and sources, and no agent', () => {
    const s = deriveState(initialState([]), [
      ev(1, 'document.retrieved', { speaker: 'Ada', passages: [{ chunk_id: 5, document_id: 'd', title: 'note', ordinal: 0 }] }),
      ev(2, 'expert.answered', {
        expert: 'Ada', speaker: 'Ada (consultant)', asked_by: 'Dana', question: 'q?',
        answer: 'It says so [note #0].', document_refs: [5], cost_usd: 0.002,
      }),
    ])
    expect(s.feed[0]).toMatchObject({
      speaker: 'Ada (consultant)', content: 'It says so [note #0].',
      consultant: { expert: 'Ada', askedBy: 'Dana', question: 'q?' },
    })
    expect(s.feed[0].sources?.[0].chunk_id).toBe(5)
    expect(s.agents['Ada']).toBeUndefined()
    expect(s.totalCost).toBeCloseTo(0.002)
  })
})
