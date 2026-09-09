// SPDX-License-Identifier: Apache-2.0
/**
 * Terminal events must end the derived stream state.
 *
 * `deriveState` only knew `sim.completed` and `sim.failed`, so a run that ended any
 * other way stayed `running` in the UI until the page was reloaded: the Stop button
 * kept being offered on a finished run, the "thinking" indicator never cleared, and
 * the analysis affordances (which key off an ended run) stayed hidden.
 *
 * `sim.capped` predates the stop feature and had the same hole, so it is covered here
 * too rather than left for the next person to find.
 */
import { describe, expect, it } from 'vitest'
import { deriveState, initialState } from './simState'
import type { Persona, SimEvent } from '../types'

const cast: Persona[] = [{ name: 'Ada', persona: 'an ethicist', goals: [] }]

const event = (type: SimEvent['event_type'], turn: number, payload = {}): SimEvent => ({
  run_id: 'r1',
  turn,
  seq: turn,
  event_type: type,
  agent_name: null,
  payload,
})

const upTo = (terminal: SimEvent['event_type']) =>
  deriveState(initialState(cast), [
    event('sim.started', 0, { topic: 'x' }),
    {
      ...event('agent.response', 1),
      agent_name: 'Ada',
      payload: { speaker: 'Ada', message: 'A turn.', cost_usd: 0.001 },
    },
    event(terminal, 1, { total_turns: 1, total_cost_usd: 0.001 }),
  ])

describe('terminal events end the run', () => {
  it.each([
    ['sim.stopped', 'stopped'],
    ['sim.capped', 'capped'],
    ['sim.interrupted', 'interrupted'],
    ['sim.completed', 'complete'],
    ['sim.failed', 'failed'],
  ] as const)('%s -> status %s', (eventType, expected) => {
    const state = upTo(eventType)
    expect(state.status).toBe(expected)
    // Whatever ended it, nothing is still in flight.
    expect(state.thinking).toBe(false)
    expect(state.activeSpeaker).toBeNull()
  })

  it('keeps the transcript generated before a stop', () => {
    // The point of stopping after the current turn: what was paid for is kept.
    const state = upTo('sim.stopped')
    expect(state.feed.map((m) => m.content)).toContain('A turn.')
  })

  it('distinguishes a stop from a completion and from a failure', () => {
    // Folding stopped into complete would claim the discussion finished; folding it
    // into failed would claim something went wrong. Neither is true.
    expect(upTo('sim.stopped').status).not.toBe('complete')
    expect(upTo('sim.stopped').status).not.toBe('failed')
    // A stop is not an error condition, so no error message is set.
    expect(upTo('sim.stopped').error).toBeNull()
    expect(upTo('sim.failed').error).toBeTruthy()
  })
})
