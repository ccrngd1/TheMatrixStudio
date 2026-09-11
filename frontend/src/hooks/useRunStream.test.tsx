// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { useRunStream } from './useRunStream'
import type { Persona, SimEvent } from '../types'

// Mock the REST client used to prime the backlog.
vi.mock('../api', () => ({
  api: { getEvents: vi.fn() },
  streamUrl: () => 'ws://test/stream',
}))
import { api } from '../api'

// A WebSocket stub that never opens/receives — so the test exercises only the
// prime-from-REST "jump to furthest" path deterministically.
class FakeWS {
  onopen: (() => void) | null = null
  onmessage: ((m: { data: string }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  close() {}
}
// @ts-expect-error test stub
global.WebSocket = FakeWS

const cast: Persona[] = [
  { name: 'Ada', persona: 'ethicist', goals: [] },
  { name: 'Ben', persona: 'engineer', goals: [] },
]

function ev(seq: number, type: string, speaker?: string, message?: string): SimEvent {
  return {
    run_id: 'r1', turn: seq, seq, event_type: type,
    agent_name: speaker ?? null,
    payload: speaker ? { speaker, message } : {},
  } as SimEvent
}

// A 3-response backlog (interleaved speaker.selected + agent.response).
const backlog: SimEvent[] = [
  ev(0, 'sim.started'),
  ev(1, 'speaker.selected', 'Ada'),
  ev(2, 'agent.response', 'Ada', 'one'),
  ev(3, 'speaker.selected', 'Ben'),
  ev(4, 'agent.response', 'Ben', 'two'),
  ev(5, 'speaker.selected', 'Ada'),
  ev(6, 'agent.response', 'Ada', 'three'),
]

describe('useRunStream jump-to-furthest', () => {
  beforeEach(() => vi.clearAllMocks())

  it('reveals the entire existing backlog immediately on load (no drip-feed)', async () => {
    ;(api.getEvents as ReturnType<typeof vi.fn>).mockResolvedValue(backlog)
    const { result } = renderHook(() => useRunStream({ runId: 'r1', cast }))

    // Once primed, the cursor jumps to the full backlog length at once — the
    // three responses are all revealed without waiting on the reveal timer.
    await waitFor(() => expect(result.current.cursor).toBe(backlog.length))
    expect(result.current.state.feed.map((m) => m.content)).toEqual(['one', 'two', 'three'])
    expect(result.current.behind).toBe(0)
  })
})

// --------------------------------------------------------------------------- //
// Phase 5: polling is the deployed system's only delivery path. Under Step
// Functions each turn runs in a worker Lambda, so there is no process holding a
// WebSocket to push from — the in-memory broker lives in the API process.
// --------------------------------------------------------------------------- //

describe('useRunStream polling', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.useRealTimers()
  })

  it('keeps polling for new events while the run is live', async () => {
    const calls: number[] = []
    ;(api.getEvents as ReturnType<typeof vi.fn>).mockImplementation(
      (_ref: string, afterSeq = -1) => {
        calls.push(afterSeq)
        // First call is the prime (afterSeq -1); later polls ask for what is new and
        // eventually a turn arrives.
        if (afterSeq < 0) return Promise.resolve([ev(0, 'sim.started')])
        if (calls.length >= 3) {
          return Promise.resolve([ev(1, 'agent.response', 'Ada', 'polled in')])
        }
        return Promise.resolve([])
      },
    )
    vi.useFakeTimers()
    const { result } = renderHook(() => useRunStream({ runId: 'r1', cast }))

    // Drive several poll intervals.
    for (let i = 0; i < 5; i++) {
      await vi.advanceTimersByTimeAsync(3100)
    }
    vi.useRealTimers()

    // It asked more than once, and it asked for only what was new.
    expect(calls.length).toBeGreaterThan(2)
    expect(calls.some((a) => a >= 0)).toBe(true)
    await waitFor(() =>
      expect(result.current.state.feed.map((m) => m.content)).toContain('polled in'),
    )
  })

  it('stops polling once the run reaches a terminal event', async () => {
    ;(api.getEvents as ReturnType<typeof vi.fn>).mockResolvedValue([
      ev(0, 'sim.started'),
      ev(1, 'agent.response', 'Ada', 'done'),
      ev(2, 'sim.completed'),
    ])
    vi.useFakeTimers()
    const { result } = renderHook(() => useRunStream({ runId: 'r1', cast }))
    await vi.advanceTimersByTimeAsync(3100)
    const afterFirst = (api.getEvents as ReturnType<typeof vi.fn>).mock.calls.length
    for (let i = 0; i < 5; i++) await vi.advanceTimersByTimeAsync(3100)
    vi.useRealTimers()

    expect(result.current.engineDone).toBe(true)
    expect((api.getEvents as ReturnType<typeof vi.fn>).mock.calls.length).toBe(afterFirst)
  })

  it.each(['sim.stopped', 'sim.capped'])(
    'treats %s as terminal so it does not poll for ever',
    async (terminal) => {
      // These two were MISSING from the terminal list. Each omission showed a
      // finished run as live AND polled it every three seconds for as long as the
      // tab stayed open.
      ;(api.getEvents as ReturnType<typeof vi.fn>).mockResolvedValue([
        ev(0, 'sim.started'),
        ev(1, 'agent.response', 'Ada', 'cut short'),
        ev(2, terminal),
      ])
      const { result } = renderHook(() => useRunStream({ runId: 'r1', cast }))
      await waitFor(() => expect(result.current.engineDone).toBe(true))
    },
  )

  it('flags a stalled run even with no WebSocket connection', async () => {
    // `stalled` used to require `connected`, i.e. an open socket. Under Step
    // Functions there is none, so the warning was unreachable on the only
    // deployment where a run can actually be orphaned.
    //
    // Fake timers are installed BEFORE the hook mounts, which matters: the 5 s clock
    // tick is a `setInterval` created during mount, so installing them afterwards
    // leaves that interval on the real clock and no amount of advancing fires it.
    // The first version of this test did exactly that and read a permanently false
    // `stalled`, which looked like a product bug.
    vi.useFakeTimers()
    ;(api.getEvents as ReturnType<typeof vi.fn>).mockResolvedValue([
      ev(0, 'sim.started'),
      ev(1, 'agent.response', 'Ada', 'then silence'),
    ])
    const { result } = renderHook(() => useRunStream({ runId: 'r1', cast }))
    // Let the prime fetch resolve so `lastEventAt` is set.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10)
    })
    expect(result.current.connected).toBe(false)
    expect(result.current.stalled).toBe(false)

    // Past the 120 s threshold. Wrapped in `act` because the tick calls setState:
    // outside it React does not flush and `result.current` stays on the earlier
    // render.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(130_000)
    })
    expect(result.current.stalled).toBe(true)
    vi.useRealTimers()
  })
})
