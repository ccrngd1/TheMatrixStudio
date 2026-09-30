// SPDX-License-Identifier: Apache-2.0
// The 8-bit theatre: a finished run replayed as personas around a conference table, its
// transcript delivered one page at a time through a dialogue box.
//
// Replay only. It reads the run's stored events once and never opens the live stream, so it
// cannot show a conversation still in progress; a run that is still going gets a notice
// instead of a partial play. The launch button is disabled for such runs too, but a deep link
// or a reload can still arrive here, so the page checks for itself.

import { useCallback, useEffect, useLayoutEffect, useMemo, useReducer, useRef, useState } from 'react'
import { api, loadAvatar } from '../api'
import { deriveState, initialState } from '../lib/simState'
import { isLive, isTerminal } from '../lib/runStatus'
import type { RunDetail, SimEvent } from '../types'
import { SME_PREFIX, computeBlocking, visitorsIn } from './blocking'
import { Stage } from './Stage'
import {
  SPRITE_IDS, TYPE_CPS, assignSprites, boardAt, buildScript, holdMs, seatLayout, spriteUrl, type Beat,
} from './script'
import type { Mark, Performer } from './stage'
import type { VisitorSprite } from './Stage'

interface Props {
  runRef: string
  /** Advance to the next page on its own once one is typed and held. Off in tests. */
  autoAdvance?: boolean
}

type Loaded =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'running'; detail: RunDetail }
  | {
      kind: 'ready'; detail: RunDetail; beats: Beat[]; performers: Performer[]; sprites: Record<string, string>
      /** Each persona's generated avatar URL, where the run generated one. */
      portraits: Record<string, string>
      /** The raw log, for what the whiteboard shows at each line. */
      events: SimEvent[]
    }

const SPEEDS = [1, 2, 4] as const

type PlayAction =
  | { type: 'go'; to: number }
  | { type: 'step'; by: number }
  | { type: 'type'; add: number }
  | { type: 'press' }

/**
 * The word on a visitor's name tag. A consultant's title ends in its noun ("Employment lawyer" →
 * LAWYER), where a first word would read "EMPLOYMENT"; a message is known by who sent it
 * ("Customer email" → CUSTOMER).
 */
function tagFor(key: string, label: string): string {
  const words = label.split(/\s+/).filter(Boolean)
  if (!words.length) return label
  return key.startsWith(SME_PREFIX) ? words[words.length - 1] : words[0]
}

const reducedMotion = () =>
  typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

export function Theatre({ runRef, autoAdvance = true }: Props) {
  const [loaded, setLoaded] = useState<Loaded>({ kind: 'loading' })

  useEffect(() => {
    let cancelled = false
    Promise.all([api.getRun(runRef), api.getEvents(runRef)])
      .then(([detail, events]) => {
        if (cancelled) return
        const state = deriveState(initialState(detail.cast ?? []), events)
        if (isLive(detail.status) && !isTerminal(state.status)) {
          setLoaded({ kind: 'running', detail })
          return
        }
        const names = state.order
        const sprites = assignSprites(names)
        const seats = seatLayout(names.length)
        setLoaded({
          kind: 'ready',
          detail,
          beats: buildScript(state.feed, { cast: names, research: detail.research }),
          sprites,
          events,
          portraits: Object.fromEntries(
            names.flatMap((n) => (state.agents[n]?.portraitUrl ? [[n, state.agents[n].portraitUrl as string]] : [])),
          ),
          performers: names.map((name, i) => ({ name, sprite: sprites[name], seat: seats[i] })),
        })
      })
      .catch((e: unknown) => {
        if (!cancelled) setLoaded({ kind: 'error', message: e instanceof Error ? e.message : String(e) })
      })
    return () => {
      cancelled = true
    }
  }, [runRef])

  useEffect(() => {
    const name = loaded.kind === 'ready' || loaded.kind === 'running' ? loaded.detail.name ?? runRef : runRef
    document.title = `${name} · 8-bit theatre`
  }, [loaded, runRef])

  if (loaded.kind === 'loading') return <Shell><p className="th-note">Loading the transcript…</p></Shell>
  if (loaded.kind === 'error') {
    return <Shell><p className="th-note th-error">Could not load this run: {loaded.message}</p></Shell>
  }
  if (loaded.kind === 'running') {
    return (
      <Shell title={loaded.detail.name ?? runRef}>
        <p className="th-note">
          This run is still in progress. The theatre replays a finished transcript, so it opens once
          the run ends.
        </p>
      </Shell>
    )
  }
  return <Player {...loaded} runRef={runRef} autoAdvance={autoAdvance} />
}

function Shell({ title, width, children }: { title?: string; width?: number; children: React.ReactNode }) {
  return (
    <div className="th-root" style={width ? ({ '--th-w': `${width}px` } as React.CSSProperties) : undefined}>
      <header className="th-top">
        <span className="th-brand">8-BIT THEATRE</span>
        {title && <span className="th-title">{title}</span>}
      </header>
      {children}
    </div>
  )
}

function Player({ detail, beats, performers, sprites, events, portraits, runRef, autoAdvance }: Extract<Loaded, { kind: 'ready' }> & {
  runRef: string; autoAdvance: boolean
}) {
  const motion = useMemo(() => !reducedMotion(), [])

  // A branch inherits every line up to its fork, so playing from line 1 would replay the parent
  // before reaching anything new. It opens at the fork instead; the scrubber still goes back over
  // what was inherited.
  const fork = useMemo(() => {
    const parent = detail.lineage?.parent
    const at = parent?.branch_turn
    if (!parent || at == null) return null
    const from = beats.findIndex((b) => b.kind !== 'prologue' && (b.turn > at || (b.outsider && b.turn >= at)))
    if (from <= 0) return null
    return { parent, at, from, inherited: beats[from].line }
  }, [detail.lineage, beats])

  const [started, setStarted] = useState(false)
  // Position and typing progress change together, and every move is relative to the CURRENT
  // position: a reducer, not two useStates read from a closure. With closures, five quick
  // presses of "next" all read the same line and moved one line in total.
  const [{ idx, typed }, dispatch] = useReducer(
    (st: { idx: number; typed: number }, a: PlayAction) => {
      const to = (i: number) => {
        const next = Math.max(0, Math.min(beats.length, i))
        return { idx: next, typed: motion ? 0 : (beats[next]?.text.length ?? 0) }
      }
      const len = beats[st.idx]?.text.length ?? 0
      switch (a.type) {
        case 'go': return to(a.to)
        case 'step': return to(st.idx + a.by)
        case 'type': return { ...st, typed: Math.min(len, st.typed + a.add) }
        // One press does one thing: finish the page if it is still typing, otherwise turn it.
        case 'press': return st.typed < len ? { ...st, typed: len } : to(st.idx + 1)
      }
    },
    { idx: 0, typed: 0 },
  )
  const [playing, setPlaying] = useState(true)
  const [speed, setSpeed] = useState<(typeof SPEEDS)[number]>(1)
  const [stageW, setStageW] = useState<number | undefined>(undefined)
  const beat: Beat | undefined = beats[idx]
  const full = beat?.text.length ?? 0
  const done = started && idx >= beats.length
  const pageComplete = typed >= full

  const goTo = useCallback((to: number) => dispatch({ type: 'go', to }), [])
  const step = useCallback((by: number) => dispatch({ type: 'step', by }), [])

  const seats = useMemo(() => Object.fromEntries(performers.map((p) => [p.name, p.seat])), [performers])
  const blocking = useMemo(() => computeBlocking(beats, started ? idx : -1, seats), [beats, started, idx, seats])
  const blockingRef = useRef(blocking)
  blockingRef.current = blocking

  // The playback clock: milliseconds of the current line at 1×, so walks pause with playback and
  // run faster at 2×. A ref, not state, because the stage reads it every frame.
  const clock = useRef({ base: 0, since: 0, running: false, speed: 1 })
  const elapsed = useCallback(() => {
    const c = clock.current
    return c.base + (c.running ? (performance.now() - c.since) * c.speed : 0)
  }, [])
  // Layout effects so the clock is right before the first frame of a new line is painted:
  // otherwise that frame would draw the new line's walk at the old line's time.
  useLayoutEffect(() => {
    clock.current.base = 0
    clock.current.since = performance.now()
  }, [idx, started])
  useLayoutEffect(() => {
    const c = clock.current
    const run = started && playing && !done
    if (run && !c.running) {
      c.since = performance.now()
      c.running = true
    } else if (!run && c.running) {
      c.base = elapsed()
      c.running = false
    }
  }, [started, playing, done, elapsed])
  useLayoutEffect(() => {
    const c = clock.current
    if (c.running) {
      c.base = elapsed()
      c.since = performance.now()
    }
    c.speed = speed
  }, [speed, elapsed])
  /** Skip to where everyone has arrived: what a press does before it finishes the page. */
  const settle = useCallback(() => {
    const c = clock.current
    c.base = Math.max(elapsed(), blockingRef.current.settleMs)
    c.since = performance.now()
  }, [elapsed])

  // Typing: advance by elapsed time rather than a fixed step, so 4× does not mean 4× the renders.
  const last = useRef(0)
  useEffect(() => {
    if (!started || !playing || done || pageComplete) return
    last.current = performance.now()
    const id = setInterval(() => {
      const now = performance.now()
      // Nothing types until whoever this line waits for has arrived.
      if (elapsed() < blockingRef.current.preMs) {
        last.current = now
        return
      }
      const add = Math.max(1, Math.round(((now - last.current) / 1000) * TYPE_CPS * speed))
      last.current = now
      dispatch({ type: 'type', add })
    }, 30)
    return () => clearInterval(id)
  }, [started, playing, done, pageComplete, full, speed, elapsed])

  // Hold a finished page long enough to read, then move on.
  useEffect(() => {
    if (!autoAdvance || !started || !playing || done || !pageComplete || !beat) return
    // Long enough to read, and never before a walk in progress has finished: the next line's
    // blocking knows nothing of this one's, so a walker cut off here would jump to their seat.
    const remaining = Math.max(0, blocking.settleMs - elapsed())
    const id = setTimeout(() => step(1), Math.max(holdMs(beat.text), remaining) / speed)
    return () => clearTimeout(id)
  }, [autoAdvance, started, playing, done, pageComplete, beat, idx, speed, step, blocking, elapsed])

  const advance = useCallback(() => {
    if (!started) {
      setStarted(true)
      goTo(fork?.from ?? 0)
      return
    }
    if (done) return
    if (!pageComplete) settle()
    dispatch({ type: 'press' })
  }, [started, done, goTo, pageComplete, settle, fork])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLElement && /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return
      if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); advance() }
      else if (e.key === 'ArrowRight') step(1)
      else if (e.key === 'ArrowLeft') step(-1)
      else if (e.key.toLowerCase() === 'p') setPlaying((p) => !p)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [advance, step])

  // The generated avatars, for the dialogue-box portrait: pixelated in CSS so an anime face sits
  // beside 8-bit sprites without looking pasted in. A run that generated none keeps the sprite
  // crop, which is what every persona had before.
  const [avatars, setAvatars] = useState<Record<string, string>>({})
  useEffect(() => {
    let live = true
    for (const p of performers) {
      const url = portraits[p.name]
      if (!url) continue
      loadAvatar(url).then((blob) => {
        if (live && blob) setAvatars((cur) => (cur[p.name] ? cur : { ...cur, [p.name]: blob }))
      })
    }
    return () => {
      live = false
    }
  }, [performers, portraits])

  const prevLine = useMemo(() => {
    for (let j = idx - 1; j >= 0; j--) if (beats[j].line !== beat?.line) return beats[j]
    return undefined
  }, [beats, idx, beat])

  // Every consultant and messenger the run brings in, each in a sheet nobody in the cast is
  // wearing and nobody else is using, so one expert looks the same each time it is called.
  const visitors: VisitorSprite[] = useMemo(() => {
    const used = new Set(Object.values(sprites))
    const free = SPRITE_IDS.filter((id) => !used.has(id))
    return visitorsIn(beats).map((v, i) => ({
      key: v.key, label: tagFor(v.key, v.label), sprite: free[i % Math.max(1, free.length)] ?? SPRITE_IDS[0],
    }))
  }, [beats, sprites])

  const inRoom = started && beat?.kind === 'speech' && seats[beat.speaker] ? beat.speaker : null
  // A visitor delivering their own line: the consultant standing in the room, or the messenger.
  const visitorSpeaking = started && beat
    ? (beat.kind === 'consultant' ? SME_PREFIX + beat.speaker : beat.outsider ? visitors.find((v) => v.label === beat.speaker)?.key ?? null : null)
    : null
  const speaker = inRoom ?? visitorSpeaking
  // The prologue is not a transcript line, so it is not counted as one.
  const lines = beats.reduce((n, b) => Math.max(n, b.line + 1), 0)
  const title = detail.name ?? runRef

  // Above-the-head marks, each caused by something this line recorded.
  const marks = useMemo(() => {
    const m: Record<string, Mark[]> = {}
    if (!beat || !started) return m
    if (inRoom) {
      m[inRoom] = [...(beat.shifted ? (['shift'] as const) : []), ...(beat.consulted ? (['book'] as const) : [])]
    }
    // The asker keeps their seat, so a "?" over them is how you can still see who asked.
    if (beat.kind === 'consultant' && beat.askedBy && seats[beat.askedBy]) m[beat.askedBy] = ['ask']
    if (beat.outsider && visitorSpeaking) m[visitorSpeaking] = ['letter']
    return m
  }, [beat, started, inRoom, seats, visitorSpeaking])


  // The whiteboard: what was pinned at this point in the log, and what went up since the last line.
  const board = useMemo(
    () => boardAt(events, !started ? (beats[0]?.seq ?? Infinity) : beat ? beat.seq : Infinity),
    [events, started, beats, beat],
  )
  const fresh = useMemo(
    () => new Set(started && prevLine ? board.filter((n) => n.since > prevLine.seq).map((n) => n.id) : []),
    [board, started, prevLine],
  )

  return (
    <Shell title={title} width={stageW}>
      <div className="th-stagebox">
        <Stage performers={performers} visitors={visitors} blocking={blocking} clock={elapsed}
          speaker={speaker} bubble={!!inRoom && !pageComplete} marks={marks} board={board} fresh={fresh}
          motion={motion} onWidth={setStageW} />
        {!started && (
          <div className="th-card">
            <p className="th-card-kicker">A replay of</p>
            <h1 className="th-card-title">{title}</h1>
            <p className="th-card-topic">{detail.topic}</p>
            <p className="th-card-meta">{performers.length} personas · {lines} lines</p>
            {fork && (
              <p className="th-card-fork">
                Forked from <b>{fork.parent.name ?? fork.parent.run_id}</b> at turn {fork.at}.
                Starts at the fork; scrub back for the {fork.inherited} inherited{' '}
                {fork.inherited === 1 ? 'line' : 'lines'}.
              </p>
            )}
            <button className="th-btn th-btn-go" onClick={advance} disabled={!beats.length}>
              {beats.length ? '▶ Start' : 'Nothing was said in this run'}
            </button>
          </div>
        )}
        {done && (
          <div className="th-card">
            <p className="th-card-kicker">End of transcript</p>
            <h1 className="th-card-title">{title}</h1>
            <p className="th-card-meta">The run ended {detail.status === 'complete' ? 'normally' : `as ${detail.status}`}.</p>
            <button className="th-btn th-btn-go" onClick={() => goTo(fork?.from ?? 0)}>↻ Replay</button>
          </div>
        )}
      </div>

      {board.length > 0 && (
        <div className="th-board" aria-label="Working assumptions on the board">
          <span className="th-board-k">On the board</span>
          {board.map((n) => (
            <span key={n.id} className={`th-board-note${fresh.has(n.id) ? ' th-new' : ''}`}>
              {fresh.has(n.id) && <b>NEW · </b>}{n.statement}
            </span>
          ))}
        </div>
      )}

      {started && !done && beat && (
        <Dialogue beat={beat} typed={typed} complete={pageComplete} sprite={sprites[beat.speaker] ?? null}
          avatar={avatars[beat.speaker] ?? null} onAdvance={advance} />
      )}

      {started && (
        <nav className="th-controls" aria-label="Playback">
          <button className="th-btn" onClick={() => goTo(0)} aria-label="Restart">↺</button>
          <button className="th-btn" onClick={() => step(-1)} aria-label="Previous page">◀</button>
          <button className="th-btn" onClick={() => setPlaying((p) => !p)} aria-label={playing ? 'Pause' : 'Play'}>
            {playing ? '❚❚' : '▶'}
          </button>
          <button className="th-btn" onClick={() => step(1)} aria-label="Next page">▶▶</button>
          <button className="th-btn" onClick={() => setSpeed((s) => SPEEDS[(SPEEDS.indexOf(s) + 1) % SPEEDS.length])}
            aria-label="Speed">{speed}×</button>
          <input className="th-scrub" type="range" min={0} max={Math.max(0, beats.length - 1)} value={Math.min(idx, beats.length - 1)}
            onChange={(e) => goTo(Number(e.target.value))} aria-label="Position in the transcript" />
          <span className="th-pos">
            {!beat ? `${lines}/${lines}`
              : beat.kind === 'prologue' ? 'BEFORE TURN 1'
                : `LINE ${beat.line + 1}/${lines} · TURN ${beat.turn}`}
          </span>
        </nav>
      )}
    </Shell>
  )
}

function Portrait({ sprite, avatar, label }: { sprite: string | null; avatar: string | null; label: string }) {
  // The run's own generated avatar where there is one, pixelated by CSS to match the sprites.
  if (avatar) return <div className="th-portrait th-portrait-av" aria-hidden style={{ backgroundImage: `url(${avatar})` }} />
  if (!sprite) return <div className="th-portrait th-portrait-none" aria-hidden>{label}</div>
  // The head and shoulders of the front-facing idle frame, at 3×.
  return <div className="th-portrait" aria-hidden style={{ backgroundImage: `url(${spriteUrl(sprite)})` }} />
}

function Dialogue({ beat, typed, complete, sprite, avatar, onAdvance }: {
  beat: Beat; typed: number; complete: boolean; sprite: string | null; avatar: string | null; onAdvance: () => void
}) {
  const shown = beat.text.slice(0, typed)
  const hidden = beat.text.slice(typed)
  const label =
    beat.kind === 'prologue' ? 'Research · before the room met'
      : beat.kind === 'consultant' ? `${beat.speaker} · consultant${beat.askedBy ? `, answering ${beat.askedBy}` : ''}`
        : beat.kind === 'injected' ? `${beat.speaker} · injected into the conversation`
          : beat.speaker
  return (
    // Space and Enter do the same from anywhere on the page (see the key handler above).
    <section className={`th-dialogue th-${beat.kind}`} onClick={onAdvance} aria-label="Dialogue: tap to continue">
      {/* Screen readers get each page once, whole, rather than a character at a time. */}
      <p className="th-sr" aria-live="polite">
        {`${label}, turn ${beat.turn}: ${beat.text}`}
        {beat.shifted ? ' Their position moved.' : ''}
        {beat.consulted ? ' They drew on their sources.' : ''}
      </p>
      <Portrait sprite={beat.kind === 'speech' ? sprite : null} avatar={beat.kind === 'speech' ? avatar : null}
        label={beat.kind === 'prologue' ? 'READ' : beat.kind === 'consultant' ? 'SME' : '\u2709'} />
      <div className="th-text">
        <div className="th-name">
          <span>{label}</span>
          <span className="th-turn">
            {beat.kind === 'prologue' && <span className="th-tag">PROLOGUE</span>}
            {beat.shifted && <span className="th-tag th-tag-shift" title="The engine recorded a change of position in this message">SHIFT</span>}
            {beat.consulted && <span className="th-tag" title="Retrieved passages were in this message's prompt">SOURCES</span>}
            {beat.kind === 'prologue' ? '' : `#${String(beat.turn).padStart(2, '0')}`}{beat.pages > 1 ? ` · ${beat.page + 1}/${beat.pages}` : ''}
          </span>
        </div>
        <p className="th-line" aria-hidden>
          {shown}
          <span className="th-unshown">{hidden}</span>
          {complete && <span className="th-more">▼</span>}
        </p>
      </div>
    </section>
  )
}
