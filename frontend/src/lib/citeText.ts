// SPDX-License-Identifier: Apache-2.0
//
// Find the source citations in a message so each can open the source it names.
//
// Personas are shown passages labelled `title #ordinal` and told to cite them that way
// (`format_documents_block` in retrieval.py). Titles can contain spaces — a researched page keeps its
// page title — so a pattern that guesses what a title looks like either misses those or swallows the
// words before them (`citations.py` records that exact failure). Instead this matches only the labels
// of sources the run actually retrieved, longest first, so the match is a real source or nothing.

import type { CitationMark, SourcePassage } from '../types'

export type Segment =
  | { text: string }
  | {
      /** The text as written, e.g. "spec.md #3". */
      cite: string
      passage: SourcePassage
      /** Whether the speaker had it in their own prompt, or it was surfaced by somebody else. */
      own: boolean
      /** The engine's verdict on it, when it recorded one. */
      mark?: CitationMark
    }

/** Titles shorter than this are not matched as bare words: "a" or "faq" would light up prose. */
const MIN_BARE_TITLE = 6

const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

export function citeSegments(
  content: string,
  sources: SourcePassage[] | undefined,
  index: Record<string, SourcePassage>,
  marks: CitationMark[] | undefined,
): Segment[] {
  const own = new Map<string, SourcePassage>()
  for (const p of sources ?? []) own.set(`${p.title} #${p.ordinal}`, p)
  const byTitle = new Map<string, SourcePassage>()
  // The speaker's own passages win a bare-title match: that is the copy they were looking at.
  for (const p of [...(sources ?? []), ...Object.values(index)]) {
    if (!byTitle.has(p.title)) byTitle.set(p.title, p)
  }
  const titles = [...byTitle.keys()].sort((a, b) => b.length - a.length)
  if (!titles.length || !content) return [{ text: content }]

  const re = new RegExp(`(${titles.map(esc).join('|')})(?:\\s*#\\s*(\\d+))?`, 'g')
  const out: Segment[] = []
  let last = 0
  for (const m of content.matchAll(re)) {
    const [whole, title, ordinal] = m
    const at = m.index ?? 0
    if (ordinal === undefined && title.length < MIN_BARE_TITLE) continue
    // Mid-word is not a citation: "respec.md" must not light up "spec.md".
    if (at > 0 && /[\w-]/.test(content[at - 1])) continue
    const label = ordinal !== undefined ? `${title} #${ordinal}` : null
    const passage = (label && (own.get(label) ?? index[label])) || byTitle.get(title)
    if (!passage) continue
    if (at > last) out.push({ text: content.slice(last, at) })
    out.push({
      cite: whole,
      passage,
      own: (label ? own.has(label) : false) || (sources ?? []).some((p) => p.title === title),
      mark: marks?.find((c) => c.title === title),
    })
    last = at + whole.length
  }
  if (last < content.length) out.push({ text: content.slice(last) })
  return out.length ? out : [{ text: content }]
}

/** Citations the engine found that name no source anyone in the run retrieved. */
export function unsourcedCitations(
  marks: CitationMark[] | undefined,
  index: Record<string, SourcePassage>,
): CitationMark[] {
  const known = new Set(Object.values(index).map((p) => p.title))
  return (marks ?? []).filter(
    (c) => c.kind === 'unverified' && c.attributive && !known.has(c.title),
  )
}
