// SPDX-License-Identifier: Apache-2.0
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { FeedMessage } from '../../types'
import { RoomMap, StanceCounts, StanceDial, countStances } from './Stance'

const stance = { Ada: 'support', Bo: 'holding', Cy: 'unstated', Di: 'support' } as const
const msg = (seq: number, speaker: string, extra: Partial<FeedMessage> = {}): FeedMessage =>
  ({ turn: seq, seq, speaker, content: 'x', ...extra })

describe('stance surfaces', () => {
  it('counts each stance and the share that supports', () => {
    expect(countStances({ ...stance })).toMatchObject({ support: 2, holding: 1, unstated: 1, pct: 50 })
    // Among a subset, e.g. the personas who spoke (§6.4): the others are left out of the share.
    expect(countStances({ ...stance }, ['Ada', 'Bo'])).toMatchObject({ support: 1, holding: 1, pct: 50, total: 2 })
  })

  it('never says a count by colour alone', () => {
    render(<StanceCounts stance={{ ...stance }} />)
    expect(screen.getByText('▲2', { exact: false }).textContent).toContain('support')
    expect(screen.getByText('▼1', { exact: false }).textContent).toContain('holding out')
  })

  it('the dial names its split for a screen reader', () => {
    render(<StanceDial stance={{ ...stance }} />)
    expect(screen.getByRole('img', { name: /50% support; 2 support, 1 not stated, 1 holding out/ })).toBeInTheDocument()
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
})
