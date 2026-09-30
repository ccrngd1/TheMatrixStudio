// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from 'vitest'
import {
  ROOM_H, ROOM_W, SPRITE_IDS, TABLE, assignSprites, boardAt, buildScript, holdMs, paginate, seatLayout,
} from './script'
import type { FeedMessage, SimEvent } from '../types'

const msg = (turn: number, speaker: string, content: string, extra: Partial<FeedMessage> = {}): FeedMessage =>
  ({ turn, seq: turn * 10, speaker, content, ...extra })

describe('assignSprites', () => {
  it('gives a name the same sheet every time', () => {
    expect(assignSprites(['Ana', 'Ben'])).toEqual(assignSprites(['Ana', 'Ben']))
  })
  it('never shares a sheet within a cast while there are sheets to spare', () => {
    const names = Array.from({ length: SPRITE_IDS.length }, (_, i) => `Persona ${i}`)
    expect(new Set(Object.values(assignSprites(names))).size).toBe(SPRITE_IDS.length)
  })
  it('only hands out sheets that ship, never the two with broken walk cycles', () => {
    const got = Object.values(assignSprites(Array.from({ length: 60 }, (_, i) => `n${i}`)))
    for (const id of got) expect(SPRITE_IDS).toContain(id)
    expect(SPRITE_IDS).not.toContain('char17')
    expect(SPRITE_IDS).not.toContain('char23')
  })
})

describe('paginate', () => {
  it('leaves a short line alone', () => {
    expect(paginate('Short and sweet.')).toEqual(['Short and sweet.'])
  })
  it('breaks at sentence ends and loses no words', () => {
    const text = Array.from({ length: 12 }, (_, i) => `Sentence number ${i} says something useful.`).join(' ')
    const pages = paginate(text, 100)
    expect(pages.length).toBeGreaterThan(1)
    for (const p of pages) {
      expect(p.length).toBeLessThanOrEqual(100)
      expect(p.endsWith('.')).toBe(true)
    }
    expect(pages.join(' ')).toBe(text)
  })
  it('breaks one over-long sentence between words, never inside one', () => {
    const text = 'word '.repeat(80).trim()
    const pages = paginate(text, 50)
    for (const p of pages) expect(p.length).toBeLessThanOrEqual(50)
    expect(pages.join(' ')).toBe(text)
  })
})

describe('buildScript', () => {
  const feed = [
    msg(1, 'Ana', 'We should pilot it.'),
    msg(2, 'State DOT', 'A letter arrives.', { injected: true }),
    msg(2, 'Road pricing handbook', 'Off-peak discounts shift a fifth of deliveries.',
      { consultant: { expert: 'Traffic economist', askedBy: 'Ben', question: 'Do discounts work?' } }),
    msg(3, 'Ben', 'Then I can live with it.'),
  ]
  it('is the feed, in order, with nothing added or dropped', () => {
    const beats = buildScript(feed)
    expect(beats.map((b) => b.line)).toEqual([0, 1, 2, 3])
    expect(beats.map((b) => b.text)).toEqual(feed.map((m) => m.content))
  })
  it('keeps injected voices and consultants distinct from the cast', () => {
    const [, injected, consultant, speech] = buildScript(feed)
    expect(injected).toMatchObject({ kind: 'injected', speaker: 'State DOT' })
    expect(consultant).toMatchObject({ kind: 'consultant', speaker: 'Traffic economist', askedBy: 'Ben' })
    expect(speech).toMatchObject({ kind: 'speech', speaker: 'Ben', turn: 3 })
  })
  it('pages a long turn and every page still traces to its message', () => {
    const long = msg(4, 'Ana', 'This is a long point. '.repeat(30).trim())
    const beats = buildScript([long], { max: 120 })
    expect(beats.length).toBeGreaterThan(1)
    expect(beats.every((b) => b.line === 0 && b.pages === beats.length)).toBe(true)
    expect(beats.map((b) => b.page)).toEqual(beats.map((_, i) => i))
  })
})

describe('buildScript flags', () => {
  it('marks what the stage stages, and only from what the message recorded', () => {
    const feed = [
      msg(1, 'Ana', 'I moved.', { shift: { sentences: [], credits: [], conditions: [], matched_conditions: [], no_listed_condition: false } }),
      msg(2, 'Ben', 'Per the survey.', { sources: [{ chunk_id: 1, document_id: 'd', title: 'Survey', ordinal: 1 }] }),
      msg(3, 'Customer email', 'Renewal is next month.', { injected: true }),
      msg(4, 'Ana', 'Words put in my mouth.', { injected: true }),
      msg(5, 'Ben', 'Plain.'),
    ]
    const [shift, sourced, outsider, ventriloquised, plain] = buildScript(feed, { cast: ['Ana', 'Ben'] })
    expect(shift.shifted).toBe(true)
    expect(sourced.consulted).toBe(true)
    expect(outsider.outsider).toBe(true)
    expect(ventriloquised.outsider).toBeUndefined()
    expect(plain).not.toHaveProperty('shifted')
    expect(plain).not.toHaveProperty('consulted')
    expect(plain.seq).toBe(50)
  })
})

describe('boardAt', () => {
  const e = (seq: number, event_type: SimEvent['event_type'], payload: Record<string, unknown>): SimEvent =>
    ({ run_id: 'r', turn: 0, seq, event_type, agent_name: null, payload })
  const events = [
    e(1, 'assumption.made', { id: 'a', statement: 'Headcount stays flat' }),
    e(5, 'agent.response', { speaker: 'Ana', message: 'hi' }),
    e(7, 'assumption.made', { id: 'b', statement: 'Budget frozen until Q3' }),
    e(9, 'assumption.withdrawn', { id: 'a' }),
    e(12, 'assumption.made', { id: 'b', statement: 'Budget frozen until Q4' }),
  ]
  it('shows what was pinned at that point in the log, not at the end', () => {
    expect(boardAt(events, 0)).toEqual([])
    expect(boardAt(events, 5).map((n) => n.statement)).toEqual(['Headcount stays flat'])
    expect(boardAt(events, 8).map((n) => n.statement)).toEqual(['Headcount stays flat', 'Budget frozen until Q3'])
  })
  it('takes a withdrawal down and lets a restatement replace the note', () => {
    expect(boardAt(events, 10).map((n) => n.id)).toEqual(['b'])
    expect(boardAt(events, 99)).toEqual([{ id: 'b', statement: 'Budget frozen until Q4', since: 12 }])
  })
  it('does not depend on the events arriving in order', () => {
    expect(boardAt([...events].reverse(), 8)).toEqual(boardAt(events, 8))
  })
})

describe('holdMs', () => {
  it('holds a longer page longer', () => {
    expect(holdMs('a'.repeat(200))).toBeGreaterThan(holdMs('a'.repeat(20)))
  })
})

describe('seatLayout', () => {
  it.each([1, 2, 5, 8, 9, 10, 13])('seats %i inside the room, one seat each', (n) => {
    const seats = seatLayout(n)
    expect(seats).toHaveLength(n)
    for (const s of seats) {
      expect(s.x).toBeGreaterThan(0)
      expect(s.x).toBeLessThan(ROOM_W)
      expect(s.y).toBeGreaterThan(0)
      expect(s.y).toBeLessThan(ROOM_H)
    }
    expect(new Set(seats.map((s) => `${s.x},${s.y}`)).size).toBe(n)
  })
  it('faces the table: the far side looks down, the near side up', () => {
    const seats = seatLayout(6)
    for (const s of seats.filter((s) => s.y < TABLE.y + TABLE.h)) expect(s.facing).toBe('down')
    for (const s of seats.filter((s) => s.y > TABLE.y + TABLE.h)) expect(s.facing).toBe('up')
  })
  it('puts no more than four on a side', () => {
    const seats = seatLayout(13)
    const far = seats.filter((s) => s.y === TABLE.y + 6).length
    const near = seats.filter((s) => s.y === TABLE.y + TABLE.h + 30).length
    expect(far).toBeLessThanOrEqual(4)
    expect(near).toBeLessThanOrEqual(4)
  })
})
