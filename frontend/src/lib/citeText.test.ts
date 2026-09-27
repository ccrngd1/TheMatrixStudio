// SPDX-License-Identifier: Apache-2.0
//
// Matching citations against the labels of sources the run actually retrieved. The failure worth
// pinning is the one `citations.py` records: a pattern that guesses at titles swallows the words
// before them, or misses a title with spaces in it.
import { describe, expect, it } from 'vitest'
import { citeSegments, unsourcedCitations } from './citeText'
import type { SourcePassage } from '../types'

const p = (title: string, ordinal: number, id = title): SourcePassage => ({
  chunk_id: ordinal + title.length * 100, document_id: id, title, ordinal,
})
const cites = (segs: ReturnType<typeof citeSegments>) =>
  segs.flatMap((s) => ('passage' in s ? [s] : []))

describe('citeSegments', () => {
  it('links a citation to the passage it names', () => {
    const own = [p('spec.md', 3)]
    const segs = citeSegments('As spec.md #3 says, no.', own, {}, undefined)
    expect(cites(segs)).toHaveLength(1)
    expect(cites(segs)[0]).toMatchObject({ cite: 'spec.md #3', own: true })
    expect(segs.map((s) => ('text' in s ? s.text : s.cite)).join('')).toBe('As spec.md #3 says, no.')
  })

  it('matches a title with spaces exactly, without swallowing the words before it', () => {
    const own = [p('Iowa Admin Code Ch. 811', 1)]
    const segs = citeSegments('Per Iowa Admin Code Ch. 811 #1, an exam.', own, {}, undefined)
    expect(cites(segs)[0].cite).toBe('Iowa Admin Code Ch. 811 #1')
    expect(segs[0]).toEqual({ text: 'Per ' })
  })

  it("opens another participant's source, and says it was not the speaker's own", () => {
    const index = { 'report.pdf #2': p('report.pdf', 2) }
    const segs = citeSegments('Casey cited report.pdf #2 as saying so.', [], index, [
      { label: 'report.pdf #2', title: 'report.pdf', kind: 'secondhand', attributive: true, via: 'Casey' },
    ])
    expect(cites(segs)[0]).toMatchObject({ own: false, mark: { kind: 'secondhand', via: 'Casey' } })
  })

  it('leaves text alone when it names no retrieved source', () => {
    expect(citeSegments('Nothing cited here.', [p('spec.md', 1)], {}, undefined)).toEqual([
      { text: 'Nothing cited here.' },
    ])
  })

  it('does not match inside a longer word, or a short bare title in prose', () => {
    const own = [p('spec.md', 1), p('faq', 0)]
    expect(cites(citeSegments('see respec.md and the faq', own, {}, undefined))).toHaveLength(0)
    // With an ordinal it is unambiguous, however short the title.
    expect(cites(citeSegments('see faq #0', own, {}, undefined))).toHaveLength(1)
  })
})

describe('unsourcedCitations', () => {
  it('lists attributive citations of documents nobody retrieved', () => {
    const marks = [
      { label: 'ghost.md #1', title: 'ghost.md', kind: 'unverified', attributive: true },
      { label: 'spec.md #1', title: 'spec.md', kind: 'unverified', attributive: true },
      { label: 'x.md', title: 'x.md', kind: 'mention', attributive: false },
    ]
    const out = unsourcedCitations(marks, { 'spec.md #1': p('spec.md', 1) })
    expect(out.map((c) => c.title)).toEqual(['ghost.md'])
  })
})
