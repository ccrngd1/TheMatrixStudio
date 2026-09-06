// SPDX-License-Identifier: Apache-2.0
/**
 * The line format is the authoring surface for convictions, so a parsing mistake
 * silently changes what a persona will defend. These lock the shape, and
 * particularly the failure modes — a typo must not delete the operator's text.
 */
import { describe, expect, it } from 'vitest'
import { buildStructured, parseList, parsePositions } from './convictions'

describe('parsePositions', () => {
  it('defaults to negotiable when no firmness is tagged', () => {
    // The safe default: a position wrongly negotiable just gets argued normally,
    // whereas one wrongly firm becomes an immovable wall.
    expect(parsePositions('Ship this quarter')).toEqual([
      { position: 'Ship this quarter', firmness: 'negotiable' },
    ])
  })

  it('reads every firmness level', () => {
    const out = parsePositions(
      '[firm] a\n[non-negotiable] b\n[requires-escalation] c\n[negotiable] d',
    )
    expect(out.map((v) => v.firmness)).toEqual([
      'firm', 'non-negotiable', 'requires-escalation', 'negotiable',
    ])
    expect(out.map((v) => v.position)).toEqual(['a', 'b', 'c', 'd'])
  })

  it('is case-insensitive about the tag', () => {
    expect(parsePositions('[FIRM] x')[0].firmness).toBe('firm')
  })

  it('keeps an unrecognised tag as part of the text instead of dropping it', () => {
    // A typo must not silently delete what the operator wrote.
    const out = parsePositions('[rock solid] Security signs off first')
    expect(out[0].position).toBe('[rock solid] Security signs off first')
    expect(out[0].firmness).toBe('negotiable')
  })

  it('reads the exit condition after ->, and splits several on semicolons', () => {
    const out = parsePositions('[firm] no external service -> a file index; a clean install')
    expect(out[0].position).toBe('no external service')
    expect(out[0].evidence_that_shifts).toEqual(['a file index', 'a clean install'])
  })

  it('omits evidence_that_shifts entirely when none is given', () => {
    // Rather than an empty array: a defended position with no exit condition is
    // flagged as unfalsifiable downstream, and [] must reach that check truthfully.
    expect(parsePositions('[firm] x')[0]).not.toHaveProperty('evidence_that_shifts')
  })

  it('ignores blank lines and whitespace-only positions', () => {
    expect(parsePositions('\n  \na\n\n[firm]   \nb\n')).toHaveLength(2)
  })
})

describe('buildStructured', () => {
  it('returns undefined when the operator authored nothing', () => {
    // An empty `structured` block would imply the feature was configured when it
    // was not, and the backend treats empty as absent anyway.
    expect(buildStructured({ positions: '', dismisses: '' })).toBeUndefined()
    expect(buildStructured({ positions: '  \n ', dismisses: '\n' })).toBeUndefined()
  })

  it('includes dismisses only when present', () => {
    expect(buildStructured({ positions: 'a', dismisses: '' })).toEqual({
      viewpoints: [{ position: 'a', firmness: 'negotiable' }],
    })
    expect(buildStructured({ positions: 'a', dismisses: 'cost\nschedule' })).toEqual({
      viewpoints: [{ position: 'a', firmness: 'negotiable' }],
      preferences: { dismisses: ['cost', 'schedule'] },
    })
  })

  it('builds from dismisses alone', () => {
    const out = buildStructured({ positions: '', dismisses: 'cost' })
    expect(out).toEqual({ viewpoints: [], preferences: { dismisses: ['cost'] } })
  })

  it('never emits underlying_concern or validity', () => {
    // Neither is authorable here by design: the withheld concern changes what a
    // persona says when pressed, and offering it on a casual form would invite
    // filling it in without realising that.
    const flat = JSON.stringify(
      buildStructured({ positions: '[firm] x -> y', dismisses: 'z' }),
    )
    expect(flat).not.toMatch(/underlying_concern|validity/)
  })
})

describe('parseList', () => {
  it('splits on newlines and semicolons and drops blanks', () => {
    expect(parseList('a\nb; c\n\n ; d ')).toEqual(['a', 'b', 'c', 'd'])
  })
})
