// SPDX-License-Identifier: Apache-2.0
// One place that knows what a run's status means.
//
// These lists were inline in three files and the duplication has already cost two real
// bugs, both of the same shape — one copy of the knowledge missing an entry the others
// had:
//
//   - `useRunStream`'s terminal-event set omitted `sim.stopped` and `sim.capped`, so a
//     stopped run displayed as live and (once polling existed) would have been polled
//     for ever.
//   - `LiveView`'s stoppable check tested `status === 'running'` only, so the Stop
//     button was hidden for the seconds-to-minutes a run spends at `pending` while the
//     state machine ingests documents and embeds the corpus.
//
// They must also agree with the SERVER: `orchestration.TERMINAL_STATUSES` and
// `storage.dynamo.TERMINAL_EVENT_TYPES`. One side deciding a run is finished while the
// other does not is exactly a stream that never ends, or a button that never appears.

/** Statuses from which the engine will produce no further turns. */
export const TERMINAL_STATUSES = [
  'complete',
  'failed',
  'stopped',
  'capped',
  'interrupted',
] as const

/**
 * Statuses meaning "this run is expected to produce more turns".
 *
 * `pending` belongs here. A run is created `pending` and its FIRST TURN flips it to
 * `running`, so the gap is real work — the machine's prepare state ingests documents
 * and embeds them first, measured at ~90 s for a 666-chunk corpus.
 */
export const LIVE_STATUSES = ['pending', 'running'] as const

/** Statuses a run can be resumed from. Mirrors `branching.RESUMABLE_STATUSES`. */
export const RESUMABLE_STATUSES = ['interrupted', 'failed', 'stopped'] as const

/** Events that tell a reader the engine will send nothing more. */
export const TERMINAL_EVENTS = new Set([
  'sim.completed',
  'sim.failed',
  'sim.interrupted',
  'sim.stopped',
  'sim.capped',
])

export function isTerminal(status?: string | null): boolean {
  return TERMINAL_STATUSES.includes((status ?? '') as never)
}

export function isLive(status?: string | null): boolean {
  return LIVE_STATUSES.includes((status ?? '') as never)
}

export function isResumable(status?: string | null): boolean {
  return RESUMABLE_STATUSES.includes((status ?? '') as never)
}

/**
 * Whether a run that claims to be live has actually gone quiet.
 *
 * `sinceSec` falls back from the last event to the run's creation, and that fallback is
 * load-bearing rather than defensive: a run whose execution never started has **no
 * events**, so a check requiring `lastEventAt` skips precisely the case worth catching.
 * A run stuck at `pending` is one whose `StartExecution` failed, was denied, or found no
 * state machine — otherwise indistinguishable from a run about to begin.
 */
export function isStalled(
  status: string | null | undefined,
  lastEventAt: number | null | undefined,
  createdAt: number | null | undefined,
  stallSeconds: number,
  nowSec: number = Date.now() / 1000,
): boolean {
  if (!isLive(status)) return false
  const since = lastEventAt ?? createdAt
  if (since == null) return false
  return nowSec - since > stallSeconds
}
