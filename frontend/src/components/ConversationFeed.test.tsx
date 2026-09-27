// SPDX-License-Identifier: Apache-2.0
//
// The jump target. `ParticipationPanel` asks for a turn; this is the half that has to
// actually move the view, and the two failure modes are both invisible in a screenshot:
// clicking the same turn twice doing nothing, and auto-scroll dragging the reader back to
// the bottom a second later on a live run.
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ConversationFeed } from './ConversationFeed'
import type { AgentView, FeedMessage } from '../types'

vi.mock('../api', () => ({ loadAvatar: vi.fn().mockResolvedValue(null) }))

const feed: FeedMessage[] = [
  { turn: 1, seq: 2, speaker: 'Ada', content: 'first' },
  { turn: 2, seq: 4, speaker: 'Bo', content: 'second' },
  { turn: 3, seq: 6, speaker: 'Ada', content: 'third' },
]

const agent = (name: string): AgentView => ({
  name, persona: 'p', goals: [], portrait: null, portraitKey: null, portraitUrl: null,
  avatarResolved: true, messageCount: 1, tokensIn: 0, tokensOut: 0, costUsd: 0,
})
const agents = { Ada: agent('Ada'), Bo: agent('Bo') }

function renderFeed(jumpTo: { seq: number; nonce: number } | null) {
  return render(
    <ConversationFeed
      feed={feed}
      agents={agents}
      activeSpeaker={null}
      thinking={false}
      jumpTo={jumpTo}
    />,
  )
}

describe('ConversationFeed jump-to-turn', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    Element.prototype.scrollIntoView = vi.fn()
  })

  it('gives every message an id the panel can target', () => {
    renderFeed(null)
    expect(screen.getByText('second').closest('[id]')?.id).toBe('turn-4')
  })

  it('scrolls the requested message into view', () => {
    renderFeed({ seq: 4, nonce: 1 })
    const target = document.getElementById('turn-4')
    expect(target?.scrollIntoView).toBeDefined()
    expect(Element.prototype.scrollIntoView).toHaveBeenCalledWith(
      expect.objectContaining({ block: 'center' }),
    )
  })

  it('scrolls again when the same turn is clicked twice', () => {
    // The nonce is the whole reason the prop is an object. With a bare seq the second click
    // would not change the prop, the effect would not re-run, and the panel would feel
    // broken for the most natural interaction there is: clicking the same marker again
    // after scrolling away.
    const { rerender } = renderFeed({ seq: 4, nonce: 1 })
    const calls = () =>
      (Element.prototype.scrollIntoView as ReturnType<typeof vi.fn>).mock.calls.filter(
        (c) => (c[0] as ScrollIntoViewOptions)?.block === 'center',
      ).length
    expect(calls()).toBe(1)
    rerender(
      <ConversationFeed
        feed={feed} agents={agents} activeSpeaker={null} thinking={false}
        jumpTo={{ seq: 4, nonce: 2 }}
      />,
    )
    expect(calls()).toBe(2)
  })

  it('turns auto-scroll off, so a live run does not yank the view back', () => {
    const { container } = renderFeed(null)
    const box = container.querySelector('input[type="checkbox"]') as HTMLInputElement
    expect(box.checked).toBe(true)
    renderFeed({ seq: 2, nonce: 1 })
    const boxes = document.querySelectorAll('input[type="checkbox"]')
    expect((boxes[boxes.length - 1] as HTMLInputElement).checked).toBe(false)
  })

  it('marks the jumped-to message and only that one', () => {
    renderFeed({ seq: 6, nonce: 1 })
    expect(document.getElementById('turn-6')?.className).toMatch(/ring-matrix-accent/)
    expect(document.getElementById('turn-2')?.className).not.toMatch(/ring-matrix-accent/)
  })

  it('does not scroll to a turn when nothing was requested', () => {
    renderFeed(null)
    const centred = (
      Element.prototype.scrollIntoView as ReturnType<typeof vi.fn>
    ).mock.calls.filter((c) => (c[0] as ScrollIntoViewOptions)?.block === 'center')
    expect(centred).toHaveLength(0)
  })
})

describe('ConversationFeed citations', () => {
  const src = { chunk_id: 7, document_id: 'd1', title: 'spec.md', ordinal: 3 }
  const cited: FeedMessage[] = [{
    turn: 1, seq: 2, speaker: 'Ada', content: 'As spec.md #3 says, no.', sources: [src],
    citations: [{ label: 'ghost.md #1', title: 'ghost.md', kind: 'unverified', attributive: true }],
  }]
  const renderCited = (runId?: string) => render(
    <ConversationFeed feed={cited} agents={agents} activeSpeaker={null} thinking={false}
      runId={runId} sourceIndex={{ 'spec.md #3': src }} />,
  )

  it('links the citation, lists what was in view, and flags a citation nobody retrieved', () => {
    renderCited('r1')
    // Once inline, once in the list of what was in view.
    expect(screen.getAllByRole('button', { name: 'spec.md #3' })).toHaveLength(2)
    expect(screen.getByText('Sources in view:')).toBeInTheDocument()
    expect(screen.getByText(/Cites ghost.md #1 — no one in this conversation retrieved it/)).toBeInTheDocument()
  })

  it('renders plain text when there is no run to open a source from', () => {
    renderCited(undefined)
    expect(screen.queryByRole('button', { name: 'spec.md #3' })).not.toBeInTheDocument()
    expect(screen.getByText('As spec.md #3 says, no.')).toBeInTheDocument()
  })
})
