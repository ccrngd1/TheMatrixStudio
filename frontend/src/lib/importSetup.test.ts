// SPDX-License-Identifier: Apache-2.0
/**
 * Setup-import tests, anchored on real files rather than invented fixtures.
 *
 * `examples/import-minimal.json` is a verbatim copy of the operator's own bare setup:
 * long prose personas carrying embedded direction, `goals: []`, no config, eight of
 * them. It is what surfaced that empty goals arrays and absent config are the NORMAL
 * case rather than the edge case.
 *
 * `examples/import-augmented.json` is the same cast with convictions, withheld
 * concerns, differing dismisses and cognition on — the reference for the full format.
 *
 * Both live in `examples/` deliberately. An earlier version read from
 * `data/sampleImport.json`, which is gitignored, so the suite would have failed on a
 * fresh clone — the fixtures have to be tracked to be fixtures.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { ImportError, parseSetup } from './importSetup'

const SAMPLE = readFileSync(join(__dirname, '../../../examples/import-minimal.json'), 'utf8')

describe('the minimal setup (bare shape)', () => {
  it('loads every persona with its prose intact', () => {
    const setup = parseSetup(SAMPLE)
    expect(setup.cast).toHaveLength(8)
    expect(setup.warnings).toEqual([])
    expect(setup.cast.map((c) => c.name)).toContain('Dr. Marcus Webb')
    // The prose carries embedded direction ("Keep responses to 2-4 paragraphs") and
    // must survive verbatim — it is the persona, not metadata to strip.
    expect(setup.cast[1].persona).toMatch(/board-certified veterinary criticalist/)
    expect(setup.cast[1].persona).toMatch(/Keep responses to 2-4 paragraphs/)
  })

  it('keeps the topic verbatim', () => {
    const setup = parseSetup(SAMPLE)
    expect(setup.topic).toMatch(/veterinary diet authorization lapses/)
  })

  it('handles empty goals arrays, which is what the sample actually has', () => {
    const setup = parseSetup(SAMPLE)
    expect(setup.cast.every((c) => c.goals === '')).toBe(true)
  })

  it('leaves cognition alone when the file says nothing about it', () => {
    // Absent must not mean "off": a setup written before cognition existed should not
    // silently disable it and rob the operator of the dossier.
    expect(parseSetup(SAMPLE).cognition).toBeUndefined()
  })

  it('produces a cast the convictions builder accepts unchanged', async () => {
    const { buildStructured } = await import('./convictions')
    const setup = parseSetup(SAMPLE)
    // No convictions authored, so no structured block — not an empty one.
    expect(buildStructured(setup.cast[0])).toBeUndefined()
  })
})

describe('augmented fields', () => {
  const augmented = JSON.stringify({
    topic: 'Should we migrate?',
    name: 'kibble-bridge',
    description: 'a vet diet question',
    config: {
      max_messages: 24,
      cognition: { enabled: true, memory: true, reflection_every: 4, goals_dynamic: true },
    },
    cast: [
      {
        name: 'Dana',
        persona: 'Cautious.',
        goals: ['Protect customers', 'Keep the install simple'],
        structured: {
          preferences: { dismisses: ['revenue targets', 'schedule'] },
          viewpoints: [
            {
              position: 'No migration without an opt-out',
              firmness: 'firm',
              evidence_that_shifts: ['a zero-disruption pilot', 'written sign-off'],
              underlying_concern: 'I get blamed when a migration loses a customer',
            },
            { position: 'Ship something this quarter', firmness: 'negotiable' },
          ],
        },
        document_texts: [{ title: 'policy.md', text: 'Consent is required.' }],
      },
    ],
  })

  it('renders convictions back into the editable line format', () => {
    const c = parseSetup(augmented).cast[0]
    expect(c.positions).toBe(
      '[firm] No migration without an opt-out -> a zero-disruption pilot; written sign-off\n' +
        '[negotiable] Ship something this quarter',
    )
  })

  it('keeps concerns index-aligned with positions, blanks included', () => {
    // The second viewpoint has no concern. If the blank collapsed, Dana's private
    // worry would attach to "Ship something this quarter" — the wrong stance.
    const c = parseSetup(augmented).cast[0]
    expect(c.concerns).toBe('I get blamed when a migration loses a customer\n')
  })

  it('loads dismisses, goals, documents, name, description and config', () => {
    const s = parseSetup(augmented)
    expect(s.cast[0].dismisses).toBe('revenue targets\nschedule')
    expect(s.cast[0].goals).toBe('Protect customers\nKeep the install simple')
    expect(s.cast[0].documents).toEqual([{ title: 'policy.md', text: 'Consent is required.' }])
    expect(s.name).toBe('kibble-bridge')
    expect(s.description).toBe('a vet diet question')
    expect(s.maxMessages).toBe(24)
    expect(s.cognition).toEqual({
      enabled: true, memory: true, reflection: true, goals_dynamic: true, relationships: false,
    })
  })

  it('round-trips convictions through the form back into a payload', async () => {
    const { buildStructured } = await import('./convictions')
    const built = buildStructured(parseSetup(augmented).cast[0])
    expect(built!.viewpoints[0]).toEqual({
      position: 'No migration without an opt-out',
      firmness: 'firm',
      evidence_that_shifts: ['a zero-disruption pilot', 'written sign-off'],
      underlying_concern: 'I get blamed when a migration loses a customer',
    })
    expect(built!.viewpoints[1]).not.toHaveProperty('underlying_concern')
  })
})

describe('tolerance and honest reporting', () => {
  const base = { topic: 't', cast: [{ name: 'A', persona: 'p' }] }

  it('warns rather than silently dropping an unusable persona', () => {
    const s = parseSetup(JSON.stringify({
      ...base,
      cast: [{ name: 'A', persona: 'p' }, { name: 'B' }, { persona: 'no name' }],
    }))
    expect(s.cast.map((c) => c.name)).toEqual(['A'])
    expect(s.warnings).toHaveLength(2)
    expect(s.warnings[0]).toMatch(/"B".*skipped/)
  })

  it('warns about duplicate names instead of letting them collide at run start', () => {
    const s = parseSetup(JSON.stringify({
      ...base, cast: [{ name: 'A', persona: 'p' }, { name: 'a', persona: 'q' }],
    }))
    expect(s.cast).toHaveLength(1)
    expect(s.warnings[0]).toMatch(/appears more than once/)
  })

  it('warns that server document paths cannot be read by a browser', () => {
    // Silently discarding them would leave the operator wondering where the
    // background material went.
    const s = parseSetup(JSON.stringify({
      ...base, cast: [{ name: 'A', persona: 'p', documents: ['./bg/spec.pdf'] }],
    }))
    expect(s.warnings[0]).toMatch(/paths are only readable by the server/)
    expect(s.warnings[0]).toMatch(/spec\.pdf/)
  })

  it('accepts a string where a list was expected', () => {
    const s = parseSetup(JSON.stringify({
      ...base, cast: [{ name: 'A', persona: 'p', goals: 'one; two' }],
    }))
    expect(s.cast[0].goals).toBe('one\ntwo')
  })

  it('ignores a viewpoint with no position', () => {
    const s = parseSetup(JSON.stringify({
      ...base,
      cast: [{ name: 'A', persona: 'p', structured: { viewpoints: [{ firmness: 'firm' }] } }],
    }))
    expect(s.cast[0].positions).toBe('')
  })
})

describe('refusals', () => {
  it('names the JSON error rather than saying "invalid"', () => {
    expect(() => parseSetup('{not json')).toThrow(ImportError)
    expect(() => parseSetup('{not json')).toThrow(/not valid JSON/)
  })

  it.each([
    ['[]', /object with "topic" and "cast"/],
    ['{"cast":[{"name":"A","persona":"p"}]}', /needs a "topic"/],
    ['{"topic":"t"}', /non-empty "cast"/],
    ['{"topic":"t","cast":[]}', /non-empty "cast"/],
    ['{"topic":"t","cast":[{"name":"A"}]}', /every cast entry was skipped/],
  ])('refuses %s with a specific reason', (input, expected) => {
    expect(() => parseSetup(input)).toThrow(expected)
  })
})

describe('the shipped augmented example', () => {
  const AUGMENTED = readFileSync(
    join(__dirname, '../../../examples/import-augmented.json'),
    'utf8',
  )

  it('loads with no warnings — it is the reference for the augmented format', () => {
    const s = parseSetup(AUGMENTED)
    expect(s.warnings).toEqual([])
    expect(s.cast).toHaveLength(8)
  })

  it('carries convictions, concerns and dismisses for every persona', () => {
    for (const c of parseSetup(AUGMENTED).cast) {
      expect(c.positions, `${c.name} has no position`).not.toBe('')
      expect(c.concerns.trim(), `${c.name} has no concern`).not.toBe('')
      expect(c.dismisses, `${c.name} dismisses nothing`).not.toBe('')
    }
  })

  it('stays calibrated: the firmest positions are not uniformly the soundest', async () => {
    // Same property the hand-built validation cast asserts. If firmness tracked
    // correctness an operator could win by conceding to whoever pushed hardest.
    const { buildStructured } = await import('./convictions')
    const firm = parseSetup(AUGMENTED)
      .cast.flatMap((c) => buildStructured(c)!.viewpoints)
      .filter((v) => v.firmness !== 'negotiable')
    expect(firm.length).toBeGreaterThan(2)
    // A defended position with no exit condition is the deliberate "unfalsifiable"
    // case the UI flags in amber — the example includes one so that path is exercised.
    expect(firm.some((v) => !v.evidence_that_shifts)).toBe(true)
  })

  it('gives every persona a different set of things it will not weigh', () => {
    // Five people who weigh the same things are one person, and the panel converges.
    const sets = new Set(parseSetup(AUGMENTED).cast.map((c) => c.dismisses))
    expect(sets.size).toBe(8)
  })
})
