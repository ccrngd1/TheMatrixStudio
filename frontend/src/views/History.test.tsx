// SPDX-License-Identifier: Apache-2.0
// The history list's status pill, and specifically its staleness branch.
//
// That branch existed and could never fire: `StatusPill` took `lastEventAt` as an
// optional prop and the call site never passed it, so `stalled` was always false. A run
// orphaned by a restart rendered as a healthy "running" indefinitely — the same shape
// of dead guard as `useRunStream.stalled`, and found the same way, by trying to make it
// work for a new case.
//
// Phase 5 makes it matter more: a run is created `pending` and its first turn flips it
// to `running`, so a run stuck at `pending` is one whose execution never started.

import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { History } from './History'
import type { RunSummary } from '../types'

vi.mock('../api', () => ({ api: { listRuns: vi.fn() } }))
import { api } from '../api'

const NOW = Math.floor(Date.now() / 1000)

function run(over: Partial<RunSummary> = {}): RunSummary {
  return {
    run_id: 'r1',
    name: 'quiet-harbour',
    description: 'd',
    slug: 'quiet-harbour',
    topic: 't',
    status: 'running',
    turn_count: 2,
    total_cost_usd: 0.01,
    created_at: NOW,
    completed_at: null,
    last_event_at: NOW,
    parent_run_id: null,
    branch_turn: null,
    ...over,
  } as RunSummary
}

async function show(r: RunSummary) {
  ;(api.listRuns as ReturnType<typeof vi.fn>).mockResolvedValue([r])
  render(<History onOpen={() => {}} onNew={() => {}} />)
  await waitFor(() => expect(screen.getByText('quiet-harbour')).toBeTruthy())
}

describe('History status pill', () => {
  beforeEach(() => vi.clearAllMocks())

  it('shows a recently-active running run as running', async () => {
    await show(run({ status: 'running', last_event_at: NOW - 5 }))
    expect(screen.getByText('running')).toBeTruthy()
    expect(screen.queryByText('stalled')).toBeNull()
  })

  it('shows a running run with no recent events as stalled', async () => {
    // The branch that could never fire, because the call site passed no timestamp.
    await show(run({ status: 'running', last_event_at: NOW - 600 }))
    expect(screen.getByText('stalled')).toBeTruthy()
    expect(screen.queryByText('running')).toBeNull()
  })

  it('shows a run stuck at pending as stalled', async () => {
    // A run whose execution never started. Under Step Functions this is a real and
    // otherwise silent failure: `start_execution` returning None, a denied
    // StartExecution, or no state machine at all.
    await show(run({ status: 'pending', last_event_at: null, created_at: NOW - 600 }))
    expect(screen.getByText('stalled')).toBeTruthy()
  })

  it('does not flag a freshly created pending run', async () => {
    // The prepare state ingests documents and embeds the corpus before the first turn,
    // which took ~90 s on the measurement corpus. Flagging that would cry wolf on every
    // run with attachments.
    await show(run({ status: 'pending', last_event_at: null, created_at: NOW - 5 }))
    expect(screen.getByText('pending')).toBeTruthy()
    expect(screen.queryByText('stalled')).toBeNull()
  })

  it('falls back to created_at when a run has no events at all', async () => {
    // `last_event_at` is null for a run that never generated, so a check requiring it
    // skips exactly the case worth catching.
    await show(run({ status: 'running', last_event_at: null, created_at: NOW - 600 }))
    expect(screen.getByText('stalled')).toBeTruthy()
  })

  it('never flags a terminal run as stalled', async () => {
    for (const status of ['complete', 'failed', 'stopped', 'capped', 'interrupted']) {
      vi.clearAllMocks()
      ;(api.listRuns as ReturnType<typeof vi.fn>).mockResolvedValue([
        run({ status, last_event_at: NOW - 100_000, created_at: NOW - 100_000 }),
      ])
      const { unmount } = render(<History onOpen={() => {}} onNew={() => {}} />)
      await waitFor(() => expect(screen.getByText(status)).toBeTruthy())
      expect(screen.queryByText('stalled')).toBeNull()
      unmount()
    }
  })
})
