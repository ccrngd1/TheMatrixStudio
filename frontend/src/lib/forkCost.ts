// SPDX-License-Identifier: Apache-2.0
import type { SimEvent } from '../types'

export interface ForkEstimate {
  /** Turns the fork regenerates, if it runs as long as the original did after the fork. */
  turns: number
  /** What the original spent on exactly those turns. */
  costUsd: number
}

/**
 * What forking at `fromTurn` should cost: what this run spent on the turns after it.
 *
 * The fork replays to `fromTurn` and regenerates everything after with the same cast, models and
 * settings, so the original's own spend on that stretch is a better estimate than any cross-run
 * average — it already includes the context those turns carried, which is why a late fork is cheap.
 * Every event's `cost_usd` counts (voice, selection, checks, consultations), as in the run's own total.
 * The automatic summary is billed outside the event log and is not included.
 */
export function forkEstimate(events: SimEvent[], fromTurn: number): ForkEstimate {
  let costUsd = 0
  const turns = new Set<number>()
  for (const e of events) {
    if (e.turn <= fromTurn) continue
    if (e.event_type === 'agent.response') turns.add(e.turn)
    const c = e.payload?.cost_usd
    if (typeof c === 'number') costUsd += c
  }
  return { turns: turns.size, costUsd }
}

export function describeFork(f: ForkEstimate): string {
  if (f.turns === 0) return 'regenerates nothing that this run said; the cost is the new turns only'
  return `about $${f.costUsd.toFixed(f.costUsd < 0.1 ? 3 : 2)} — what this run spent on the ${f.turns} turn${
    f.turns === 1 ? '' : 's'} after it, which the fork generates again (plus its summary)`
}
