// SPDX-License-Identifier: Apache-2.0
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { FeedMessage, SimEvent } from '../../types'
import { MessageContext } from './MessageContext'

vi.mock('../../api', () => ({
  api: {
    getTurnTrace: vi.fn().mockResolvedValue({ run_id: 'r', turn: 2, available: true, rationale: 'To press the cost point.' }),
  },
}))

const feed: FeedMessage[] = [
  { turn: 1, seq: 1, speaker: 'Ada', content: 'Opening point.' },
  { turn: 2, seq: 3, speaker: 'Bo', content: 'A reply of sorts.' },
  { turn: 3, seq: 5, speaker: 'Ada', content: 'Closing point.' },
]
const ev = (seq: number, turn: number, event_type: SimEvent['event_type'], payload: object): SimEvent =>
  ({ run_id: 'r', seq, turn, event_type, agent_name: null, payload }) as SimEvent
const events = [
  ev(2, 2, 'speaker.selected', { speaker: 'Bo', reason: 'Bo has not answered the cost question.' }),
  ev(3, 2, 'agent.response', { speaker: 'Bo', message: 'x', cost_usd: 0.0123, tokens_in: 900, tokens_out: 80 }),
]
const renderIt = (props: Partial<Parameters<typeof MessageContext>[0]> = {}) =>
  render(
    <MessageContext message={feed[1]} feed={feed} events={events} order={['Ada', 'Bo']} runId="r"
      assumptions={[{ id: 'A1', statement: 'Budget is fixed', basis: '', source: 'operator', turn: 0 },
        { id: 'A2', statement: 'Made later', basis: '', source: 'moderator', turn: 3 }]}
      onOpenDossier={vi.fn()} onJump={vi.fn()} {...props} />,
  )

describe('a message’s context', () => {
  it('says why this speaker was picked, and what came before and after', () => {
    const onJump = vi.fn()
    renderIt({ onJump })
    expect(screen.getByText(/Bo has not answered the cost question/)).toBeInTheDocument()
    fireEvent.click(screen.getByText('Opening point.'))
    expect(onJump).toHaveBeenCalledWith(1)
    expect(screen.getByText('Closing point.')).toBeInTheDocument()
  })

  it('shows only the assumptions in force at that turn, and what the message cost', () => {
    renderIt()
    expect(screen.getByText('Budget is fixed')).toBeInTheDocument()
    expect(screen.queryByText('Made later')).not.toBeInTheDocument()
    expect(screen.getByText('$0.0123')).toBeInTheDocument()
  })

  it('says a missing reason was not recorded rather than leaving it blank', () => {
    renderIt({ events: [] })
    expect(screen.getByText(/No reason was recorded/)).toBeInTheDocument()
  })

  it('loads the speaker’s own account on request', async () => {
    renderIt()
    fireEvent.click(screen.getByRole('button', { name: 'Why did they say that?' }))
    await waitFor(() => expect(screen.getByText(/To press the cost point/)).toBeInTheDocument())
  })
})
