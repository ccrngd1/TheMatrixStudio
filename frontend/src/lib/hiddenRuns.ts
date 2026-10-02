// SPDX-License-Identifier: Apache-2.0
// Hiding a run from the Runs list, from either place that offers it: a card on the Runs screen, and the run's
// own ⋯ menu.
//
// One module because a hide has three things to keep in step, and each screen would otherwise keep two of
// them: the server, the cached list the Runs screen comes back to (lib/listCache.ts), and any list request
// already in flight when the hide was sent.
//
// The third is the one that bites. The list is slow to answer (it reads every run's event log for the turn
// counts and costs) and the Runs screen refreshes it on every visit, so a hide sent just after arriving races
// that refresh: the refresh read the run before the hide was written, answers after it, and puts the run
// straight back on screen and in the cache. So a list request takes a stamp as it is sent, and every hide
// sent after that stamp is laid over its answer.
import { api } from '../api'
import type { RunSummary } from '../types'
import { cached, remember } from './listCache'

/** The Runs screen's key for its list in lib/listCache.ts. */
export const RUNS_KEY = 'runs'

let clock = 0
// Per run, the latest hide or show this page sent and when. Kept for the life of the page: an entry only
// ever applies to answers to requests sent BEFORE it, so an old one is inert rather than wrong.
const sent = new Map<string, { hidden: boolean; at: number }>()

/** Taken as a list request is sent, and handed to `withSentHides` with its answer. */
export const listStamp = () => ++clock

/** A list answer, with every hide or show sent after its request was laid over it. */
export function withSentHides(rows: RunSummary[], stamp: number): RunSummary[] {
  if (!sent.size) return rows
  return rows.map((r) => {
    const s = sent.get(r.run_id)
    return s && s.at > stamp && Boolean(r.hidden) !== s.hidden ? { ...r, hidden: s.hidden } : r
  })
}

/** The same rows with one run marked hidden or shown. */
export function markHidden(rows: RunSummary[], runId: string, hidden: boolean): RunSummary[] {
  return rows.map((r) => (r.run_id === runId ? { ...r, hidden } : r))
}

/**
 * Hide a run, or show it again. The cached list changes at once and the caller changes what it has on
 * screen, so the run goes before the server answers. If the server refuses, the cache is put back and this
 * rejects, for the caller to put its own state back and say why.
 */
export async function setRunHidden(runId: string, hidden: boolean): Promise<void> {
  const at = ++clock
  sent.set(runId, { hidden, at })
  patchCache(runId, hidden)
  try {
    await api.setRunHidden(runId, hidden)
  } catch (e) {
    // Back to what it was before THIS request, even if another was sent since: undoing each failed step
    // leaves the cache where the server is, whichever order the answers come back in.
    patchCache(runId, !hidden)
    if (sent.get(runId)?.at === at) sent.delete(runId)
    throw e
  }
}

function patchCache(runId: string, hidden: boolean) {
  const rows = cached<RunSummary[]>(RUNS_KEY)
  if (rows) remember(RUNS_KEY, markHidden(rows, runId, hidden))
}
