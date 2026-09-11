// SPDX-License-Identifier: Apache-2.0
// The status vocabulary, and its agreement with the server's.
//
// This file exists because the SAME bug happened twice, in two copies of the same
// knowledge: `useRunStream` omitted `sim.stopped`/`sim.capped` from its terminal-event
// set, and `LiveView` omitted `pending` from its live-status check. Neither raised.
// Both produced a UI that quietly disagreed with the backend about whether a run was
// still going.

import { describe, expect, it } from 'vitest'
import {
  LIVE_STATUSES,
  RESUMABLE_STATUSES,
  TERMINAL_EVENTS,
  TERMINAL_STATUSES,
  isLive,
  isResumable,
  isStalled,
  isTerminal,
} from './runStatus'

describe('run status vocabulary', () => {
  it('matches the server list of terminal statuses', () => {
    // Mirrors `orchestration.TERMINAL_STATUSES`. Asserted as an exact set rather than
    // membership checks: the failure mode is an entry MISSING, which every `includes`
    // test passes.
    expect([...TERMINAL_STATUSES].sort()).toEqual([
      'capped',
      'complete',
      'failed',
      'interrupted',
      'stopped',
    ])
  })

  it('matches the server list of terminal events', () => {
    // Mirrors `storage.dynamo.TERMINAL_EVENT_TYPES` and `api/manager.TERMINAL_EVENTS`.
    expect([...TERMINAL_EVENTS].sort()).toEqual([
      'sim.capped',
      'sim.completed',
      'sim.failed',
      'sim.interrupted',
      'sim.stopped',
    ])
  })

  it('has one terminal event per terminal status', () => {
    // A status with no event means the log never says the run ended, so a reader polls
    // for ever. An event with no status means the row never says it either. Both
    // happened, in different directions.
    const fromEvents = new Set(
      [...TERMINAL_EVENTS].map((e) => e.replace('sim.', '').replace('completed', 'complete')),
    )
    expect([...fromEvents].sort()).toEqual([...TERMINAL_STATUSES].sort())
  })

  it('treats pending as live, not terminal', () => {
    // A run is created `pending` and its first turn flips it to `running`.
    expect(isLive('pending')).toBe(true)
    expect(isTerminal('pending')).toBe(false)
    expect(LIVE_STATUSES).toContain('pending')
  })

  it('has no status that is both live and terminal', () => {
    for (const s of LIVE_STATUSES) expect(isTerminal(s)).toBe(false)
    for (const s of TERMINAL_STATUSES) expect(isLive(s)).toBe(false)
  })

  it('mirrors the server on what is resumable', () => {
    // `branching.RESUMABLE_STATUSES`. The API rejects anything else with 409, so an
    // extra entry here offers a button that always fails.
    expect([...RESUMABLE_STATUSES].sort()).toEqual(['failed', 'interrupted', 'stopped'])
    for (const s of RESUMABLE_STATUSES) {
      expect(isTerminal(s)).toBe(true)
      expect(isResumable(s)).toBe(true)
    }
    // `complete` is deliberately NOT resumable — you branch a finished run instead,
    // because resuming it in place would rewrite a canonical timeline.
    expect(isResumable('complete')).toBe(false)
  })

  it('handles null and unknown statuses without claiming anything', () => {
    for (const s of [null, undefined, '', 'nonsense']) {
      expect(isLive(s)).toBe(false)
      expect(isTerminal(s)).toBe(false)
      expect(isResumable(s)).toBe(false)
    }
  })
})

describe('isStalled', () => {
  const NOW = 1_000_000

  it('flags a live run that has gone quiet', () => {
    expect(isStalled('running', NOW - 600, null, 120, NOW)).toBe(true)
    expect(isStalled('pending', null, NOW - 600, 120, NOW)).toBe(true)
  })

  it('does not flag a live run that is still producing', () => {
    expect(isStalled('running', NOW - 5, null, 120, NOW)).toBe(false)
    expect(isStalled('pending', null, NOW - 5, 120, NOW)).toBe(false)
  })

  it('falls back from the last event to creation', () => {
    // The fallback is what catches a run whose execution never started: it has no
    // events at all, so a check requiring `lastEventAt` skips it.
    expect(isStalled('running', null, NOW - 600, 120, NOW)).toBe(true)
    // And the last event WINS when both are present, since it is the fresher signal —
    // an old run that is currently generating is not stalled.
    expect(isStalled('running', NOW - 5, NOW - 100_000, 120, NOW)).toBe(false)
  })

  it('never flags a terminal run', () => {
    for (const s of TERMINAL_STATUSES) {
      expect(isStalled(s, NOW - 100_000, NOW - 100_000, 120, NOW)).toBe(false)
    }
  })

  it('says nothing when there is no timestamp at all', () => {
    expect(isStalled('running', null, null, 120, NOW)).toBe(false)
  })
})
