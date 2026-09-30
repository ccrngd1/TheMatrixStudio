// SPDX-License-Identifier: Apache-2.0
// The 8-bit theatre's script: a finished run's transcript turned into what the stage plays.
//
// Pure functions only, so what the replay claims is testable without a canvas. Every beat comes
// from one message in the feed, in feed order, and nothing is invented to fill a gap. The stage
// may be theatrical about HOW a line is delivered; it may not add a line, drop one, or change
// who said it.

import type { FeedMessage, SimEvent } from '../types'

/**
 * The character sheets shipped in `public/theatre/sprites`. `char17` and `char23` are absent on
 * purpose: their sheets are six rows rather than eight and their walk cycles are broken (the
 * source project maps them to neighbours for the same reason).
 */
export const SPRITE_IDS: readonly string[] = Array.from({ length: 41 }, (_, i) => i + 1)
  .filter((n) => n !== 17 && n !== 23)
  .map((n) => `char${n}`)

export const spriteUrl = (id: string) => `/theatre/sprites/${id}.png`
export const EMOTES_URL = '/theatre/sprites/emotes.png'

/** FNV-1a: small, stable across sessions, and good enough to spread names over 39 sheets. */
function hash(s: string): number {
  let h = 0x811c9dc5
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 0x01000193) >>> 0
  }
  return h
}

/**
 * A sprite per persona, stable for a name, and never shared within one cast while there are
 * sheets to spare. Stable matters because the same persona reappears across a run's branches
 * and across runs built from one template, and should look the same in each.
 */
export function assignSprites(names: readonly string[]): Record<string, string> {
  const taken = new Set<string>()
  const out: Record<string, string> = {}
  for (const name of names) {
    let i = hash(name) % SPRITE_IDS.length
    for (let probe = 0; probe < SPRITE_IDS.length && taken.has(SPRITE_IDS[i]); probe++) {
      i = (i + 1) % SPRITE_IDS.length
    }
    out[name] = SPRITE_IDS[i]
    taken.add(SPRITE_IDS[i])
  }
  return out
}

export type BeatKind = 'speech' | 'consultant' | 'injected'

export interface Beat {
  /** Index of the feed message this beat came from, so every beat traces to a real message. */
  line: number
  /** That message's place in the event log, which is what orders it against assumptions. */
  seq: number
  /** Which page of that message this is, and of how many. A long turn is paged, not cut. */
  page: number
  pages: number
  kind: BeatKind
  /** Who is speaking: a persona, a consultant, or the injected voice's own name. */
  speaker: string
  /** For a consultant's answer: the persona who asked. */
  askedBy?: string
  turn: number
  text: string
  /** An injected voice from outside the cast (a letter, a customer). Delivered by a messenger. */
  outsider?: boolean
  /** The engine recorded a position shift in this message (`position.shift`). */
  shifted?: boolean
  /** Retrieved passages were in this message's prompt (`document.retrieved`). */
  consulted?: boolean
}

/** Roughly four lines of the dialogue box at its narrowest. */
export const PAGE_CHARS = 240

/**
 * Split text into dialogue-box pages at sentence ends, falling back to word breaks for a
 * sentence longer than a page. Every character of the input survives into some page; only the
 * whitespace at a break is dropped.
 */
export function paginate(text: string, max = PAGE_CHARS): string[] {
  const clean = text.replace(/\s+/g, ' ').trim()
  if (!clean) return ['']
  if (clean.length <= max) return [clean]
  const sentences = clean.match(/[^.!?]+(?:[.!?]+["')\]]*|$)\s*/g) ?? [clean]
  const pages: string[] = []
  let cur = ''
  const flush = () => {
    if (cur.trim()) pages.push(cur.trim())
    cur = ''
  }
  for (const s of sentences) {
    if ((cur + s).trim().length <= max) {
      cur += s
      continue
    }
    flush()
    if (s.trim().length <= max) {
      cur = s
      continue
    }
    // A single sentence longer than a page: break between words.
    for (const word of s.trim().split(' ')) {
      if ((cur ? cur + ' ' + word : word).length > max && cur) flush()
      cur = cur ? cur + ' ' + word : word
    }
  }
  flush()
  return pages
}

export function buildScript(
  feed: readonly FeedMessage[],
  { max = PAGE_CHARS, cast = [] }: { max?: number; cast?: readonly string[] } = {},
): Beat[] {
  const inCast = new Set(cast)
  const beats: Beat[] = []
  feed.forEach((m, line) => {
    const kind: BeatKind = m.consultant ? 'consultant' : m.injected ? 'injected' : 'speech'
    const pages = paginate(m.content, max)
    pages.forEach((text, page) => {
      beats.push({
        line,
        seq: m.seq,
        page,
        pages: pages.length,
        kind,
        speaker: m.consultant?.expert ?? m.speaker,
        ...(m.consultant ? { askedBy: m.consultant.askedBy } : {}),
        turn: m.turn,
        text,
        // A cast member's name on an injected message is the operator putting words in that
        // persona's mouth; only a voice from outside the cast arrives by messenger.
        ...(kind === 'injected' && !inCast.has(m.speaker) ? { outsider: true } : {}),
        ...(m.shift ? { shifted: true } : {}),
        ...(m.sources?.length ? { consulted: true } : {}),
      })
    })
  })
  return beats
}

/** A working assumption on the whiteboard, and when it went up. */
export interface BoardNote {
  id: string
  statement: string
  /** The seq of its `assumption.made` event. */
  since: number
}

/**
 * What is pinned to the board at a point in the event log: every `assumption.made` up to `seq`,
 * less those withdrawn by then. Read from the raw events because the reduced run state keeps
 * only the assumptions still standing at the END, which cannot say what the room was reasoning
 * from at turn 5.
 */
export function boardAt(events: readonly SimEvent[], seq: number): BoardNote[] {
  const notes = new Map<string, BoardNote>()
  for (const e of [...events].sort((a, b) => a.seq - b.seq)) {
    if (e.seq > seq) break
    const id = e.payload?.id == null ? '' : String(e.payload.id)
    if (e.event_type === 'assumption.made' && id && e.payload.statement) {
      notes.delete(id)
      notes.set(id, { id, statement: String(e.payload.statement), since: e.seq })
    } else if (e.event_type === 'assumption.withdrawn' && id) {
      notes.delete(id)
    }
  }
  return [...notes.values()]
}

/** Characters per second the dialogue box types at, before the speed multiplier. */
export const TYPE_CPS = 45

/** How long a fully typed page stays up before the next one, at 1×. */
export function holdMs(text: string): number {
  return 1200 + text.length * 18
}

// ----------------------------------------------------------------------------------------- //
// The room. Logical pixels; the stage scales them by an integer so the sprites stay crisp.

export const ROOM_W = 384
export const ROOM_H = 224
export const TABLE = { x: 96, y: 104, w: 192, h: 44 }

export type Facing = 'down' | 'right' | 'up' | 'left'

export interface Seat {
  /** Where the sprite's feet go. */
  x: number
  y: number
  facing: Facing
  /** Drawn before the table (seated behind it) or after (seated in front, back to camera). */
  behindTable: boolean
}

/**
 * Seats around the table for `n` personas. Up to four a side, then one at each end; a cast
 * larger than ten doubles up at the ends rather than refusing to render.
 */
export function seatLayout(n: number): Seat[] {
  const ends = n > 8 ? Math.min(2, n - 8) : 0
  const sides = Math.min(n - ends, 8)
  const top = Math.ceil(sides / 2)
  const bottom = sides - top
  const row = (count: number, y: number, facing: Facing, behindTable: boolean): Seat[] => {
    const span = TABLE.w - 24
    return Array.from({ length: count }, (_, i) => ({
      x: Math.round(TABLE.x + 12 + (count === 1 ? span / 2 : (span * i) / (count - 1))),
      y,
      facing,
      behindTable,
    }))
  }
  const seats = [
    ...row(top, TABLE.y + 6, 'down', true),
    ...row(bottom, TABLE.y + TABLE.h + 30, 'up', false),
  ]
  if (ends >= 1) seats.push({ x: TABLE.x - 18, y: TABLE.y + TABLE.h - 2, facing: 'right', behindTable: false })
  if (ends >= 2) seats.push({ x: TABLE.x + TABLE.w + 18, y: TABLE.y + TABLE.h - 2, facing: 'left', behindTable: false })
  // More than ten: the rest stand along the back wall, facing the room.
  for (let i = 0; seats.length < n; i++) {
    seats.push({ x: 40 + ((i * 44) % (ROOM_W - 80)), y: 84, facing: 'down', behindTable: true })
  }
  return seats
}
