// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from 'vitest'
import { MESSENGER, MESSENGER_SPOT, computeBlocking, pathToSmeDoor, poseAt, walkMs } from './blocking'
import { ROOM_W, TABLE, buildScript, seatLayout, type Seat } from './script'
import type { FeedMessage } from '../types'

const cast = ['Ana', 'Ben', 'Cy', 'Di']
const layout = seatLayout(cast.length)
const seats: Record<string, Seat> = Object.fromEntries(cast.map((n, i) => [n, layout[i]]))
let seq = 0
const say = (speaker: string, content = `${speaker} speaks.`): FeedMessage => ({ turn: 1, seq: seq++, speaker, content })
const answer = (askedBy: string): FeedMessage => ({
  turn: 1, seq: seq++, speaker: 'Lawyer (consultant)', content: 'Not in my sources.',
  consultant: { expert: 'Lawyer', askedBy, question: 'Can we?' },
})
const letter = (): FeedMessage => ({ turn: 1, seq: seq++, speaker: 'Customer email', content: 'Renewal is next month.', injected: true })
const script = (feed: FeedMessage[]) => buildScript(feed, { cast })
const block = (feed: FeedMessage[], i: number) => computeBlocking(script(feed), i, seats)
const end = (b: ReturnType<typeof block>) => b.settleMs + 1

describe('computeBlocking: nothing moves without a cause', () => {
  it('keeps everyone seated through ordinary speech', () => {
    const b = block([say('Ana'), say('Ben')], 1)
    expect(b.moves).toEqual([])
    expect(b.preMs).toBe(0)
    for (const n of cast) expect(poseAt(b, n, seats[n], 5000)).toMatchObject({ x: seats[n].x, y: seats[n].y, visible: true })
  })
  it('never shows the messenger without an outside voice', () => {
    expect(poseAt(block([say('Ana')], 0), MESSENGER, null, 0).visible).toBe(false)
  })
})

describe('computeBlocking: consulting', () => {
  const feed = () => [say('Ana'), answer('Ben'), say('Ben', 'So it cannot be done.'), say('Cy')]

  it('sends the persona who asked out through the SME door before the answer is read', () => {
    const b = block(feed(), 1)
    expect(b.moves.map((m) => m.actor)).toEqual(['Ben'])
    expect(b.preMs).toBeGreaterThan(0)
    const out = poseAt(b, 'Ben', seats.Ben, end(b))
    expect(out.visible).toBe(false)
    expect(out.x).toBeLessThan(0)
    // Everyone else stays put.
    expect(poseAt(b, 'Ana', seats.Ana, end(b))).toMatchObject({ x: seats.Ana.x, visible: true })
  })

  it('brings them back to their seat, and waits for them when they speak next', () => {
    const b = block(feed(), 2)
    expect(b.moves.map((m) => m.actor)).toEqual(['Ben'])
    expect(b.preMs).toBeGreaterThan(0)
    expect(poseAt(b, 'Ben', seats.Ben, 0).x).toBeLessThan(0)
    expect(poseAt(b, 'Ben', seats.Ben, end(b))).toMatchObject({ x: seats.Ben.x, y: seats.Ben.y, facing: seats.Ben.facing, visible: true })
  })

  it('does not hold up someone else\'s line while the asker walks back', () => {
    const b = block([say('Ana'), answer('Ben'), say('Cy')], 2)
    expect(b.moves.map((m) => m.actor)).toEqual(['Ben'])
    expect(b.preMs).toBe(0)
  })

  it('keeps an asker out across two answers in a row rather than walking them back and forth', () => {
    const b = block([answer('Ben'), answer('Ben')], 1)
    expect(b.moves).toEqual([])
    expect(poseAt(b, 'Ben', seats.Ben, 0).visible).toBe(false)
  })

  it('moves nobody when the asker is not in the cast', () => {
    const b = block([answer('Somebody else')], 0)
    expect(b.moves).toEqual([])
    expect(b.preMs).toBe(0)
  })

  it('stays out on the later pages of a long answer', () => {
    const long: FeedMessage = { ...answer('Ben'), content: 'A long answer. '.repeat(40).trim() }
    const beats = script([long])
    expect(beats.length).toBeGreaterThan(1)
    const b = computeBlocking(beats, 1, seats)
    expect(b.moves).toEqual([])
    expect(poseAt(b, 'Ben', seats.Ben, 0).visible).toBe(false)
  })
})

describe('computeBlocking: the messenger', () => {
  it('brings an outside voice in, and waits for them to arrive', () => {
    const b = block([say('Ana'), letter()], 1)
    expect(b.moves.map((m) => m.actor)).toEqual([MESSENGER])
    expect(b.preMs).toBeGreaterThan(0)
    expect(poseAt(b, MESSENGER, null, end(b))).toMatchObject({ ...MESSENGER_SPOT, visible: true })
  })
  it('sees them out when the next line starts, without holding it up', () => {
    const b = block([letter(), say('Ana')], 1)
    expect(b.preMs).toBe(0)
    expect(poseAt(b, MESSENGER, null, end(b)).visible).toBe(false)
  })
  it('sends no messenger for words injected under a cast member\'s name', () => {
    const b = block([{ ...say('Ana'), injected: true }], 0)
    expect(b.moves).toEqual([])
  })
})

describe('pathToSmeDoor', () => {
  it.each(seatLayout(10).map((s, i) => [i, s] as const))('leaves seat %i by right angles, inside the room until the door', (_, seat) => {
    const path = pathToSmeDoor(seat)
    for (let i = 1; i < path.length; i++) {
      expect(path[i].x === path[i - 1].x || path[i].y === path[i - 1].y).toBe(true)
    }
    for (const p of path.slice(0, -1)) expect(p.x).toBeGreaterThanOrEqual(0)
    expect(path[path.length - 1].x).toBeLessThan(0)
    // The walk never crosses the tabletop. The seat itself is exempt: a persona seated behind the
    // table has their feet behind its edge, which is how they come to look seated behind it.
    for (const p of path.slice(1)) {
      const onTable = p.x > TABLE.x && p.x < TABLE.x + TABLE.w && p.y > TABLE.y && p.y < TABLE.y + TABLE.h
      expect(onTable).toBe(false)
    }
    expect(walkMs(path)).toBeLessThan(((ROOM_W * 2) / 0.075))
  })
})
