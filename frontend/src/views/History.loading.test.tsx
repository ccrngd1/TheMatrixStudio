// SPDX-License-Identifier: Apache-2.0
// How the history list behaves while it is waiting, and when the wait fails.
//
// Written after a real report: "the home page says 'loading' after logging in". The API
// was fine — the deployed logs show `GET /api/runs 200` — but that response took 24.8
// seconds, because the container-image Lambda's init phase overran Lambda's hard 10 s
// limit twice and was retried each time. The view said "Loading…" and nothing else, so
// a working app was indistinguishable from a broken one.
//
// Three defects, one surface:
//   1. two identical requests on mount, each starting its own cold sandbox;
//   2. no indication that a long wait is a cold start rather than a hang;
//   3. a failed load rendered as "No runs yet" — an empty account, not an error.

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { History } from './History'
import type { RunSummary } from '../types'

vi.mock('../api', () => ({ api: { listRuns: vi.fn() } }))
import { api } from '../api'

const listRuns = api.listRuns as ReturnType<typeof vi.fn>

function run(over: Partial<RunSummary> = {}): RunSummary {
  return {
    run_id: 'r1',
    name: 'quiet-harbour',
    description: 'd',
    slug: 'quiet-harbour',
    topic: 't',
    status: 'complete',
    turn_count: 2,
    total_cost_usd: 0.01,
    created_at: Math.floor(Date.now() / 1000),
    completed_at: null,
    last_event_at: Math.floor(Date.now() / 1000),
    parent_run_id: null,
    branch_turn: null,
    ...over,
  } as RunSummary
}

beforeEach(() => vi.clearAllMocks())
afterEach(() => vi.useRealTimers())

// --------------------------------------------------------------------------- //
// One request per page load
// --------------------------------------------------------------------------- //

describe('the initial load', () => {
  it('fetches exactly ONCE on mount', async () => {
    // Two effects both fired on mount — an immediate `load()` and a debounced one 250 ms
    // later with an identical query. Ordinarily just waste; against a cold Lambda each
    // request starts its own sandbox and pays its own ~5.7 s init, and the deployed logs
    // show three concurrent cold starts for one page load.
    listRuns.mockResolvedValue([run()])
    render(<History onOpen={() => {}} onNew={() => {}} />)
    await waitFor(() => expect(screen.getByText('quiet-harbour')).toBeTruthy())

    // Past the 250 ms debounce window, so a duplicate would have landed by now.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 400))
    })
    expect(listRuns).toHaveBeenCalledTimes(1)
  })

  it('does not wait for the debounce before the first fetch', async () => {
    // The fix must not make the first load 250 ms slower — the point is to remove the
    // duplicate, not to delay the one that matters.
    listRuns.mockResolvedValue([])
    render(<History onOpen={() => {}} onNew={() => {}} />)
    expect(listRuns).toHaveBeenCalledTimes(1)
  })

  it('searches the loaded list without a request per keystroke', async () => {
    listRuns.mockResolvedValue([])
    render(<History onOpen={() => {}} onNew={() => {}} />)
    await waitFor(() => expect(listRuns).toHaveBeenCalledTimes(1))
    const input = screen.getByPlaceholderText(/search/i)
    for (const value of ['h', 'ha', 'har', 'harb', 'harbo', 'harbou', 'harbour']) {
      fireEvent.change(input, { target: { value } })
    }
    await new Promise((r) => setTimeout(r, 400))
    expect(listRuns).toHaveBeenCalledTimes(1)
  })

  it('still debounces when a full list sends the search to the server', async () => {
    const full = Array.from({ length: 200 }, (_, i) => ({ run_id: `r${i}`, topic: 't', status: 'complete' }))
    listRuns.mockResolvedValue(full)
    render(<History onOpen={() => {}} onNew={() => {}} />)
    await waitFor(() => expect(listRuns).toHaveBeenCalledTimes(1))
    // Seven keystrokes must not be seven requests.
    const input = screen.getByPlaceholderText(/search/i)
    for (const value of ['h', 'ha', 'har', 'harb', 'harbo', 'harbou', 'harbour']) {
      fireEvent.change(input, { target: { value } })
    }
    await waitFor(() => expect(listRuns).toHaveBeenCalledTimes(2), { timeout: 2000 })
    expect(listRuns).toHaveBeenLastCalledWith('harbour')
  })
})

// --------------------------------------------------------------------------- //
// Saying why the wait is long
// --------------------------------------------------------------------------- //

describe('a slow load', () => {
  it('explains itself once the wait passes the threshold', async () => {
    vi.useFakeTimers()
    listRuns.mockReturnValue(new Promise(() => {})) // never settles
    render(<History onOpen={() => {}} onNew={() => {}} />)

    expect(screen.getByText('Loading…')).toBeTruthy()
    expect(screen.queryByText(/up to 30 seconds/i)).toBeNull()

    // `advanceTimersByTimeAsync` is not act-wrapped, so the `setSlow` it triggers
    // would be a React update outside act.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3500)
    })
    expect(screen.getByText(/up to 30 seconds/i)).toBeTruthy()
  })

  it('says nothing extra for a load that returns promptly', async () => {
    // Otherwise every navigation flashes a warning about cold starts.
    vi.useFakeTimers()
    listRuns.mockResolvedValue([run()])
    render(<History onOpen={() => {}} onNew={() => {}} />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100)
      await vi.advanceTimersByTimeAsync(5000)
    })
    expect(screen.queryByText(/up to 30 seconds/i)).toBeNull()
  })
})

// --------------------------------------------------------------------------- //
// A failed load is not an empty account
// --------------------------------------------------------------------------- //

describe('a failed load', () => {
  it('shows the error rather than "No runs yet"', async () => {
    // The dangerous version of this bug: a 504 from a cold start that overran told the
    // user their entire run history was gone.
    listRuns.mockRejectedValue(new Error('504: Gateway Timeout'))
    render(<History onOpen={() => {}} onNew={() => {}} />)

    await waitFor(() => expect(screen.getByText(/could not load your runs/i)).toBeTruthy())
    expect(screen.getByText(/504/)).toBeTruthy()
    expect(screen.queryByText(/no runs yet/i)).toBeNull()
  })

  it('offers a retry that actually refetches', async () => {
    listRuns.mockRejectedValueOnce(new Error('boom')).mockResolvedValue([run()])
    render(<History onOpen={() => {}} onNew={() => {}} />)
    await waitFor(() => expect(screen.getByText(/could not load your runs/i)).toBeTruthy())

    fireEvent.click(screen.getByRole('button', { name: /try again/i }))
    await waitFor(() => expect(screen.getByText('quiet-harbour')).toBeTruthy())
    expect(screen.queryByText(/could not load your runs/i)).toBeNull()
  })

  it('clears a previous error once a load succeeds', async () => {
    // A stale error banner above a freshly loaded list is its own kind of lying.
    listRuns.mockRejectedValueOnce(new Error('boom')).mockResolvedValue([])
    render(<History onOpen={() => {}} onNew={() => {}} />)
    await waitFor(() => expect(screen.getByText(/could not load your runs/i)).toBeTruthy())

    fireEvent.click(screen.getByRole('button', { name: /try again/i }))
    await waitFor(() => expect(screen.getByText(/no runs yet/i)).toBeTruthy())
    expect(screen.queryByText(/could not load your runs/i)).toBeNull()
  })

  it('distinguishes a genuinely empty account from a failure', async () => {
    listRuns.mockResolvedValue([])
    render(<History onOpen={() => {}} onNew={() => {}} />)
    await waitFor(() => expect(screen.getByText(/no runs yet/i)).toBeTruthy())
    expect(screen.queryByText(/could not load your runs/i)).toBeNull()
  })
})
