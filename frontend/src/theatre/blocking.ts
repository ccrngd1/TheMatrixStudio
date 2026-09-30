// SPDX-License-Identifier: Apache-2.0
// Blocking: who walks where during a line, as a pure function of the script and the line.
//
// Every movement is caused by something in the transcript, and only by that:
//
//   * a consultant's answer → that consultant walks in through the SME door, stands at the end of
//     the table to answer, and leaves when the next line starts. The persona who asked stays in
//     their seat with a "?" over them, so who asked is still visible.
//   * an injected outside voice (a letter, a customer) → a messenger walks in with it, and out
//     again when the next line starts.
//
// Both are visitors: not cast, one stable sprite per identity for the whole run, one at a time in
// each spot. Two answers in a row from the same consultant keep them standing rather than sending
// them out and straight back in.
//
// The cast never move during the conversation. They do walk in during the research prologue,
// which is before turn 1 and is the only scene that is not a line.
//
// Pure, and a function of (beats, index) alone rather than of how playback got there, so a
// scrub straight to line 30 stages line 30 exactly as playing through would.

import { ROOM_H, TABLE, type Beat, type Facing, type Seat } from './script'

export interface Point {
  x: number
  y: number
}

export interface Pose extends Point {
  facing: Facing
  walking: boolean
  visible: boolean
}

/** The stand-in who carries an injected outside voice into the room. Not a cast member. */
export const MESSENGER = '__messenger__'
/** A consultant's visitor key is its expert name behind this prefix, so two experts never share one. */
export const SME_PREFIX = 'sme:'

/** How far apart the cast set off during the prologue, so they file in one behind another. */
export const PROLOGUE_STAGGER_MS = 420

/** 75 logical px a second at 1×: the width of the table in about two and a half seconds. */
export const WALK_PX_PER_MS = 0.075

/** Just inside the left-hand (SME) door, and just beyond it, out of the room. */
const SME_DOOR: Point = { x: 16, y: 128 }
const SME_OFF: Point = { x: -26, y: 128 }
/** Where a consultant stands to answer: off the left end of the table, facing the room. */
export const SME_SPOT: Point = { x: TABLE.x - 42, y: TABLE.y + TABLE.h + 16 }
/** Where the messenger stands to deliver, beside the right end of the table, and where they come from. */
export const MESSENGER_SPOT: Point = { x: TABLE.x + TABLE.w + 34, y: TABLE.y + TABLE.h + 12 }
const MESSENGER_OFF: Point = { x: MESSENGER_SPOT.x, y: ROOM_H + 44 }
/** The right-hand (LIB) door, for the research prologue. */
const LIB_DOOR: Point = { x: 368, y: 128 }
const LIB_OFF: Point = { x: 410, y: 128 }

export interface Move {
  actor: string
  path: Point[]
  /** Wait this long (1× ms) before setting off, so a group files in instead of overlapping. */
  startMs?: number
  /** Out of sight once the path ends (it ended off-stage). */
  hideAtEnd: boolean
  /** Which way to face on arrival; otherwise the direction of the last step. */
  endFacing?: Facing
}

export interface Blocking {
  /** How long this line waits, at 1×, for someone to arrive before its text starts typing. */
  preMs: number
  /** When every move in this line has finished, at 1×. */
  settleMs: number
  moves: Move[]
  /** Visitors standing in place for the whole line (they arrived on an earlier line). */
  holding: string[]
  /** Cast members who are not in the room at all yet (before the prologue walk-in). */
  offstage: string[]
}

const STILL: Blocking = { preMs: 0, settleMs: 0, moves: [], holding: [], offstage: [] }

function dedupe(path: Point[]): Point[] {
  return path.filter((p, i) => i === 0 || p.x !== path[i - 1].x || p.y !== path[i - 1].y)
}

/** Someone who is not in the cast, who comes in to say one thing and then leaves. */
export interface Visitor {
  key: string
  /** What their name tag says. */
  label: string
  /** Where they stand to speak. */
  spot: Point
  facing: Facing
  /** The way in; reversed to leave. */
  path: Point[]
}

const SME_PATH = dedupe([SME_OFF, SME_DOOR, { x: SME_DOOR.x, y: SME_SPOT.y }, SME_SPOT])
const MESSENGER_PATH = [MESSENGER_OFF, MESSENGER_SPOT]

/** The visitor a line brings in, if it brings one. */
export function visitorFor(b: Beat | undefined): Visitor | null {
  if (!b) return null
  if (b.kind === 'consultant') {
    return { key: SME_PREFIX + b.speaker, label: b.speaker, spot: SME_SPOT, facing: 'right', path: SME_PATH }
  }
  if (b.outsider) {
    return { key: MESSENGER, label: b.speaker, spot: MESSENGER_SPOT, facing: 'left', path: MESSENGER_PATH }
  }
  return null
}

/** Every visitor a script brings in, in first-appearance order, so each can be given a sprite. */
export function visitorsIn(beats: readonly Beat[]): Visitor[] {
  const seen = new Map<string, Visitor>()
  for (const b of beats) {
    const v = visitorFor(b)
    if (v && !seen.has(v.key)) seen.set(v.key, v)
  }
  return [...seen.values()]
}

/** From the right-hand (LIB) door to a seat, along right angles: the research prologue's walk. */
export function pathFromLibDoor(seat: Seat): Point[] {
  const aisle = seat.behindTable
    ? Math.min(seat.y, TABLE.y - 12)
    : seat.y > TABLE.y + TABLE.h
      ? seat.y + 16
      : seat.y
  return dedupe([
    LIB_OFF,
    LIB_DOOR,
    { x: LIB_DOOR.x, y: aisle },
    { x: seat.x, y: aisle },
    { x: seat.x, y: seat.y },
  ])
}

export function pathLength(path: Point[]): number {
  let d = 0
  for (let i = 1; i < path.length; i++) d += Math.abs(path[i].x - path[i - 1].x) + Math.abs(path[i].y - path[i - 1].y)
  return d
}

export const walkMs = (path: Point[]) => pathLength(path) / WALK_PX_PER_MS

function facingOf(from: Point, to: Point, fallback: Facing): Facing {
  if (to.x < from.x) return 'left'
  if (to.x > from.x) return 'right'
  if (to.y < from.y) return 'up'
  if (to.y > from.y) return 'down'
  return fallback
}

/** Where a mover is `t` ms (1×) into its path. */
export function poseOnPath(m: Move, t: number): Pose {
  const { path } = m
  let left = Math.max(0, t - (m.startMs ?? 0)) * WALK_PX_PER_MS
  for (let i = 1; i < path.length; i++) {
    const a = path[i - 1], b = path[i]
    const seg = Math.abs(b.x - a.x) + Math.abs(b.y - a.y)
    if (left <= seg) {
      const f = seg ? left / seg : 1
      return { x: a.x + (b.x - a.x) * f, y: a.y + (b.y - a.y) * f, facing: facingOf(a, b, 'down'), walking: true, visible: true }
    }
    left -= seg
  }
  const end = path[path.length - 1]
  const prev = path[path.length - 2] ?? end
  return { ...end, facing: m.endFacing ?? facingOf(prev, end, 'down'), walking: false, visible: !m.hideAtEnd }
}

/** The last beat of the line before `beats[i]`'s line, if any. */
function previousLine(beats: readonly Beat[], i: number): Beat | undefined {
  const line = beats[i]?.line
  for (let j = i - 1; j >= 0; j--) if (beats[j].line !== line) return beats[j]
  return undefined
}

export function computeBlocking(beats: readonly Beat[], i: number, seats: Readonly<Record<string, Seat>>): Blocking {
  const b = beats[i]
  if (!b) return STILL

  // The prologue: the cast walk in from the library door. Cast members move only here.
  if (b.kind === 'prologue') {
    // Staggered, because eight people leaving the same doorway at once overlap into one blob.
    const moves = Object.entries(seats).map(([name, seat], n) => ({
      actor: name, path: pathFromLibDoor(seat), hideAtEnd: false, endFacing: seat.facing,
      startMs: n * PROLOGUE_STAGGER_MS,
    }))
    const settleMs = Math.max(0, ...moves.map((m) => (m.startMs ?? 0) + walkMs(m.path)))
    // The caption reads while they walk in; it would be a long wait otherwise.
    return { preMs: 0, settleMs, moves, holding: [], offstage: [] }
  }

  const now = visitorFor(b)

  // Later pages of a line hold whatever its first page arrived at.
  if (b.page > 0) {
    return { ...STILL, holding: now ? [now.key] : [] }
  }

  const prev = previousLine(beats, i)
  const before = visitorFor(prev)
  const moves: Move[] = []
  const holding: string[] = []
  let preMs = 0

  // A visitor stays only for consecutive lines that are their own; otherwise they see themselves
  // out as the next line starts, which does not hold that line up.
  if (before && (!now || now.key !== before.key)) {
    moves.push({ actor: before.key, path: [...before.path].reverse(), hideAtEnd: true })
  }
  if (now) {
    if (before?.key === now.key) holding.push(now.key)
    else {
      moves.push({ actor: now.key, path: now.path, hideAtEnd: false, endFacing: now.facing })
      // Nobody speaks before they are in the room.
      preMs = Math.max(preMs, walkMs(now.path))
    }
  }

  // The cast are off-stage until the prologue has walked them in; after it, everyone is seated.
  const settleMs = Math.max(preMs, ...moves.map((m) => (m.startMs ?? 0) + walkMs(m.path)))
  return { preMs, settleMs, moves, holding, offstage: [] }
}

/** Where an actor is `t` ms (1×) into the line. `seat` is null for a visitor. */
export function poseAt(bl: Blocking, actor: string, seat: Seat | null, t: number): Pose {
  const move = bl.moves.find((m) => m.actor === actor)
  if (move) return poseOnPath(move, t)
  if (bl.holding.includes(actor)) {
    const spot = actor === MESSENGER ? MESSENGER_SPOT : SME_SPOT
    return { ...spot, facing: actor === MESSENGER ? 'left' : 'right', walking: false, visible: true }
  }
  if (!seat) return { x: -100, y: -100, facing: 'down', walking: false, visible: false }
  if (bl.offstage.includes(actor)) return { ...LIB_OFF, facing: 'left', walking: false, visible: false }
  return { x: seat.x, y: seat.y, facing: seat.facing, walking: false, visible: true }
}
