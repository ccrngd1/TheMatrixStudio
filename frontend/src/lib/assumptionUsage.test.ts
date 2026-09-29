// SPDX-License-Identifier: Apache-2.0
// The same cases as tests/test_assumptions.py's usage tests, so the card and the report cannot drift apart.
import { describe, expect, it } from 'vitest'
import { assumptionUsage } from './assumptionUsage'
import type { FeedMessage } from '../types'

const m = (seq: number, speaker: string, content: string, extra: Partial<FeedMessage> = {}): FeedMessage =>
  ({ seq, turn: seq, speaker, content, ...extra })

const FEED = [
  m(1, 'Marcus', "Using A1's 2-5% range as our baseline, we ship."),
  m(2, 'Dana', 'I can work with it. But A1 is a benchmark, not a guarantee our users behave the same way.'),
  m(3, 'Marcus', "If A2 doesn't hold and we get 12 responses, we pause."),
  m(4, 'Dana', 'A12 is irrelevant here. That 7% is soft.'),
  m(5, 'Customer', 'A1 is wrong.', { injected: true }),
]

describe('assumptionUsage', () => {
  it('counts citations and flags disputes, but not conditionals, word-only ids, or injected text', () => {
    const u = assumptionUsage(['A1', 'A2'], FEED)
    expect(u.A1.cited).toBe(2)
    expect(u.A1.disputes.map((d) => d.speaker)).toEqual(['Dana'])
    expect(u.A1.disputes[0].sentence).toMatch(/not a guarantee/)
    expect(u.A2).toEqual({ cited: 1, disputes: [] })
  })
})
