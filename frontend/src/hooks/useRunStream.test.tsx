// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
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
