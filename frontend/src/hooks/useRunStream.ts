// SPDX-License-Identifier: Apache-2.0
// useRunStream — the buffered live/replay stream + UI-only playback engine.
//
// HARD RULE (spec §5a): the engine always runs full-speed to completion. This
// hook receives every event over the WebSocket and buffers it. The viewer's
// controls (pause/resume/step/reveal-speed) only move a client-side *reveal
// cursor* over that buffer — they NEVER send anything to the server and NEVER
// pause, slow, or gate generation. "Catch up" simply jumps the cursor to the
// buffer head. This is identical to how a completed run is replayed.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, streamUrl } from '../api'
import type { Persona, SimEvent } from '../types'
import { deriveState, initialState } from '../lib/simState'

export type PlaybackMode = 'live' | 'paused'

interface Options {
  runId: string | null
  cast: Persona[]
  // When true (history/replay), we do not need a live socket if the run is done;
  // but we still open one — the backend replays then closes, which is harmless.
  autoConnect?: boolean
}

export function useRunStream({ runId, cast, autoConnect = true }: Options) {
  // The full ordered event buffer (everything received — the engine's truth).
  const [buffer, setBuffer] = useState<SimEvent[]>([])
  // How many buffered events are currently *revealed* to the view.
  const [cursor, setCursor] = useState(0)
  const [mode, setMode] = useState<PlaybackMode>('live')
  const [speedMs, setSpeedMs] = useState(700) // reveal pace during auto-play
  const [connected, setConnected] = useState(false)
  const [engineDone, setEngineDone] = useState(false)
  // Wall-clock (ms) when the most recent event was received; used to detect a
  // run that is still "running" server-side but has gone quiet (stalled).
  const [lastEventAt, setLastEventAt] = useState<number | null>(null)
  // A ticking clock so the stalled flag re-evaluates without a new event.
  const [nowMs, setNowMs] = useState<number>(Date.now())

  const seenSeqs = useRef<Set<number>>(new Set())
  const wsRef = useRef<WebSocket | null>(null)

  // Reset when the run changes.
  useEffect(() => {
    setBuffer([])
    setCursor(0)
    setMode('live')
    setEngineDone(false)
    setLastEventAt(null)
    seenSeqs.current = new Set()
  }, [runId])

  const pushEvents = useCallback((incoming: SimEvent[]) => {
    let added = false
    setBuffer((prev) => {
      const next = [...prev]
      for (const e of incoming) {
        if (seenSeqs.current.has(e.seq)) continue
        seenSeqs.current.add(e.seq)
        next.push(e)
        added = true
        if (
          e.event_type === 'sim.completed' ||
          e.event_type === 'sim.failed' ||
          e.event_type === 'sim.interrupted'
        ) {
          setEngineDone(true)
        }
      }
      next.sort((a, b) => a.seq - b.seq)
      return next
    })
    if (added) setLastEventAt(Date.now())
  }, [])

  // Open the WebSocket. The backend replays persisted events on connect, then
  // streams live ones — so this single channel covers both live and replay.
  useEffect(() => {
    if (!runId || !autoConnect) return
    let closed = false
    const ws = new WebSocket(streamUrl(runId))
    wsRef.current = ws

    ws.onopen = () => !closed && setConnected(true)
    ws.onmessage = (msg) => {
      try {
        const evt = JSON.parse(msg.data) as SimEvent
        if (evt.event_type === 'error') return
        pushEvents([evt])
      } catch {
        /* ignore malformed frame */
      }
    }
    ws.onclose = () => !closed && setConnected(false)
    ws.onerror = () => {
      // Fall back to a one-shot REST replay so a completed run still renders
      // even if the socket fails.
      api.getEvents(runId).then(pushEvents).catch(() => undefined)
    }

    return () => {
      closed = true
      ws.close()
      wsRef.current = null
    }
  }, [runId, autoConnect, pushEvents])

  // Auto-play: while live and not caught up, advance the cursor on a timer.
  useEffect(() => {
    if (mode !== 'live') return
    if (cursor >= buffer.length) return
    const id = setTimeout(() => setCursor((c) => Math.min(c + 1, buffer.length)), speedMs)
    return () => clearTimeout(id)
  }, [mode, cursor, buffer.length, speedMs])

  // Derived view state from the revealed slice only.
  const revealed = useMemo(() => buffer.slice(0, cursor), [buffer, cursor])
  const state = useMemo(
    () => deriveState(initialState(cast), revealed),
    [cast, revealed],
  )

  // ----- Playback controls (client-only; never touch the engine) ----- //
  const pause = useCallback(() => setMode('paused'), [])
  const resume = useCallback(() => setMode('live'), [])
  const stepForward = useCallback(() => {
    setMode('paused')
    setCursor((c) => Math.min(c + 1, buffer.length))
  }, [buffer.length])
  const catchUp = useCallback(() => {
    setCursor(buffer.length)
    setMode('live')
  }, [buffer.length])

  const behind = buffer.length - cursor // buffered-but-unrevealed events

  // Stalled detection (item 2): the socket is connected and the engine has NOT
  // reported a terminal event, yet no new event has arrived for a while. A
  // healthy live run streams events steadily; prolonged silence means the run
  // is orphaned/stalled. Only meaningful once at least one event has landed.
  useEffect(() => {
    if (engineDone) return
    const id = setInterval(() => setNowMs(Date.now()), 5000)
    return () => clearInterval(id)
  }, [engineDone])
  const STALL_MS = 120_000
  const stalled =
    !engineDone &&
    connected &&
    lastEventAt != null &&
    nowMs - lastEventAt > STALL_MS

  return {
    state,
    connected,
    engineDone,
    stalled,
    mode,
    speedMs,
    setSpeedMs,
    cursor,
    bufferLength: buffer.length,
    behind,
    pause,
    resume,
    stepForward,
    catchUp,
  }
}
