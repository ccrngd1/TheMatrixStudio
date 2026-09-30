// SPDX-License-Identifier: Apache-2.0
// Blocking: who walks where during a line, as a pure function of the script and the line.
//
// Every movement is caused by something in the transcript, and only by that:
//
//   * a consultant's answer  → the persona who asked walks out through the SME door, and walks
//     back when the next line starts;
//   * an injected outside voice (a letter, a customer) → a messenger walks in with it, and out
//     again when the next line starts.
//
// Nothing else moves anyone. In particular nobody leaves to "do research" mid-conversation:
// research happens before turn 1, so if it is ever staged it belongs before the first line.
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

/** 75 logical px a second at 1×: the width of the table in about two and a half seconds. */
export const WALK_PX_PER_MS = 0.075

/** Just inside the left-hand (SME) door, and just beyond it, out of the room. */
const SME_DOOR: Point = { x: 16, y: 128 }
const SME_OFF: Point = { x: -26, y: 128 }
/** Where the messenger stands to deliver, beside the right end of the table, and where they come from. */
export const MESSENGER_SPOT: Point = { x: TABLE.x + TABLE.w + 34, y: TABLE.y + TABLE.h + 12 }
const MESSENGER_OFF: Point = { x: MESSENGER_SPOT.x, y: ROOM_H + 44 }

export interface Move {
  actor: string
  path: Point[]
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
  /** Cast members out of the room for the whole line. */
  away: string[]
  /** The messenger stands at the table for the whole line. */
  messengerStays: boolean
}

const STILL: Blocking = { preMs: 0, settleMs: 0, moves: [], away: [], messengerStays: false }

function dedupe(path: Point[]): Point[] {
  return path.filter((p, i) => i === 0 || p.x !== path[i - 1].x || p.y !== path[i - 1].y)
}

/**
 * From a seat to outside the SME door, along right angles like a sprite should: out from the
 * table to an aisle, across to the door's column, down or up to the door, then through it.
 */
export function pathToSmeDoor(seat: Seat): Point[] {
  const aisle = seat.behindTable
    ? Math.min(seat.y, TABLE.y - 12)
    : seat.y > TABLE.y + TABLE.h
      ? seat.y + 16
      : seat.y
  return dedupe([
    { x: seat.x, y: seat.y },
    { x: seat.x, y: aisle },
    { x: SME_DOOR.x, y: aisle },
    SME_DOOR,
    SME_OFF,
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
  let left = Math.max(0, t) * WALK_PX_PER_MS
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
  const asker = (x: Beat | undefined) => (x?.kind === 'consultant' && x.askedBy && seats[x.askedBy] ? x.askedBy : null)

  // Later pages of a line hold whatever its first page arrived at.
  if (b.page > 0) {
    const a = asker(b)
    return { ...STILL, away: a ? [a] : [], messengerStays: !!b.outsider }
  }

  const prev = previousLine(beats, i)
  const moves: Move[] = []
  const away: string[] = []
  let preMs = 0
  let messengerStays = false

  // Whoever left for the last line comes back, unless they are still consulting.
  const leftLast = asker(prev)
  const leavesNow = asker(b)
  if (leftLast) {
    if (leavesNow === leftLast) {
      away.push(leftLast)
    } else {
      const seat = seats[leftLast]
      const path = [...pathToSmeDoor(seat)].reverse()
      moves.push({ actor: leftLast, path, hideAtEnd: false, endFacing: seat.facing })
      // Someone about to speak has to be back in their seat first; anyone else walks back while
      // the next line is already being read.
      if (b.kind === 'speech' && b.speaker === leftLast) preMs = Math.max(preMs, walkMs(path))
    }
  }
  if (leavesNow && leavesNow !== leftLast) {
    const path = pathToSmeDoor(seats[leavesNow])
    moves.push({ actor: leavesNow, path, hideAtEnd: true })
    preMs = Math.max(preMs, walkMs(path))
  }

  // The messenger: in with an outside voice, out when the line after it starts.
  if (prev?.outsider && b.outsider) messengerStays = true
  else if (prev?.outsider) moves.push({ actor: MESSENGER, path: [MESSENGER_SPOT, MESSENGER_OFF], hideAtEnd: true })
  else if (b.outsider) {
    const path = [MESSENGER_OFF, MESSENGER_SPOT]
    moves.push({ actor: MESSENGER, path, hideAtEnd: false, endFacing: 'left' })
    preMs = Math.max(preMs, walkMs(path))
  }

  const settleMs = Math.max(preMs, ...moves.map((m) => walkMs(m.path)))
  return { preMs, settleMs, moves, away, messengerStays }
}

/** Where an actor is `t` ms (1×) into the line. `seat` is null for the messenger. */
export function poseAt(bl: Blocking, actor: string, seat: Seat | null, t: number): Pose {
  const move = bl.moves.find((m) => m.actor === actor)
  if (move) return poseOnPath(move, t)
  if (bl.away.includes(actor)) return { ...SME_OFF, facing: 'left', walking: false, visible: false }
  if (actor === MESSENGER) {
    return { ...MESSENGER_SPOT, facing: 'left', walking: false, visible: bl.messengerStays }
  }
  if (!seat) return { x: -100, y: -100, facing: 'down', walking: false, visible: false }
  return { x: seat.x, y: seat.y, facing: seat.facing, walking: false, visible: true }
}
