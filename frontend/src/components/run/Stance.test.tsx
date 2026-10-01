// SPDX-License-Identifier: Apache-2.0
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { FeedMessage, StanceBasisEntry } from '../../types'
import { RoomCell, RoomMap, RoomMapKey, StanceBar, StanceBasisList, StanceCounts, StanceDial, countStances } from './Stance'

const stance = { Ada: 'support', Bo: 'holding', Cy: 'unstated', Di: 'support' } as const
// With one persona who accepted with conditions (2026-10-01).
const withCond = { Ada: 'support', Bo: 'holding', Cy: 'conditional', Di: 'support' } as const
const msg = (seq: number, speaker: string, extra: Partial<FeedMessage> = {}): FeedMessage =>
  ({ turn: seq, seq, speaker, content: 'x', ...extra })

describe('stance surfaces', () => {
  it('counts each stance and the share that supports', () => {
    expect(countStances({ ...stance })).toMatchObject({ support: 2, holding: 1, unstated: 1, pct: 50 })
    // Among a subset, e.g. the personas who spoke (§6.4): the others are left out of the share.
    expect(countStances({ ...stance }, ['Ada', 'Bo'])).toMatchObject({ support: 1, holding: 1, pct: 50, total: 2 })
  })

  it('does not fold acceptance with conditions into % support; it is a share of its own', () => {
    expect(countStances({ ...withCond })).toMatchObject({ support: 2, conditional: 1, pct: 50, pctConditional: 25 })
  })

  it('never says a count by colour alone', () => {
    render(<StanceCounts stance={{ ...stance }} />)
    expect(screen.getByText('▲2', { exact: false }).textContent).toContain('support')
    expect(screen.getByText('▼1', { exact: false }).textContent).toContain('holding out')
  })

  it('counts with conditions by glyph and word, and only where there is one', () => {
    const { unmount } = render(<StanceCounts stance={{ ...withCond }} />)
    expect(screen.getByText('◐1', { exact: false }).textContent).toContain('with conditions')
    unmount()
    // A run that could not have it (no closing round) does not grow a "◐0".
    render(<StanceCounts stance={{ ...stance }} />)
    expect(screen.queryByText(/◐/)).not.toBeInTheDocument()
  })

  it('the bar names every persona’s stance and orders with conditions after support', () => {
    render(<StanceBar stance={{ ...withCond }} order={['Bo', 'Cy', 'Ada', 'Di']} />)
    expect(screen.getByRole('img', { name: 'Ada: support, Di: support, Cy: with conditions, Bo: holding out' }))
      .toBeInTheDocument()
  })

  it('the HUD cell labels the figure as support and gives conditional acceptance its own share', () => {
    const { unmount } = render(<RoomCell stance={{ ...withCond }} order={['Ada', 'Bo', 'Cy', 'Di']} />)
    expect(screen.getByText('50% support · +25% with conditions')).toBeInTheDocument()
    unmount()
    render(<RoomCell stance={{ ...stance }} order={['Ada', 'Bo', 'Cy', 'Di']} />)
    expect(screen.getByText('50% support')).toBeInTheDocument()
  })

  it('the dial names its split for a screen reader', () => {
    render(<StanceDial stance={{ ...stance }} />)
    expect(screen.getByRole('img', { name: /50% support; 2 support, 1 not stated, 1 holding out/ })).toBeInTheDocument()
  })

  it('the dial draws, prints and names acceptance with conditions apart from support', () => {
    const { container } = render(<StanceDial stance={{ ...withCond }} />)
    expect(screen.getByRole('img', {
      name: 'Where the room ended: 50% support, +25% with conditions; 2 support, 1 with conditions, 0 not stated, '
        + '1 holding out',
    })).toBeInTheDocument()
    expect(screen.getByText('+25% WITH CONDITIONS')).toBeInTheDocument()
    expect(container.querySelectorAll('.cc-g-seg')).toHaveLength(3)
    const counts = container.querySelector('.cc-gcounts') as HTMLElement
    expect(counts.textContent).toContain('with conditions')
    expect(counts.querySelector('.cc-c-conditional')?.textContent).toBe('1with conditions')
  })

  it('the room map draws one line per pair who spoke one after the other, and opens a dossier', () => {
    const onOpen = vi.fn()
    const feed = [msg(1, 'Ada'), msg(2, 'Bo'), msg(3, 'Ada'), msg(4, 'Cy'), msg(5, 'Ada', { injected: true })]
    const { container } = render(
      <RoomMap order={['Ada', 'Bo', 'Cy']} feed={feed} stance={null} next={null} onOpen={onOpen} />,
    )
    // Ada→Bo, Bo→Ada are one pair; Ada→Cy another. The injected message is not a speaker's turn.
    expect(container.querySelectorAll('.cc-edge')).toHaveLength(2)
    expect(screen.getByText('NO STANCE YET')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /^Bo: 1 turns/ }))
    expect(onOpen).toHaveBeenCalledWith('Bo')
  })

  it('shows the share that supports in the centre once there is a stance', () => {
    render(<RoomMap order={['Ada', 'Bo']} feed={[msg(1, 'Ada'), msg(2, 'Bo')]} stance={{ Ada: 'support', Bo: 'holding' }}
      next={null} onOpen={() => {}} />)
    expect(screen.getByText('50%')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Bo: 1 turns, holding out/ })).toBeInTheDocument()
  })

  it('names a node that accepted with conditions in words', () => {
    render(<RoomMap order={['Ada', 'Bo']} feed={[msg(1, 'Ada'), msg(2, 'Bo')]}
      stance={{ Ada: 'support', Bo: 'conditional' }} next={null} onOpen={() => {}} />)
    expect(screen.getByRole('button', { name: /Bo: 1 turns, with conditions/ })).toBeInTheDocument()
    // ▲ alone: Bo's conditional acceptance is not in the centre's figure.
    expect(screen.getByText('50%')).toBeInTheDocument()
  })

  it('the room map’s phase is taken once, not on every new message', () => {
    // Read per render, the phase followed the clock, and a running loop jumped each time it changed.
    const now = vi.spyOn(Date, 'now').mockReturnValue(1000)
    try {
      const props = { order: ['Ada', 'Bo'], stance: null, next: 'Bo', onOpen: () => {} }
      const { container, rerender } = render(<RoomMap {...props} feed={[msg(1, 'Ada'), msg(2, 'Bo')]} />)
      const live = () => (container.querySelector('.cc-edge-live') as SVGElement).style.getPropertyValue('--ph')
      expect(live()).toBe('-1000ms')
      now.mockReturnValue(5000)
      rerender(<RoomMap {...props} feed={[msg(1, 'Ada'), msg(2, 'Bo'), msg(3, 'Ada')]} />)
      expect(live()).toBe('-1000ms')
    } finally {
      now.mockRestore()
    }
  })

  it('the key spells out every stance, and what the centre leaves out', () => {
    render(<RoomMapKey hasStance />)
    expect(screen.getByText(/▲ support, ◐ with conditions, ◆ not stated, ▼ holding out/)).toBeInTheDocument()
    expect(screen.getByText(/those who accept with conditions are not counted in it/)).toBeInTheDocument()
  })
})

describe('why each persona has their stance', () => {
  const basis: Record<string, StanceBasisEntry> = {
    Ada: { stance: 'support', source: 'closing', class: 'accepts', quote: 'I sign it as written' },
    Bo: { stance: 'holding', source: 'summary', class: null, quote: 'Objects to the start date', fallback: 'no_statement' },
    Cy: { stance: 'conditional', source: 'closing', class: 'accepts_with_conditions', quote: 'I sign once the audit runs' },
  }

  it('lists each persona who spoke with their stance, its source and the words it rests on', () => {
    render(<StanceBasisList basis={basis} order={['Ada', 'Bo', 'Cy', 'Zed']} />)
    const items = screen.getAllByRole('listitem')
    expect(items).toHaveLength(3)
    expect(items[0].textContent).toContain('▲ support')
    expect(items[0].textContent).toContain('From their closing statement')
    expect(items[0].textContent).toContain('“I sign it as written”')
    expect(items[1].textContent).toContain("From the summary: named among its dissenters. In the summary's words")
    expect(items[1].textContent).toContain('Not from the closing statement: they made no closing statement.')
    expect(items[2].textContent).toContain('◐ with conditions')
    expect(items[2].textContent).toContain('“I sign once the audit runs”')
  })

  it('renders nothing for a run with no basis among those listed', () => {
    const { container } = render(<StanceBasisList basis={basis} order={['Zed']} />)
    expect(container).toBeEmptyDOMElement()
  })
})
