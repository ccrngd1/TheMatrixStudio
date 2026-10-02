// SPDX-License-Identifier: Apache-2.0
// The canvas the room is drawn on, and the loop that redraws it.
//
// The loop reads the latest props through a ref, so a new line re-renders the dialogue box
// without restarting the animation. Scale is an integer whenever the room fits at 1× or more,
// which keeps 48 px sprites from smearing; below that (a phone narrower than the room) it
// shrinks fractionally rather than overflowing.
//
// The scale comes from the WINDOW, less a fixed reserve for the dialogue box and controls, not
// from the space the layout happens to leave. Measuring the leftover space fed back: the
// canvas widened its own container, and a long page of dialogue would have rescaled the room
// mid-line.
//
// Positions come from the blocking at the playback clock's time, every frame, so a walk pauses
// when playback pauses and runs faster at 2×.

import { useEffect, useRef, useState } from 'react'
import { SME_PREFIX, poseAt, type Blocking } from './blocking'
import { EMOTES_URL, ROOM_H, ROOM_W, spriteUrl, type BoardNote } from './script'
import { drawScene, type Actor, type Mark, type Performer, type SpriteImages } from './stage'

/** Someone not in the cast who comes in to say one thing: a consultant, or a messenger. */
export interface VisitorSprite {
  key: string
  /** The short form for their name tag: a consultant's noun, or who a message is from. */
  label: string
  sprite: string
}

interface Props {
  performers: Performer[]
  /** Every visitor the run brings in. Each is drawn only where the blocking has them on stage. */
  visitors: VisitorSprite[]
  blocking: Blocking
  /** Playback milliseconds (1× units) since the current line started. */
  clock: () => number
  /** Whose tag is lit: a cast member's name, the messenger's key, or nobody. */
  speaker: string | null
  /** Show the "…" bubble over the speaker: they are speaking and the page is still typing. */
  bubble: boolean
  marks: Record<string, Mark[]>
  board: BoardNote[]
  fresh: ReadonlySet<string>
  motion: boolean
  /** Told the stage's on-screen width, so the dialogue box and controls can match it. */
  onWidth?: (px: number) => void
}

/** Height kept free below the room for the dialogue box, the board strip, the controls and the header. */
const RESERVE_H = 330
/** The canvas's border, both sides. */
const FRAME_PX = 8

export function Stage(props: Props) {
  const { performers, visitors, speaker, onWidth } = props
  const wrap = useRef<HTMLDivElement>(null)
  const canvas = useRef<HTMLCanvasElement>(null)
  const images = useRef<SpriteImages>({})
  const latest = useRef(props)
  latest.current = props
  const [scale, setScale] = useState(1)

  // Load each sheet once; the loop draws whichever have arrived.
  useEffect(() => {
    const want = new Set([...performers.map((p) => p.sprite), ...visitors.map((v) => v.sprite)])
    for (const id of want) {
      if (images.current[id]) continue
      const img = new Image()
      img.src = spriteUrl(id)
      images.current[id] = img
    }
    if (!images.current.emotes) {
      const img = new Image()
      img.src = EMOTES_URL
      images.current.emotes = img
    }
  }, [performers, visitors])

  const widthCb = useRef(onWidth)
  widthCb.current = onWidth
  useEffect(() => {
    const el = wrap.current
    if (!el) return
    const fit = () => {
      const w = el.clientWidth - FRAME_PX
      const h = window.innerHeight - RESERVE_H - FRAME_PX
      const raw = Math.min(w / ROOM_W, Math.max(h, ROOM_H * 0.5) / ROOM_H)
      const s = raw >= 1 ? Math.floor(raw) : Math.max(0.5, raw)
      setScale(s)
      widthCb.current?.(Math.round(ROOM_W * s) + FRAME_PX)
    }
    fit()
    window.addEventListener('resize', fit)
    const ro = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(fit)
    ro?.observe(el)
    return () => {
      window.removeEventListener('resize', fit)
      ro?.disconnect()
    }
  }, [])

  useEffect(() => {
    const c = canvas.current
    const ctx = c?.getContext?.('2d')
    if (!c || !ctx) return
    const dpr = window.devicePixelRatio || 1
    c.width = Math.round(ROOM_W * scale * dpr)
    c.height = Math.round(ROOM_H * scale * dpr)
    let raf = 0
    const draw = (t: number) => {
      const cur = latest.current
      const elapsed = cur.clock()
      // Every cast member and consultant is a simulated persona and its tag says so; the messenger carries the
      // operator's words and does not.
      const actors: Actor[] = cur.performers.map((p) => ({
        key: p.name, label: p.name.split(/\s+/)[0], sprite: p.sprite,
        pose: poseAt(cur.blocking, p.name, p.seat, elapsed), marks: cur.marks[p.name] ?? [], bot: true,
      }))
      for (const v of cur.visitors) {
        actors.push({
          key: v.key, label: v.label, sprite: v.sprite,
          pose: poseAt(cur.blocking, v.key, null, elapsed), marks: cur.marks[v.key] ?? [],
          bot: v.key.startsWith(SME_PREFIX),
        })
      }
      ctx.setTransform(scale * dpr, 0, 0, scale * dpr, 0, 0)
      ctx.imageSmoothingEnabled = false
      drawScene(ctx, images.current, {
        seats: cur.performers.map((p) => p.seat),
        actors,
        speaker: cur.speaker,
        typing: cur.bubble,
        markT: elapsed - cur.blocking.preMs,
        board: cur.board,
        fresh: cur.fresh,
        allNames: scale >= 2,
        motion: cur.motion,
        t,
      })
      raf = requestAnimationFrame(draw)
    }
    raf = requestAnimationFrame(draw)
    return () => cancelAnimationFrame(raf)
  }, [scale])

  return (
    <div ref={wrap} className="th-stage">
      <canvas
        ref={canvas}
        role="img"
        aria-label={`Conference room with ${performers.length} personas${
          speaker && !visitors.some((v) => v.key === speaker) ? `; ${speaker} is speaking` : ''}`}
        style={{ width: ROOM_W * scale, height: ROOM_H * scale }}
      />
    </div>
  )
}
