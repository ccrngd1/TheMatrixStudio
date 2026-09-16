// SPDX-License-Identifier: Apache-2.0
//
// Rounds in the transcript. The simultaneous method puts every survivor of a round on one
// turn number, and a flat list of them reads as a sequence — as though the second speaker
// had heard the first. That is the one thing about this mode a reader can be actively
// misled about, so the grouping is a correctness concern, not decoration.
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ConversationFeed } from './ConversationFeed'
import type { AgentView, FeedMessage } from '../types'

vi.mock('../api', () => ({ loadAvatar: vi.fn().mockResolvedValue(null) }))

const agent = (name: string): AgentView => ({
  name, persona: 'p', goals: [], portrait: null, portraitKey: null, portraitUrl: null,
  avatarResolved: true, messageCount: 1, tokensIn: 0, tokensOut: 0, costUsd: 0,
})
const agents = { Ada: agent('Ada'), Bo: agent('Bo'), Cy: agent('Cy') }

// Round 1: three at once. Round 2: two (Cy passed). Round 3: one (the others passed).
const rounds: FeedMessage[] = [
  { turn: 1, seq: 2, speaker: 'Ada', content: 'a1' },
  { turn: 1, seq: 4, speaker: 'Bo', content: 'b1' },
  { turn: 1, seq: 6, speaker: 'Cy', content: 'c1' },
  { turn: 2, seq: 8, speaker: 'Ada', content: 'a2' },
  { turn: 2, seq: 10, speaker: 'Bo', content: 'b2' },
  { turn: 3, seq: 12, speaker: 'Ada', content: 'a3' },
]

function renderFeed(feed: FeedMessage[]) {
  return render(
    <ConversationFeed feed={feed} agents={agents} activeSpeaker={null} thinking={false} />,
  )
}

describe('ConversationFeed rounds', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    Element.prototype.scrollIntoView = vi.fn()
  })

  it('names each round and says how many spoke in it', () => {
    renderFeed(rounds)
    expect(screen.getByText(/round 1 · 3 spoke at once/)).toBeInTheDocument()
    expect(screen.getByText(/round 2 · 2 spoke at once/)).toBeInTheDocument()
  })

  it('marks every message after the first in a round as concurrent', () => {
    renderFeed(rounds)
    // Round 1 has three messages: one divider, two "at the same time" marks.
    // Round 2 has two: one divider, one mark. Three marks in total.
    expect(screen.getAllByText(/at the same time/)).toHaveLength(3)
  })

  it('does not draw a round for a turn with a single speaker', () => {
    // Round 3 is one message — everybody else passed. A divider there would announce a
    // round of one, and in moderated mode EVERY turn is one message, so this is also what
    // keeps the sequential transcript exactly as it was.
    renderFeed(rounds)
    expect(screen.queryByText(/round 3/)).not.toBeInTheDocument()
  })

  it('leaves a moderated transcript completely ungrouped', () => {
    renderFeed([
      { turn: 1, seq: 2, speaker: 'Ada', content: 'a' },
      { turn: 2, seq: 4, speaker: 'Bo', content: 'b' },
      { turn: 3, seq: 6, speaker: 'Cy', content: 'c' },
    ])
    expect(screen.queryByText(/spoke at once/)).not.toBeInTheDocument()
    expect(screen.queryByText(/at the same time/)).not.toBeInTheDocument()
  })

  it('still gives every message its own jump anchor', () => {
    // The participation panel jumps by seq, and a round's messages share a TURN — so
    // grouping must not collapse the anchors or two thirds of a round becomes unreachable.
    renderFeed(rounds)
    for (const m of rounds) {
      expect(document.getElementById(`turn-${m.seq}`)).not.toBeNull()
    }
  })
})
