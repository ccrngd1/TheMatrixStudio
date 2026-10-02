// SPDX-License-Identifier: Apache-2.0
// Hiding a run from the Runs list from the run's own ⋯ menu, and the tag that says a run opened by URL is hidden.
// Every name and topic is invented.
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { deriveState, initialState } from '../lib/simState'
import { cached, remember } from '../lib/listCache'
import type { RunSummary, SimEvent } from '../types'

const run = { current: {} as Record<string, unknown> }
const setRunHidden = vi.fn()

// Anything the screen asks for that this test does not care about stays pending.
vi.mock('../api', () => {
  const known: Record<string, (...a: unknown[]) => Promise<unknown>> = {
    getRun: () => Promise.resolve(run.current),
    getModels: () => Promise.resolve({ default: 'm', models: [] }),
    getSummary: () => Promise.resolve({ run_id: 'r1', generated: null, imported: null, default_instructions: '' }),
    getRunQuotes: () => Promise.resolve({ quotes: {} }),
    setRunHidden: (...a: unknown[]) => setRunHidden(...a),
  }
  return {
    api: new Proxy({}, { get: (_, k: string) => known[k] ?? (() => new Promise(() => {})) }),
    avatarUrl: () => null,
    loadAvatar: () => Promise.resolve(null),
  }
})

vi.mock('../hooks/useRunStream', () => ({
  useRunStream: ({ cast }: { cast: { name: string; persona: string; goals: string[] }[] }) => ({
    state: deriveState(initialState(cast), [] as SimEvent[]),
    events: [],
    connected: true, engineDone: true, stalled: false, mode: 'live', speedMs: 700, setSpeedMs: () => {},
    cursor: 0, bufferLength: 0, behind: 0,
    pause: () => {}, resume: () => {}, stepForward: () => {}, catchUp: () => {},
  }),
}))

import { LiveView } from './LiveView'

function setup(hidden: boolean) {
  run.current = {
    run_id: 'r1', name: 'amber-lantern', description: null, slug: null, topic: 'A topic', status: 'complete',
    cast: [{ name: 'Ana Silva', persona: 'Operations lead', goals: [] }], config: {}, result: null, hidden,
  }
  // The Runs screen's cached list, as backing out of this run would find it.
  remember<Partial<RunSummary>[]>('runs', [{ run_id: 'r1', name: 'amber-lantern', hidden }, { run_id: 'r2', name: 'birch-signal' }])
}

const cachedHidden = () => cached<RunSummary[]>('runs')?.find((r) => r.run_id === 'r1')?.hidden

async function openMenu() {
  fireEvent.click(await screen.findByRole('button', { name: 'More' }))
  return within(await screen.findByRole('dialog'))
}

function deferred() {
  let resolve!: (v: unknown) => void
  let reject!: (e: unknown) => void
  const promise = new Promise((res, rej) => ((resolve = res), (reject = rej)))
  return { promise, resolve, reject }
}

beforeEach(() => {
  setRunHidden.mockReset()
  HTMLCanvasElement.prototype.getContext = (() => null) as unknown as HTMLCanvasElement['getContext']
})
afterEach(() => vi.restoreAllMocks())

describe('LiveView: hiding the run from the Runs list', () => {
  it('hides it from Run options at once, tags the header, and updates the cached list', async () => {
    setup(false)
    const answer = deferred()
    setRunHidden.mockReturnValue(answer.promise)
    // Opened by codename: the request still names the run by its id.
    render(<LiveView runId="amber-lantern" onBack={() => {}} />)
    expect(screen.queryByText('Hidden')).not.toBeInTheDocument()

    const menu = await openMenu()
    fireEvent.click(menu.getByRole('button', { name: 'Hide from the Runs list' }))
    expect(setRunHidden).toHaveBeenCalledWith('r1', true)
    // Before the server has answered.
    expect(screen.getByText('Hidden')).toBeInTheDocument()
    expect(cachedHidden()).toBe(true)
    await act(async () => answer.resolve({ run_id: 'r1', hidden: true }))

    expect((await openMenu()).getByRole('button', { name: 'Show in the Runs list' })).toBeInTheDocument()
  })

  it('shows a hidden run again', async () => {
    setup(true)
    setRunHidden.mockResolvedValue({ run_id: 'r1', hidden: false })
    render(<LiveView runId="r1" onBack={() => {}} />)
    expect(await screen.findByText('Hidden')).toBeInTheDocument()
    fireEvent.click((await openMenu()).getByRole('button', { name: 'Show in the Runs list' }))
    expect(setRunHidden).toHaveBeenCalledWith('r1', false)
    expect(screen.queryByText('Hidden')).not.toBeInTheDocument()
    expect(cachedHidden()).toBe(false)
  })

  it('puts it back and says why when the server refuses', async () => {
    setup(false)
    const answer = deferred()
    setRunHidden.mockReturnValue(answer.promise)
    render(<LiveView runId="r1" onBack={() => {}} />)
    fireEvent.click((await openMenu()).getByRole('button', { name: 'Hide from the Runs list' }))
    expect(screen.getByText('Hidden')).toBeInTheDocument()

    await act(async () => answer.reject(new Error('503: Service Unavailable')))
    expect(screen.queryByText('Hidden')).not.toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('Hiding from the Runs list failed: 503: Service Unavailable')
    expect(cachedHidden()).toBe(false)
  })
})
