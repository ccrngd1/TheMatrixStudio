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
// `sim.stopped` and `sim.capped` were once missing from this set — see runStatus.ts
// for what that cost. It lives there now so there is one copy.
import { TERMINAL_EVENTS } from '../lib/runStatus'
import type { Persona, SimEvent } from '../types'
import { deriveState, initialState } from '../lib/simState'

export type PlaybackMode = 'live' | 'paused'

// How often to poll for new events while a run is live.
//
// Polling is how the deployed system streams at all: under Step Functions each turn
// runs in a worker Lambda, so there is no process holding a WebSocket to push from
// (the in-memory broker lives in the API process). The socket is kept for local
// single-process use, where it is genuinely sub-second.
//
// 3 s against a measured 6-13 s per turn means a turn is visible within roughly half
// its own duration. Polling and the socket run TOGETHER rather than one falling back
// to the other: `seenSeqs` already dedupes by seq, so double delivery is free, and
// "detect the socket is not really working" is a harder problem than just asking.
const POLL_MS = 3000

interface Options {
  runId: string | null
  cast: Persona[]
  // When true (history/replay), we do not need a live socket if the run is done;
  // but we still open one — the backend replays then closes, which is harmless.
  autoConnect?: boolean
  // Bump to force a full reset + WebSocket reconnect (e.g. after resuming an
  // interrupted run, whose prior socket already closed on the terminal event).
  reloadKey?: number
}

export function useRunStream({ runId, cast, autoConnect = true, reloadKey = 0 }: Options) {
  // The full ordered event buffer (everything received — the engine's truth).
  const [buffer, setBuffer] = useState<SimEvent[]>([])
  // How many buffered events are currently *revealed* to the view.
  const [cursor, setCursor] = useState(0)
  const [mode, setMode] = useState<PlaybackMode>('live')
  const [speedMs, setSpeedMs] = useState(700) // reveal pace during auto-play
  const [connected, setConnected] = useState(false)
  const [engineDone, setEngineDone] = useState(false)
  // Whether we've done the one-time "jump to furthest point" on load. Until
  // primed, we don't gradually reveal — we wait for the initial backlog so we
  // can dump it all at once, then stream only new events.
  const [primed, setPrimed] = useState(false)
  // Wall-clock (ms) when the most recent event was received; used to detect a
  // run that is still "running" server-side but has gone quiet (stalled).
  const [lastEventAt, setLastEventAt] = useState<number | null>(null)
  // A ticking clock so the stalled flag re-evaluates without a new event.
  const [nowMs, setNowMs] = useState<number>(Date.now())

  const seenSeqs = useRef<Set<number>>(new Set())
  // Highest seq seen, so a poll asks only for what is new. Tracked as a ref
  // rather than derived from `buffer` so the polling effect does not restart on
  // every event, which would reset its timer and stall the poll indefinitely.
  const maxSeq = useRef<number>(-1)
  const wsRef = useRef<WebSocket | null>(null)

  // Reset when the run changes OR a reload is requested (resume reconnect).
  useEffect(() => {
    setBuffer([])
    setCursor(0)
    setMode('live')
    setEngineDone(false)
    setLastEventAt(null)
    setPrimed(false)
    seenSeqs.current = new Set()
    maxSeq.current = -1
  }, [runId, reloadKey])

  const pushEvents = useCallback((incoming: SimEvent[]) => {
    // Dedupe FIRST, outside the state updater.
    //
    // This used to set a local `added` flag inside the `setBuffer(prev => ...)`
    // callback and read it on the next line. React runs a functional updater during
    // the re-render, not at the call site, so `added` was always still `false` when
    // it was checked — `lastEventAt` was therefore never set, and `stalled` (which
    // requires it to be non-null) could never become true. The stall warning had
    // been dead code since it was written; found while making it reachable without a
    // WebSocket.
    //
    // The ref is what makes this correct: `seenSeqs` is mutable and synchronous, so
    // the filter is accurate before any state has changed.
    const fresh = incoming.filter((e) => !seenSeqs.current.has(e.seq))
    if (!fresh.length) return
    for (const e of fresh) {
      seenSeqs.current.add(e.seq)
      if (e.seq > maxSeq.current) maxSeq.current = e.seq
      if (TERMINAL_EVENTS.has(e.event_type)) setEngineDone(true)
    }
    setBuffer((prev) => [...prev, ...fresh].sort((a, b) => a.seq - b.seq))
    setLastEventAt(Date.now())
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
  }, [runId, autoConnect, reloadKey, pushEvents])

  // On load, jump straight to the FURTHEST already-generated point: fetch the
  // current backlog once and reveal all of it instantly (cursor -> its length),
  // then let only newly-arriving live events stream in gradually. For a finished
  // run this reveals the whole conversation at once; for a run 10-in-and-going
  // it dumps the 10 immediately, then streams 11, 12, ... as they arrive.
  useEffect(() => {
    if (!runId) return
    let alive = true
    api
      .getEvents(runId)
      .then((evts) => {
        if (!alive) return
        pushEvents(evts)
        // The backlog is the lowest-seq `evts.length` events (seq is monotonic;
        // any live event already received has a higher seq and stays unrevealed).
        setCursor((c) => Math.max(c, evts.length))
        setPrimed(true)
      })
      .catch(() => {
        // Even if the prime fetch fails, unblock gradual reveal so the WS path
        // still renders.
        if (alive) setPrimed(true)
      })
    return () => {
      alive = false
    }
  }, [runId, reloadKey, pushEvents])

  // Poll for new events while the run is live. This is the deployed system's only
  // delivery path — see POLL_MS above for why it runs alongside the socket rather
  // than as its fallback.
  //
  // Stops as soon as a terminal event lands, which is what makes the missing
  // `sim.stopped`/`sim.capped` entries above a real bug and not a tidy-up: without
  // them a stopped run is polled every three seconds for as long as the tab is open.
  useEffect(() => {
    if (!runId || !autoConnect || engineDone) return
    let alive = true
    let timer: ReturnType<typeof setTimeout>

    const tick = async () => {
      try {
        const evts = await api.getEvents(runId, maxSeq.current)
        if (!alive) return
        if (evts.length) pushEvents(evts)
      } catch {
        // A failed poll is not worth surfacing: the next one is three seconds away,
        // and a transient 5xx during a deploy would otherwise show the user an error
        // for a run that is fine.
      }
      if (alive) timer = setTimeout(tick, POLL_MS)
    }

    timer = setTimeout(tick, POLL_MS)
    return () => {
      alive = false
      clearTimeout(timer)
    }
  }, [runId, autoConnect, engineDone, reloadKey, pushEvents])

  // Auto-play: while live and not caught up, advance the cursor on a timer.
  // Gated on `primed` so we don't drip-feed the initial backlog before the
  // one-time jump-to-furthest has run.
  useEffect(() => {
    if (!primed) return
    if (mode !== 'live') return
    if (cursor >= buffer.length) return
    const id = setTimeout(() => setCursor((c) => Math.min(c + 1, buffer.length)), speedMs)
    return () => clearTimeout(id)
  }, [primed, mode, cursor, buffer.length, speedMs])

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
  // `connected` is deliberately NOT part of this any more. It meant "the WebSocket is
  // open", and under Step Functions there is no socket to open — so requiring it made
  // the stall warning unreachable on the deployed system, which is the only place a
  // run can actually be orphaned. What matters is that the run is not finished and has
  // gone quiet, and that is true whichever way events were arriving.
  const stalled =
    !engineDone &&
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
