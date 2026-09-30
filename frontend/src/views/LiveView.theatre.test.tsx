// SPDX-License-Identifier: Apache-2.0
// The run screen offers the 8-bit theatre. Kept as its own test because the control has already
// been lost once: the run screen was rewritten and the button did not come along, and nothing
// failed. This fails if either the header control or the Run options entry goes missing.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { deriveState, initialState } from '../lib/simState'
import type { SimEvent } from '../types'

const run = { current: {} as Record<string, unknown> }
const events = { current: [] as SimEvent[] }

// Anything the screen asks for that this test does not care about stays pending, rather than
// each call needing a hand-written stub.
vi.mock('../api', () => {
  const known: Record<string, (...a: unknown[]) => Promise<unknown>> = {
    getRun: () => Promise.resolve(run.current),
    getModels: () => Promise.resolve({ default: 'm', models: [] }),
    getSummary: () => Promise.resolve({ run_id: 'r1', generated: null, imported: null, default_instructions: '' }),
    getRunQuotes: () => Promise.resolve({ quotes: {} }),
  }
  return {
    api: new Proxy({}, { get: (_, k: string) => known[k] ?? (() => new Promise(() => {})) }),
    avatarUrl: () => null,
    loadAvatar: () => Promise.resolve(null),
  }
})

vi.mock('../hooks/useRunStream', () => ({
  useRunStream: ({ cast }: { cast: { name: string; persona: string; goals: string[] }[] }) => ({
    state: deriveState(initialState(cast), events.current),
    events: events.current,
    connected: true, engineDone: true, stalled: false, mode: 'live', speedMs: 700, setSpeedMs: () => {},
    cursor: events.current.length, bufferLength: events.current.length, behind: 0,
    pause: () => {}, resume: () => {}, stepForward: () => {}, catchUp: () => {},
  }),
}))

import { LiveView } from './LiveView'

const cast = [{ name: 'Ana Silva', persona: 'Operations lead', goals: [] }]
let seq = 0
const ev = (turn: number, event_type: SimEvent['event_type'], payload: Record<string, unknown> = {}): SimEvent =>
  ({ run_id: 'r1', turn, seq: seq++, event_type, agent_name: null, payload })

function setup(status: 'complete' | 'running') {
  seq = 0
  run.current = { run_id: 'r1', name: 'quiet-harbor', description: null, slug: null, topic: 'A topic', status, cast, config: {}, result: null }
  events.current = [
    ev(0, 'sim.started'),
    ev(1, 'agent.response', { speaker: 'Ana Silva', message: 'We should pilot it.' }),
    ...(status === 'complete' ? [ev(1, 'sim.completed')] : []),
  ]
}

const label = /Replay in the 8-bit theatre/

beforeEach(() => {
  HTMLCanvasElement.prototype.getContext = (() => null) as unknown as HTMLCanvasElement['getContext']
})
afterEach(() => vi.restoreAllMocks())

describe('LiveView: the 8-bit theatre', () => {
  it('offers it in the header of a finished run, and opens it for this run', async () => {
    setup('complete')
    const open = vi.spyOn(window, 'open').mockImplementation(() => null)
    render(<LiveView runId="r1" onBack={() => {}} />)
    const button = await screen.findByRole('button', { name: label })
    await vi.waitFor(() => expect(button).toBeEnabled())
    fireEvent.click(button)
    expect(open).toHaveBeenCalledWith('/theatre.html?run=r1', '_blank')
  })

  it('shows it greyed out while the run is still going', async () => {
    setup('running')
    render(<LiveView runId="r1" onBack={() => {}} />)
    const button = await screen.findByRole('button', { name: label })
    await vi.waitFor(() => expect(button).toBeDisabled())
  })

  it('lists it in Run options too', async () => {
    setup('complete')
    render(<LiveView runId="r1" onBack={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: 'More' }))
    const sheet = await screen.findByRole('dialog')
    expect(within(sheet).getByRole('button', { name: label })).toBeInTheDocument()
  })
})
