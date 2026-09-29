// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from 'vitest'
import { describeFork, forkEstimate } from './forkCost'
import type { SimEvent } from '../types'

const ev = (turn: number, type: SimEvent['event_type'], cost?: number): SimEvent =>
  ({ run_id: 'r', turn, seq: turn * 10, event_type: type, agent_name: null,
     payload: cost === undefined ? {} : { cost_usd: cost } })

describe('forkEstimate', () => {
  const events = [
    ev(0, 'sim.started', 0.08), ev(1, 'agent.response', 0.01), ev(1, 'speaker.selected', 0.001),
    ev(2, 'agent.response', 0.02), ev(2, 'assumption.checked' as SimEvent['event_type'], 0.002),
    ev(3, 'agent.response', 0.03),
  ]
  it('is what the run spent after the fork, every kind of call included', () => {
    const f = forkEstimate(events, 1)
    expect(f.turns).toBe(2)
    expect(f.costUsd).toBeCloseTo(0.052)
  })
  it('costs nothing from the last turn, and says so', () => {
    const f = forkEstimate(events, 3)
    expect(f).toEqual({ turns: 0, costUsd: 0 })
    expect(describeFork(f)).toMatch(/new turns only/)
  })
  it('names the turns and the amount', () => {
    expect(describeFork(forkEstimate(events, 0))).toMatch(/about \$0\.063 — .* 3 turns after it/)
  })
})
