// SPDX-License-Identifier: Apache-2.0
//
// Who spoke, how often, and when — plus the jump.
//
// The panel reads the revealed feed rather than a stats endpoint, so these tests are the
// whole contract: there is no server-side number to compare against. The case that matters
// most is a persona with ZERO turns, because that is the defect the speaker-selection work
// exists to remove (in run 64cff65d two of six personas took 1 turn of 40) and a panel that
// only listed speakers would hide it.
import { describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { ParticipationPanel, giniOfShares } from './ParticipationPanel'
import type { FeedMessage } from '../types'

const feed: FeedMessage[] = [
  { turn: 1, seq: 2, speaker: 'Ada', content: 'first' },
  { turn: 2, seq: 4, speaker: 'Bo', content: 'second' },
  { turn: 3, seq: 6, speaker: 'Ada', content: 'third' },
  { turn: 4, seq: 8, speaker: 'Ada', content: 'fourth' },
]
const ORDER = ['Ada', 'Bo', 'Cy']

function row(name: string) {
  return screen.getByText(name).closest('li') as HTMLElement
}

describe('ParticipationPanel', () => {
  it('counts each persona’s turns and their share', () => {
    render(<ParticipationPanel feed={feed} order={ORDER} onJump={() => {}} />)
    expect(within(row('Ada')).getByText('3 turns')).toBeInTheDocument()
    expect(within(row('Ada')).getByText('75%')).toBeInTheDocument()
    expect(within(row('Bo')).getByText('1 turn')).toBeInTheDocument()
    expect(within(row('Bo')).getByText('25%')).toBeInTheDocument()
    expect(screen.getByText(/4 turns · 3 speakers/)).toBeInTheDocument()
  })

  it('shows a cast member who never spoke, and says so', () => {
    // The finding, not an edge case: a persona whose knowledge base and convictions were
    // authored and who never got a turn.
    render(<ParticipationPanel feed={feed} order={ORDER} onJump={() => {}} />)
    const cy = row('Cy')
    expect(within(cy).getByText('never spoke')).toBeInTheDocument()
    expect(within(cy).getByText('0 turns')).toBeInTheDocument()
    // …and offers nothing to click, since there is nowhere to jump to.
    expect(within(cy).queryAllByRole('button')).toHaveLength(0)
  })

  it('orders rows loudest first so the skew is the first thing read', () => {
    // Cast order REVERSED against turn order, deliberately: with the two agreeing, a
    // component that did no sorting at all would pass this test. It did, until this
    // fixture changed.
    render(
      <ParticipationPanel feed={feed} order={['Cy', 'Bo', 'Ada']} onJump={() => {}} />,
    )
    const names = screen.getAllByRole('listitem').map((li) => li.textContent || '')
    expect(names[0]).toMatch(/Ada/)
    expect(names[1]).toMatch(/Bo/)
    expect(names[2]).toMatch(/Cy/)
  })

  it('renders one clickable marker per turn, labelled with the turn number', () => {
    render(<ParticipationPanel feed={feed} order={ORDER} onJump={() => {}} />)
    const cells = within(row('Ada')).getAllByRole('button')
    expect(cells).toHaveLength(3)
    expect(cells[0]).toHaveAccessibleName('Jump to turn 1, Ada')
    expect(cells[2]).toHaveAccessibleName('Jump to turn 4, Ada')
  })

  it('places each marker at its position in the run, not packed to the left', () => {
    // Bo speaks only at turn 2 of 4. A packed strip would put his single marker first,
    // which reads as "Bo opened the conversation" — the opposite of the truth. Every row
    // is the full length of the run so the columns are turns.
    render(<ParticipationPanel feed={feed} order={ORDER} onJump={() => {}} />)
    const strip = within(row('Bo')).getByRole('button').parentElement as HTMLElement
    const cells = Array.from(strip.children)
    expect(cells).toHaveLength(feed.length)
    expect(cells.map((c) => c.tagName)).toEqual(['SPAN', 'BUTTON', 'SPAN', 'SPAN'])
  })

  it('gives every row the same number of cells, so the columns line up', () => {
    render(<ParticipationPanel feed={feed} order={ORDER} onJump={() => {}} />)
    for (const name of ['Ada', 'Bo', 'Cy']) {
      const r = row(name)
      const filled = within(r).queryAllByRole('button').length
      const gaps = within(r).queryAllByTestId('gap').length
      expect(filled + gaps).toBe(feed.length)
    }
  })

  it('labels the axis so a gap has a scale', () => {
    render(<ParticipationPanel feed={feed} order={ORDER} onJump={() => {}} />)
    expect(screen.getByText('turn 1')).toBeInTheDocument()
    expect(screen.getByText('turn 4')).toBeInTheDocument()
  })

  it('labels the axis with the feed’s own turn numbers, not 1..count', () => {
    // A branch's feed begins at the fork. Labelling turns 13–16 as "turn 1 … turn 4"
    // misdescribes every position on the axis, and the count alone cannot tell.
    const branchFeed: FeedMessage[] = [
      { turn: 13, seq: 26, speaker: 'Ada', content: 'a' },
      { turn: 14, seq: 28, speaker: 'Bo', content: 'b' },
      { turn: 15, seq: 30, speaker: 'Ada', content: 'c' },
      { turn: 16, seq: 32, speaker: 'Ada', content: 'd' },
    ]
    render(<ParticipationPanel feed={branchFeed} order={ORDER} onJump={() => {}} />)
    expect(screen.getByText('turn 13')).toBeInTheDocument()
    expect(screen.getByText('turn 16')).toBeInTheDocument()
    expect(screen.queryByText('turn 1')).not.toBeInTheDocument()
  })

  it('does not jump when an empty slot is clicked', () => {
    // The gaps are spacers. Wiring them to a jump would send a click on "Bo did not speak
    // here" to whichever turn happened to be underneath.
    const onJump = vi.fn()
    render(<ParticipationPanel feed={feed} order={ORDER} onJump={onJump} />)
    for (const gap of within(row('Bo')).getAllByTestId('gap')) {
      ;(gap as HTMLElement).click()
    }
    expect(onJump).not.toHaveBeenCalled()
  })

  it('jumps by seq, not by turn', () => {
    // seq is what identifies a message in the feed; turn numbers repeat across a branch's
    // replayed prefix, so jumping by turn would land on the wrong message there.
    const onJump = vi.fn()
    render(<ParticipationPanel feed={feed} order={ORDER} onJump={onJump} />)
    within(row('Bo')).getAllByRole('button')[0].click()
    expect(onJump).toHaveBeenCalledWith(4)
  })

  it('includes a speaker who is not in the cast list', () => {
    // Branch mutations can add a persona mid-run; the feed is the source of truth for who
    // actually spoke, so an unlisted speaker must still be counted rather than dropped.
    render(
      <ParticipationPanel
        feed={[...feed, { turn: 5, seq: 10, speaker: 'Zed', content: 'late' }]}
        order={ORDER}
        onJump={() => {}}
      />,
    )
    expect(within(row('Zed')).getByText('1 turn')).toBeInTheDocument()
  })

  it('says nothing rather than dividing by zero on an empty feed', () => {
    render(<ParticipationPanel feed={[]} order={ORDER} onJump={() => {}} />)
    expect(screen.getByText('No turns yet.')).toBeInTheDocument()
    expect(screen.queryByText(/%/)).not.toBeInTheDocument()
  })

  it('reports the evenness of the turn share', () => {
    render(<ParticipationPanel feed={feed} order={ORDER} onJump={() => {}} />)
    // 3/1/0 of four turns is a badly skewed run and should not be described as even.
    expect(screen.getByText(/very uneven/)).toBeInTheDocument()
  })
})

describe('giniOfShares', () => {
  it('is 0 when everyone spoke equally', () => {
    expect(giniOfShares([4, 4, 4])).toBe(0)
  })

  it('approaches 1 when one persona took every turn', () => {
    expect(giniOfShares([12, 0, 0])).toBeCloseTo(2 / 3, 5)
    expect(giniOfShares([100, 0])).toBeCloseTo(0.5, 5)
  })

  it('agrees with the evaluation harness on a real run', () => {
    // renewal-renewal-opus (64cff65d) actually went 16/11/8/3/1/1 over 40 turns.
    // `gini()` in scripts/eval_speaker_selection.py returns 0.4583333333333333 for that
    // list — checked directly, not copied from a report. The UI and the evaluation must
    // never print different numbers for the same shape, or a user comparing the panel
    // against the design doc finds a discrepancy that is only a second implementation.
    expect(giniOfShares([16, 11, 8, 3, 1, 1])).toBeCloseTo(0.4583333, 6)
  })

  it('is 0 rather than NaN when no one has spoken', () => {
    expect(giniOfShares([0, 0, 0])).toBe(0)
    expect(giniOfShares([])).toBe(0)
  })

  it('counts neither an injected message nor a consultant as a speaker', () => {
    render(
      <ParticipationPanel
        order={['Ada']}
        onJump={() => {}}
        feed={[
          { turn: 1, seq: 1, speaker: 'Ada', content: 'x' },
          { turn: 1, seq: 2, speaker: "Residents' survey", content: 'y', injected: true },
          { turn: 1, seq: 3, speaker: 'Lee (consultant)', content: 'z', consultant: { expert: 'Lee', askedBy: 'Ada', question: 'q' } },
        ]}
      />,
    )
    expect(screen.queryByText("Residents' survey")).toBeNull()
    expect(screen.queryByText('Lee (consultant)')).toBeNull()
  })
})
