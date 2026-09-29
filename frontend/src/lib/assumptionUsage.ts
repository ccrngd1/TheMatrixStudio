// SPDX-License-Identifier: Apache-2.0
// The browser's copy of `matrix_studio/assumptions.usage`: how often each assumption is cited by id, and the
// sentences that appear to dispute it. The same word match and the same exclusion of conditionals ("if A2
// doesn't hold…"), so the card and the report agree. Keep the two in step; tests pin the shared cases.
import type { FeedMessage } from '../types'

const DISPUTE =
  /\b(?:wrong|doubt\w*|disagree\w*|don'?t (?:buy|accept|trust|believe)|not (?:a guarantee|realistic|convinced|credible|safe to assume)|too (?:optimistic|pessimistic|high|low|rosy|aggressive|conservative)|unrealistic|optimistic|skeptic\w*|sceptic\w*|reject\w*|push(?:ing)? back|can'?t accept|isn'?t (?:right|realistic|credible)|questionable|shaky|flawed|overstat\w*|understat\w*|soft|dispute\w*|won'?t hold|doesn'?t hold|unfounded)\b/i
const SENTENCE = /(?<=[.!?])\s+/

export interface Usage {
  cited: number
  disputes: { speaker: string; turn: number; sentence: string }[]
}

const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

export function assumptionUsage(ids: string[], feed: FeedMessage[]): Record<string, Usage> {
  const out: Record<string, Usage> = {}
  for (const id of ids) {
    const word = new RegExp(`\\b${esc(id)}\\b`)
    const conditional = new RegExp(`\\b(?:if|unless|in case|should|whether)\\b[^.;:]{0,60}\\b${esc(id)}\\b`, 'i')
    const u: Usage = { cited: 0, disputes: [] }
    for (const m of feed) {
      if (m.injected || !word.test(m.content)) continue
      u.cited += 1
      const hit = m.content.split(SENTENCE).find((s) => word.test(s) && DISPUTE.test(s) && !conditional.test(s))
      if (hit) u.disputes.push({ speaker: m.speaker, turn: m.turn, sentence: hit.trim() })
    }
    out[id] = u
  }
  return out
}
