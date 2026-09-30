// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from 'vitest'
import {
  MESSENGER, MESSENGER_SPOT, SME_PREFIX, SME_SPOT, computeBlocking, pathFromLibDoor, poseAt,
  visitorsIn, walkMs,
} from './blocking'
import { ROOM_H, ROOM_W, TABLE, buildScript, researchPrologue, seatLayout, type Seat } from './script'
import type { FeedMessage, ResearchRecord } from '../types'

const cast = ['Ana', 'Ben', 'Cy', 'Di']
const layout = seatLayout(cast.length)
const seats: Record<string, Seat> = Object.fromEntries(cast.map((n, i) => [n, layout[i]]))
let seq = 0
const say = (speaker: string, content = `${speaker} speaks.`): FeedMessage => ({ turn: 1, seq: seq++, speaker, content })
const answer = (askedBy: string, expert = 'Lawyer'): FeedMessage => ({
  turn: 1, seq: seq++, speaker: `${expert} (consultant)`, content: 'Not in my sources.',
  consultant: { expert, askedBy, question: 'Can we?' },
})
const letter = (): FeedMessage => ({ turn: 1, seq: seq++, speaker: 'Customer email', content: 'Renewal is next month.', injected: true })
const script = (feed: FeedMessage[]) => buildScript(feed, { cast })
const block = (feed: FeedMessage[], i: number) => computeBlocking(script(feed), i, seats)
const end = (b: ReturnType<typeof block>) => b.settleMs + 1
const LAWYER = `${SME_PREFIX}Lawyer`

describe('computeBlocking: nothing moves without a cause', () => {
  it('keeps everyone seated through ordinary speech', () => {
    const b = block([say('Ana'), say('Ben')], 1)
    expect(b.moves).toEqual([])
    expect(b.preMs).toBe(0)
    for (const n of cast) expect(poseAt(b, n, seats[n], 5000)).toMatchObject({ x: seats[n].x, y: seats[n].y, visible: true })
  })
  it('never shows a visitor who has not been brought in', () => {
    const b = block([say('Ana')], 0)
    expect(poseAt(b, MESSENGER, null, 0).visible).toBe(false)
    expect(poseAt(b, LAWYER, null, 0).visible).toBe(false)
  })
})

describe('computeBlocking: a consultant comes to the room', () => {
  const feed = () => [say('Ana'), answer('Ben'), say('Ben', 'So it cannot be done.'), say('Cy')]

  it('walks the consultant in before the answer is read, and leaves the asker seated', () => {
    const b = block(feed(), 1)
    expect(b.moves.map((m) => m.actor)).toEqual([LAWYER])
    expect(b.preMs).toBeGreaterThan(0)
    expect(poseAt(b, LAWYER, null, 0)).toMatchObject({ visible: true, walking: true })
    expect(poseAt(b, LAWYER, null, end(b))).toMatchObject({ ...SME_SPOT, facing: 'right', visible: true, walking: false })
    // The whole cast, the asker included, stay where they were.
    for (const n of cast) expect(poseAt(b, n, seats[n], end(b))).toMatchObject({ x: seats[n].x, y: seats[n].y, visible: true })
  })

  it('sees the consultant out when the next line starts, without holding it up', () => {
    const b = block(feed(), 2)
    expect(b.moves.map((m) => m.actor)).toEqual([LAWYER])
    expect(b.preMs).toBe(0)
    expect(poseAt(b, LAWYER, null, 0)).toMatchObject({ ...SME_SPOT, walking: true })
    expect(poseAt(b, LAWYER, null, end(b)).visible).toBe(false)
  })

  it('keeps one consultant standing across two of their answers in a row', () => {
    const b = block([answer('Ben'), answer('Cy')], 1)
    expect(b.moves).toEqual([])
    expect(b.holding).toEqual([LAWYER])
    expect(poseAt(b, LAWYER, null, 0)).toMatchObject({ ...SME_SPOT, visible: true, walking: false })
  })

  it('swaps one consultant for another: the first leaves as the second arrives', () => {
    const b = block([answer('Ben', 'Lawyer'), answer('Cy', 'Economist')], 1)
    expect(b.moves.map((m) => m.actor).sort()).toEqual([`${SME_PREFIX}Economist`, LAWYER].sort())
    expect(poseAt(b, LAWYER, null, end(b)).visible).toBe(false)
    expect(poseAt(b, `${SME_PREFIX}Economist`, null, end(b))).toMatchObject({ ...SME_SPOT, visible: true })
  })

  it('still stages the answer when the asker is not in the cast', () => {
    const b = block([answer('Somebody else')], 0)
    expect(b.moves.map((m) => m.actor)).toEqual([LAWYER])
  })

  it('holds the consultant in place on the later pages of a long answer', () => {
    const long: FeedMessage = { ...answer('Ben'), content: 'A long answer. '.repeat(40).trim() }
    const beats = script([long])
    expect(beats.length).toBeGreaterThan(1)
    const b = computeBlocking(beats, 1, seats)
    expect(b.moves).toEqual([])
    expect(poseAt(b, LAWYER, null, 0)).toMatchObject({ ...SME_SPOT, visible: true })
  })

  it('gives every visitor in a script one identity, so each can have its own sprite', () => {
    const vs = visitorsIn(script([answer('Ben', 'Lawyer'), letter(), answer('Cy', 'Lawyer'), answer('Di', 'Economist')]))
    expect(vs.map((v) => v.key)).toEqual([LAWYER, MESSENGER, `${SME_PREFIX}Economist`])
    expect(vs.map((v) => v.label)).toEqual(['Lawyer', 'Customer email', 'Economist'])
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
  it('lets a consultant and a messenger be in the room on consecutive lines', () => {
    const b = block([answer('Ben'), letter()], 1)
    expect(b.moves.map((m) => m.actor).sort()).toEqual([MESSENGER, LAWYER].sort())
    expect(poseAt(b, LAWYER, null, end(b)).visible).toBe(false)
    expect(poseAt(b, MESSENGER, null, end(b))).toMatchObject({ ...MESSENGER_SPOT, visible: true })
  })
})

describe('computeBlocking: the research prologue', () => {
  const research: ResearchRecord = {
    status: 'researched',
    scopes: [{ scope: 'shared', documents: 6, controlling: 2 }, { scope: 'Ana', documents: 3 }],
  }
  const feed = [say('Ana'), say('Ben')]
  const withPrologue = () => buildScript(feed, { cast, research })

  it('walks every persona in from the library door to their own seat', () => {
    const beats = withPrologue()
    expect(beats[0].kind).toBe('prologue')
    const b = computeBlocking(beats, 0, seats)
    expect(b.moves.map((m) => m.actor).sort()).toEqual([...cast].sort())
    for (const n of cast) {
      expect(poseAt(b, n, seats[n], 0)).toMatchObject({ visible: true, walking: true })
      expect(poseAt(b, n, seats[n], end(b))).toMatchObject({ x: seats[n].x, y: seats[n].y, facing: seats[n].facing, walking: false })
    }
  })

  it('files them in one behind another, not all out of the doorway at once', () => {
    const b = computeBlocking(withPrologue(), 0, seats)
    const starts = b.moves.map((m) => m.startMs ?? 0).sort((x, y) => x - y)
    expect(new Set(starts).size).toBe(cast.length)
    // Each is still walking when the next sets off, so they overlap as a queue rather than a blob.
    expect(starts[1] - starts[0]).toBeGreaterThan(0)
    expect(b.settleMs).toBeGreaterThan(Math.max(...b.moves.map((m) => walkMs(m.path))))
    // Nobody has moved before their turn to set off.
    const last = b.moves.reduce((a2, c) => ((a2.startMs ?? 0) > (c.startMs ?? 0) ? a2 : c))
    expect(poseAt(b, last.actor, seats[last.actor], 1)).toMatchObject({ x: last.path[0].x, y: last.path[0].y })
  })

  it('reads its caption while they walk, rather than after', () => {
    const b = computeBlocking(withPrologue(), 0, seats)
    expect(b.preMs).toBe(0)
    expect(b.settleMs).toBeGreaterThan(0)
  })

  it('is over by the first line: everyone is seated and nothing is walking', () => {
    const beats = withPrologue()
    const b = computeBlocking(beats, 1, seats)
    expect(b.moves).toEqual([])
    for (const n of cast) expect(poseAt(b, n, seats[n], 0)).toMatchObject({ x: seats[n].x, walking: false, visible: true })
  })
})

describe('researchPrologue', () => {
  it('counts the run\'s own record: sources, controlling authority and who brought what', () => {
    const p = researchPrologue({
      status: 'researched',
      scopes: [{ scope: 'shared', documents: 6, controlling: 2 }, { scope: 'Ana Silva', documents: 3 }],
    }, 7)
    expect(p?.text).toBe('Before the room met, it read up: 9 sources, 2 of them controlling authority. 6 went to a corpus everyone can see. Ana Silva brought 3.')
    expect(p).toMatchObject({ kind: 'prologue', line: -1, seq: 7, turn: 0 })
  })
  it('is nothing at all for a run that did not research, found nothing, or failed', () => {
    expect(researchPrologue(null, 0)).toBeNull()
    expect(researchPrologue(undefined, 0)).toBeNull()
    expect(researchPrologue({ status: 'skipped' }, 0)).toBeNull()
    expect(researchPrologue({ status: 'failed', error: 'boom' }, 0)).toBeNull()
    expect(researchPrologue({ status: 'found-nothing', scopes: [] }, 0)).toBeNull()
    // "Researched" but every scope empty is the same fact as finding nothing.
    expect(researchPrologue({ status: 'researched', scopes: [{ scope: 'shared', documents: 0 }] }, 0)).toBeNull()
  })
  it('counts only what the room holds, and names a consultant\'s library as outside it', () => {
    const p = researchPrologue({
      status: 'researched',
      scopes: [{ scope: 'shared', documents: 4 }, { scope: 'Lawyer', consultant: true, documents: 50 }],
    }, 0)
    // Not 54: a consultant answers only from its own library and is not in the room.
    expect(p?.text).toContain('4 sources')
    expect(p?.text).toContain('Outside the room, a consultant keeps a library of 50.')
    expect(p?.text).not.toContain('Lawyer brought')
  })

  it('is nothing when only a consultant researched, because the room read nothing', () => {
    expect(researchPrologue({
      status: 'researched', scopes: [{ scope: 'Lawyer', consultant: true, documents: 50 }],
    }, 0)).toBeNull()
  })
})

describe('pathFromLibDoor', () => {
  it.each(seatLayout(10).map((s, i) => [i, s] as const))('reaches seat %i by right angles, never over the table', (_, seat) => {
    const path = pathFromLibDoor(seat)
    for (let i = 1; i < path.length; i++) {
      expect(path[i].x === path[i - 1].x || path[i].y === path[i - 1].y).toBe(true)
    }
    expect(path[0].x).toBeGreaterThan(ROOM_W)
    expect(path[path.length - 1]).toMatchObject({ x: seat.x, y: seat.y })
    // The seat itself is exempt: a persona seated behind the table has their feet behind its edge.
    for (const p of path.slice(0, -1)) {
      const onTable = p.x > TABLE.x && p.x < TABLE.x + TABLE.w && p.y > TABLE.y && p.y < TABLE.y + TABLE.h
      expect(onTable).toBe(false)
    }
    expect(walkMs(path)).toBeLessThan((ROOM_W * 3) / 0.075)
    expect(path.every((p) => p.y > 0 && p.y < ROOM_H)).toBe(true)
  })
})
