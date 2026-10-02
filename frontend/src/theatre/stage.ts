// SPDX-License-Identifier: Apache-2.0
// Drawing the 8-bit room. Everything here is decoration over the script: it receives who is
// seated where and who is speaking, and draws that. It decides nothing about the transcript.
//
// Coordinates are logical pixels (ROOM_W × ROOM_H). The caller scales the context by an integer
// where it can, with smoothing off, so sprites stay crisp; text is drawn by the context too, so
// at 2× and above it is rendered at device resolution rather than blown up.

import { ROOM_H, ROOM_W, TABLE, type BoardNote, type Facing, type Seat } from './script'
import type { Pose } from './blocking'

/** Sprite sheets are 4 frames × 8 rows of 48 px: rows 0-3 idle, 4-7 walking; down, right, up, left. */
export const FRAME = 48
const ROW: Record<Facing, number> = { down: 0, right: 1, up: 2, left: 3 }
/** Where a character's feet sit inside its frame, measured over all 39 sheets (feet y 39-41). */
const FOOT_X = 24
const FOOT_Y = 40
/** The wheelchair sheets have no idle cycle in the source project; they hold frame 0. */
const STATIC_IDLE = new Set(['char39', 'char40', 'char41'])

export interface Performer {
  name: string
  sprite: string
  seat: Seat
}

/** Something drawn above an actor's head, each caused by a recorded event on the current line. */
export type Mark = 'shift' | 'book' | 'letter' | 'ask'

/** Someone on stage this frame: a cast member wherever the blocking has put them, or the messenger. */
export interface Actor {
  /** Identity: the cast member's name, or a visitor's key. */
  key: string
  /** The short form on the name tag: a first name, or a visitor's noun. */
  label: string
  sprite: string
  pose: Pose
  marks: Mark[]
  /** A simulated persona or consultant, whose tag carries the robot marker. Not the messenger: that is the operator. */
  bot?: boolean
}

export interface SceneFrame {
  /** Every chair, occupied or not: a persona who has stepped out leaves an empty one. */
  seats: Seat[]
  actors: Actor[]
  /** The actor delivering the current line, if one in the room is. */
  speaker: string | null
  /** True while the dialogue box is still typing, which is when the speech bubble shows. */
  typing: boolean
  /** Milliseconds since this line's text began typing; drives the "!" pop. Negative before. */
  markT: number
  board: BoardNote[]
  /** Notes pinned since the previous line, drawn highlighted. */
  fresh: ReadonlySet<string>
  /** Name tags for everyone (room to read them) or only the speaker (a phone at 1×). */
  allNames: boolean
  motion: boolean
  t: number
}

export type SpriteImages = Record<string, HTMLImageElement | undefined>

const C = {
  wall: '#c9bfa6', wallTop: '#8f866f', wallShade: '#b3a98f', skirting: '#5b4a36',
  carpetA: '#344264', carpetB: '#2e3a59', carpetEdge: '#27314b',
  wood: '#8b5a2b', woodTop: '#a26a35', woodEdge: '#5c3a1a', woodHi: '#b97f45',
  chair: '#252c38', chairHi: '#3a4456',
  glass: '#86b7d8', glassHi: '#c6e2f2', frame: '#5a4632',
  board: '#eef0f2', boardFrame: '#7d8791',
  door: '#6b4a2b', doorHi: '#80593a', knob: '#e2c15a',
  leaf: '#3c8d40', leafHi: '#5fb15e', pot: '#8a4b2a',
  shadow: 'rgba(0,0,0,0.28)', tag: 'rgba(8,10,20,0.78)', tagOn: '#ffd24a', ink: '#f4f1e8',
}

const rect = (ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, c: string) => {
  ctx.fillStyle = c
  ctx.fillRect(x, y, w, h)
}

function drawRoom(ctx: CanvasRenderingContext2D) {
  // Carpet, a 16 px checker so the scale reads.
  for (let y = 64; y < ROOM_H; y += 16) {
    for (let x = 0; x < ROOM_W; x += 16) rect(ctx, x, y, 16, 16, (x + y) % 32 ? C.carpetA : C.carpetB)
  }
  rect(ctx, 0, ROOM_H - 4, ROOM_W, 4, C.carpetEdge)
  // Back wall with a darker cornice and a skirting board.
  rect(ctx, 0, 0, ROOM_W, 64, C.wall)
  rect(ctx, 0, 0, ROOM_W, 8, C.wallTop)
  for (let x = 0; x < ROOM_W; x += 32) rect(ctx, x, 8, 1, 52, C.wallShade)
  rect(ctx, 0, 58, ROOM_W, 6, C.skirting)
  // Two windows either side of a whiteboard.
  for (const wx of [24, 304]) {
    rect(ctx, wx, 14, 56, 34, C.frame)
    rect(ctx, wx + 3, 17, 50, 28, C.glass)
    rect(ctx, wx + 27, 17, 2, 28, C.frame)
    for (let i = 0; i < 10; i++) rect(ctx, wx + 6 + i, 40 - i * 2, 2, 2, C.glassHi)
  }
  rect(ctx, 120, 12, 144, 40, C.boardFrame)
  rect(ctx, 123, 15, 138, 34, C.board)
  rect(ctx, 150, 49, 84, 3, C.boardFrame)
  // (Notes on the board are drawn by drawBoard, from the run's working assumptions.)
  // A clock.
  rect(ctx, 186, 1, 12, 10, C.frame)
  rect(ctx, 188, 2, 8, 8, C.ink)
  rect(ctx, 191, 3, 1, 4, C.frame)
  rect(ctx, 191, 6, 3, 1, C.frame)
  // Doors in the side walls: where a persona will leave to see a consultant or to research.
  drawDoor(ctx, 0, 92, 'SME')
  drawDoor(ctx, ROOM_W - 10, 92, 'LIB')
  // Plants in the back corners.
  drawPlant(ctx, 6, 62)
  drawPlant(ctx, ROOM_W - 22, 62)
}

function drawDoor(ctx: CanvasRenderingContext2D, x: number, y: number, label: string) {
  rect(ctx, x, y, 10, 44, C.door)
  rect(ctx, x + (x === 0 ? 7 : 1), y + 2, 2, 40, C.doorHi)
  rect(ctx, x + (x === 0 ? 7 : 1), y + 22, 2, 3, C.knob)
  ctx.save()
  ctx.fillStyle = C.ink
  ctx.font = 'bold 5px ui-monospace, monospace'
  ctx.textAlign = x === 0 ? 'left' : 'right'
  ctx.fillText(label, x === 0 ? 12 : ROOM_W - 12, y - 2)
  ctx.restore()
}

function drawPlant(ctx: CanvasRenderingContext2D, x: number, y: number) {
  rect(ctx, x + 3, y + 10, 10, 8, C.pot)
  rect(ctx, x + 2, y + 9, 12, 2, C.woodEdge)
  for (const [dx, dy, w, h] of [[6, -6, 4, 16], [2, -2, 4, 10], [10, -1, 4, 10], [4, -9, 3, 6], [9, -8, 3, 6]]) {
    rect(ctx, x + dx, y + dy, w, h, C.leaf)
  }
  rect(ctx, x + 7, y - 5, 2, 8, C.leafHi)
}

function drawTable(ctx: CanvasRenderingContext2D) {
  const { x, y, w, h } = TABLE
  rect(ctx, x + 6, y + h, 6, 8, C.woodEdge)
  rect(ctx, x + w - 12, y + h, 6, 8, C.woodEdge)
  rect(ctx, x - 2, y - 2, w + 4, h + 4, C.woodEdge)
  rect(ctx, x, y, w, h - 6, C.woodTop)
  rect(ctx, x, y + h - 6, w, 6, C.wood)
  rect(ctx, x + 4, y + 3, w - 8, 1, C.woodHi)
  // Papers and a laptop, so the table is not an empty slab.
  rect(ctx, x + 40, y + 12, 12, 9, C.ink)
  rect(ctx, x + 132, y + 16, 12, 9, C.ink)
  rect(ctx, x + 86, y + 10, 20, 12, C.chair)
  rect(ctx, x + 88, y + 12, 16, 8, C.glass)
}

function drawChairBack(ctx: CanvasRenderingContext2D, s: Seat) {
  rect(ctx, s.x - 11, s.y - 30, 22, 26, C.chair)
  rect(ctx, s.x - 9, s.y - 28, 18, 2, C.chairHi)
}

function drawChairSeat(ctx: CanvasRenderingContext2D, s: Seat) {
  if (s.facing === 'up') {
    rect(ctx, s.x - 11, s.y - 10, 22, 8, C.chair)
    rect(ctx, s.x - 9, s.y - 2, 3, 6, C.chair)
    rect(ctx, s.x + 6, s.y - 2, 3, 6, C.chair)
  } else {
    rect(ctx, s.x - 8, s.y - 14, 16, 12, C.chair)
  }
}

const NOTE_COLOURS = ['#ffe27a', '#ffb3c7', '#b8f2a1', '#a8d8ff']
const NOTE_W = 31
const NOTE_MAX = 4

function wrap(ctx: CanvasRenderingContext2D, text: string, width: number, lines: number): string[] {
  const out: string[] = []
  let cur = ''
  for (const word of text.split(/\s+/)) {
    const next = cur ? `${cur} ${word}` : word
    if (ctx.measureText(next).width <= width) cur = next
    else {
      if (cur) out.push(cur)
      cur = word
    }
    if (out.length === lines) break
  }
  if (out.length < lines && cur) out.push(cur)
  if (out.length === lines && out.join(' ').length < text.length) out[lines - 1] = out[lines - 1].replace(/.?$/, '…')
  return out.slice(0, lines)
}

/** The working assumptions in force at this line, as sticky notes. Readable at 2× and above; the
 *  strip under the stage carries the full text at any size. */
function drawBoard(ctx: CanvasRenderingContext2D, notes: BoardNote[], fresh: ReadonlySet<string>, t: number, motion: boolean) {
  const shown = notes.length > NOTE_MAX ? notes.slice(0, NOTE_MAX - 1) : notes
  ctx.save()
  ctx.font = 'bold 4px ui-monospace, monospace'
  ctx.textBaseline = 'top'
  shown.forEach((n, i) => {
    const x = 127 + i * (NOTE_W + 3)
    const y = 18
    const isNew = fresh.has(n.id)
    const lift = isNew && motion ? Math.round(Math.abs(Math.sin(t / 260)) * 1) : 0
    rect(ctx, x + 1, y + 1 - lift, NOTE_W, 27, 'rgba(0,0,0,0.18)')
    rect(ctx, x, y - lift, NOTE_W, 27, NOTE_COLOURS[i % NOTE_COLOURS.length])
    if (isNew) {
      ctx.strokeStyle = '#e63946'
      ctx.lineWidth = 1
      ctx.strokeRect(x + 0.5, y - lift + 0.5, NOTE_W - 1, 26)
    }
    rect(ctx, x + NOTE_W / 2 - 1, y - 1 - lift, 3, 3, '#e63946')
    ctx.fillStyle = '#1b1b1b'
    wrap(ctx, n.statement, NOTE_W - 4, 5).forEach((line, j) => ctx.fillText(line, x + 2, y + 3 + j * 4.6 - lift))
  })
  if (notes.length > NOTE_MAX) {
    const x = 127 + (NOTE_MAX - 1) * (NOTE_W + 3)
    rect(ctx, x, 18, NOTE_W, 27, '#e8e8e8')
    ctx.fillStyle = '#1b1b1b'
    ctx.font = 'bold 6px ui-monospace, monospace'
    ctx.textAlign = 'center'
    ctx.fillText(`+${notes.length - NOTE_MAX + 1}`, x + NOTE_W / 2, 26)
  }
  ctx.restore()
}

/** Frame index for an idle actor: two frames, slow, offset per actor so nobody breathes in step. */
function idleFrame(sprite: string, t: number, i: number): number {
  if (STATIC_IDLE.has(sprite)) return 0
  return Math.floor((t + i * 377) / 1100) % 2
}

/** In front of the table or behind it, by where the feet are. */
const behindTable = (p: Pose) => p.y < TABLE.y + TABLE.h / 2

function drawActor(ctx: CanvasRenderingContext2D, img: HTMLImageElement | undefined, a: Actor, i: number, f: SceneFrame) {
  const { pose } = a
  if (!pose.visible) return
  const active = f.speaker === a.key
  const lift = active && !pose.walking && f.motion ? Math.round(Math.abs(Math.sin(f.t / 170)) * 2) : 0
  if (!behindTable(pose)) {
    ctx.fillStyle = C.shadow
    ctx.beginPath()
    ctx.ellipse(pose.x, pose.y, 11, 4, 0, 0, Math.PI * 2)
    ctx.fill()
  }
  if (!img || !img.complete || !img.naturalWidth) return
  const row = ROW[pose.facing] + (pose.walking ? 4 : 0)
  const frame = pose.walking ? (f.motion ? Math.floor(f.t / 140) % 4 : 0) : idleFrame(a.sprite, f.t, i)
  ctx.drawImage(img, frame * FRAME, row * FRAME, FRAME, FRAME,
    Math.round(pose.x) - FOOT_X, Math.round(pose.y) - FOOT_Y - lift, FRAME, FRAME)
}

/**
 * The simulated-persona marker as pixels: a robot head five wide and five tall (antenna, head, eyes, chin), drawn
 * in the tag's ink. The app's marker is a vector icon; on the stage everything is pixel art at 1×, where a
 * scaled-down vector would blur into a smudge, so the same robot is drawn on the room's own grid.
 */
const BOT_PIXELS = ['..#..', '#####', '#.#.#', '#####', '.###.']
const BOT_W = 5
const BOT_GAP = 2

function drawBotMark(ctx: CanvasRenderingContext2D, x: number, y: number) {
  BOT_PIXELS.forEach((row, j) => {
    for (let i = 0; i < row.length; i++) if (row[i] === '#') ctx.fillRect(x + i, y + j, 1, 1)
  })
}

/** Exported for its test: jsdom has no canvas, so the tag is checked against a recording context. */
export function drawNameTag(ctx: CanvasRenderingContext2D, a: Actor, active: boolean) {
  const first = a.label.toUpperCase()
  ctx.save()
  ctx.font = `bold ${active ? 7 : 6}px ui-monospace, monospace`
  const text = Math.ceil(ctx.measureText(first).width)
  const mark = a.bot ? BOT_W + BOT_GAP : 0
  const w = text + mark + 6
  // Behind the table the tag goes above the head; in front, below the feet.
  const y = behindTable(a.pose) ? a.pose.y - 44 : a.pose.y + 4
  const left = Math.round(a.pose.x - w / 2)
  rect(ctx, left, y, w, 9, active ? C.tagOn : C.tag)
  ctx.fillStyle = active ? '#141414' : C.ink
  if (a.bot) drawBotMark(ctx, left + 3, y + 2)
  ctx.textAlign = 'center'
  ctx.textBaseline = 'middle'
  // Centred in what is left of the tag once the marker has its place.
  ctx.fillText(first, Math.round(left + 3 + mark + text / 2), y + 5)
  ctx.restore()
}

const headTop = (p: Pose) => p.y - 36
/** Where icons over an actor sit: above the head, or above the name tag where the tag is there. */
const markTop = (p: Pose) => (behindTable(p) ? p.y - 44 - 17 : headTop(p) - 14)

/** From `emotes.png`: row 1 is the "…" bubble growing, row 2 the "!" popping. */
function drawEmote(ctx: CanvasRenderingContext2D, emotes: HTMLImageElement | undefined, row: number, frame: number, x: number, y: number) {
  if (!emotes || !emotes.complete || !emotes.naturalWidth) return
  ctx.drawImage(emotes, frame * 16, row * 16, 16, 16, Math.round(x), Math.round(y), 16, 16)
}

function drawBook(ctx: CanvasRenderingContext2D, x: number, y: number) {
  rect(ctx, x, y + 1, 12, 8, '#6b3f1d')
  rect(ctx, x + 1, y, 5, 8, C.ink)
  rect(ctx, x + 6, y, 5, 8, '#e8e2d0')
  rect(ctx, x + 2, y + 2, 3, 1, '#9aa3b8')
  rect(ctx, x + 2, y + 4, 3, 1, '#9aa3b8')
  rect(ctx, x + 7, y + 2, 3, 1, '#9aa3b8')
}

function drawLetter(ctx: CanvasRenderingContext2D, x: number, y: number) {
  rect(ctx, x, y, 12, 8, C.ink)
  ctx.strokeStyle = '#9aa3b8'
  ctx.lineWidth = 1
  ctx.beginPath()
  ctx.moveTo(x + 0.5, y + 0.5)
  ctx.lineTo(x + 6, y + 5)
  ctx.lineTo(x + 11.5, y + 0.5)
  ctx.stroke()
  rect(ctx, x + 5, y + 4, 2, 2, '#e63946')
}

function drawMarks(ctx: CanvasRenderingContext2D, emotes: HTMLImageElement | undefined, a: Actor, f: SceneFrame) {
  if (!a.pose.visible || f.markT < 0) return
  const top = markTop(a.pose)
  for (const m of a.marks) {
    if (m === 'shift') {
      const frame = f.motion && f.markT < 360 ? 4 + Math.floor(f.markT / 90) : 7
      drawEmote(ctx, emotes, 2, frame, a.pose.x - 20, top)
    } else if (m === 'book') {
      drawBook(ctx, a.pose.x - 6, top + (a.marks.includes('shift') ? -12 : 2))
    } else if (m === 'letter' && !a.pose.walking) {
      drawLetter(ctx, a.pose.x - 6, top + 2)
    } else if (m === 'ask') {
      // The "?" bubble, on the same side as the speech bubble: whoever asked the consultant.
      drawEmote(ctx, emotes, 3, 7, a.pose.x + 4, top)
    }
  }
}

export function drawScene(ctx: CanvasRenderingContext2D, images: SpriteImages, f: SceneFrame) {
  ctx.clearRect(0, 0, ROOM_W, ROOM_H)
  drawRoom(ctx)
  drawBoard(ctx, f.board, f.fresh, f.t, f.motion)
  const indexed = f.actors.map((a, i) => [a, i] as const)
  const back = indexed.filter(([a]) => behindTable(a.pose)).sort(([a], [b]) => a.pose.y - b.pose.y)
  const front = indexed.filter(([a]) => !behindTable(a.pose)).sort(([a], [b]) => a.pose.y - b.pose.y)
  for (const s of f.seats) if (s.behindTable) drawChairBack(ctx, s)
  for (const [a, i] of back) drawActor(ctx, images[a.sprite], a, i, f)
  drawTable(ctx)
  for (const s of f.seats) if (!s.behindTable) drawChairSeat(ctx, s)
  for (const [a, i] of front) drawActor(ctx, images[a.sprite], a, i, f)
  for (const a of f.actors) {
    const active = a.key === f.speaker
    // Not while they are still in the doorway: a tag half outside the room reads as a glitch.
    const inside = a.pose.x > 24 && a.pose.x < ROOM_W - 24
    if (a.pose.visible && inside && (f.allNames || active)) drawNameTag(ctx, a, active)
  }
  for (const a of f.actors) drawMarks(ctx, images.emotes, a, f)
  const speaking = f.actors.find((a) => a.key === f.speaker && a.pose.visible && !a.pose.walking)
  if (speaking && f.typing) {
    const frame = 4 + (Math.floor(f.t / 220) % 4)
    drawEmote(ctx, images.emotes, 1, frame, speaking.pose.x + 4, markTop(speaking.pose))
  }
}
