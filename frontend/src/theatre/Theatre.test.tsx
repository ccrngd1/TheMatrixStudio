// SPDX-License-Identifier: Apache-2.0
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import type { SimEvent } from '../types'

vi.mock('../api', () => ({ api: { getRun: vi.fn(), getEvents: vi.fn() } }))
import { api } from '../api'
import { Theatre } from './Theatre'

// jsdom has no 2D canvas; the stage must still mount and the dialogue must still play.
beforeAll(() => {
  HTMLCanvasElement.prototype.getContext = (() => null) as unknown as HTMLCanvasElement['getContext']
})

const cast = [
  { name: 'Ana Silva', persona: 'Operations lead', goals: [] },
  { name: 'Ben Carter', persona: 'Finance partner', goals: [] },
]
let seq = 0
const ev = (turn: number, event_type: SimEvent['event_type'], payload: Record<string, unknown>, agent_name: string | null = null): SimEvent =>
  ({ run_id: 'r1', turn, seq: seq++, event_type, agent_name, payload })

const events = (): SimEvent[] => [
  ev(0, 'sim.started', {}),
  ev(1, 'agent.response', { speaker: 'Ana Silva', message: 'We should pilot it for a quarter.' }, 'Ana Silva'),
  ev(2, 'agent.response', { speaker: 'State DOT', message: 'A letter arrives from the state.', injected: true }),
  ev(2, 'expert.answered', { expert: 'Traffic economist', asked_by: 'Ben Carter', question: 'Does it work?', answer: 'It shifts a fifth of deliveries.' }),
  ev(3, 'agent.response', { speaker: 'Ben Carter', message: 'Then I can live with it.' }, 'Ben Carter'),
  ev(3, 'sim.completed', {}),
]

const run = (status: string) => ({
  run_id: 'r1', name: 'quiet-harbor', description: null, slug: null, topic: 'Should we pilot a four-day week?',
  status, cast, config: {}, result: null,
})

beforeEach(() => {
  seq = 0
  vi.mocked(api.getRun).mockReset()
  vi.mocked(api.getEvents).mockReset()
})

const dialogue = () => screen.getByRole('region', { name: /Dialogue/ })
/** What has been typed so far: the line minus its hidden, not-yet-typed remainder (which is in
 *  the DOM to hold the box's height) and minus the whole-page copy announced to screen readers. */
const visibleLine = () => {
  const el = dialogue().querySelector('.th-line')
  if (!el) return ''
  return Array.from(el.childNodes)
    .filter((n) => !(n instanceof HTMLElement && n.classList.contains('th-unshown')))
    .map((n) => n.textContent ?? '')
    .join('')
}
const nameplate = () => dialogue().querySelector('.th-name')?.textContent ?? ''
/** From a fully typed page: one press turns it, the next finishes typing the new one. */
const nextPage = () => {
  fireEvent.click(dialogue())
  fireEvent.click(dialogue())
}

describe('Theatre', () => {
  it('opens on a title card with the run and its topic', async () => {
    vi.mocked(api.getRun).mockResolvedValue(run('complete') as never)
    vi.mocked(api.getEvents).mockResolvedValue(events())
    render(<Theatre runRef="r1" autoAdvance={false} />)
    expect(await screen.findByRole('heading', { name: 'quiet-harbor' })).toBeInTheDocument()
    expect(screen.getByText('Should we pilot a four-day week?')).toBeInTheDocument()
    expect(screen.getByText(/2 personas · 4 lines/)).toBeInTheDocument()
  })

  it('plays the transcript in order, one line per page, and labels who is speaking', async () => {
    vi.mocked(api.getRun).mockResolvedValue(run('complete') as never)
    vi.mocked(api.getEvents).mockResolvedValue(events())
    render(<Theatre runRef="r1" autoAdvance={false} />)
    fireEvent.click(await screen.findByRole('button', { name: /Start/ }))

    fireEvent.click(dialogue()) // finish typing
    expect(visibleLine()).toContain('We should pilot it for a quarter.')
    expect(nameplate()).toMatch(/^Ana Silva#01/)
    expect(screen.getByText(/LINE 1\/4 · TURN 1/)).toBeInTheDocument()

    nextPage()
    expect(nameplate()).toMatch(/^State DOT · injected into the conversation#02/)

    nextPage()
    expect(nameplate()).toMatch(/^Traffic economist · consultant, answering Ben Carter#02/)

    nextPage()
    expect(nameplate()).toMatch(/^Ben Carter#03/)
    expect(screen.getByText(/LINE 4\/4 · TURN 3/)).toBeInTheDocument()

    fireEvent.click(dialogue())
    expect(screen.getByText('End of transcript')).toBeInTheDocument()
  })

  it('moves one line per press however fast the presses come', async () => {
    vi.mocked(api.getRun).mockResolvedValue(run('complete') as never)
    vi.mocked(api.getEvents).mockResolvedValue(events())
    render(<Theatre runRef="r1" autoAdvance={false} />)
    fireEvent.click(await screen.findByRole('button', { name: /Start/ }))
    const next = screen.getByRole('button', { name: 'Next page' })
    // All three inside one batch, as real rapid presses (or a script) can land. Reading the line
    // from a closure moved one line in total here.
    act(() => {
      next.click()
      next.click()
      next.click()
    })
    expect(screen.getByText(/LINE 4\/4 · TURN 3/)).toBeInTheDocument()
  })

  it('announces each page whole to screen readers, not a character at a time', async () => {
    vi.mocked(api.getRun).mockResolvedValue(run('complete') as never)
    vi.mocked(api.getEvents).mockResolvedValue(events())
    const { container } = render(<Theatre runRef="r1" autoAdvance={false} />)
    fireEvent.click(await screen.findByRole('button', { name: /Start/ }))
    const live = container.querySelector('[aria-live="polite"]')
    expect(live?.textContent).toBe('Ana Silva, turn 1: We should pilot it for a quarter.')
  })

  it('keeps the whiteboard to what was pinned at that point, and marks what went up since the last line', async () => {
    const withBoard = (): SimEvent[] => {
      seq = 0
      return [
        ev(0, 'sim.started', {}),
        ev(0, 'assumption.made', { id: 'a1', statement: 'Headcount stays flat' }),
        ev(1, 'agent.response', { speaker: 'Ana Silva', message: 'We should pilot it for a quarter.' }, 'Ana Silva'),
        ev(1, 'assumption.made', { id: 'a2', statement: 'Budget frozen until Q3' }),
        ev(2, 'agent.response', { speaker: 'Ben Carter', message: 'Then I can live with it.' }, 'Ben Carter'),
        ev(2, 'assumption.withdrawn', { id: 'a1' }),
        ev(3, 'agent.response', { speaker: 'Ana Silva', message: 'Agreed.' }, 'Ana Silva'),
        ev(3, 'sim.completed', {}),
      ]
    }
    vi.mocked(api.getRun).mockResolvedValue(run('complete') as never)
    vi.mocked(api.getEvents).mockResolvedValue(withBoard())
    render(<Theatre runRef="r1" autoAdvance={false} />)
    const board = () => screen.getByLabelText('Working assumptions on the board').textContent ?? ''
    fireEvent.click(await screen.findByRole('button', { name: /Start/ }))
    expect(board()).toContain('Headcount stays flat')
    expect(board()).not.toContain('Budget frozen')
    expect(board()).not.toContain('NEW')

    fireEvent.click(dialogue())
    nextPage() // Ben's line: the budget note went up after Ana spoke
    expect(board()).toMatch(/NEW · Budget frozen until Q3/)
    expect(board()).toContain('Headcount stays flat')

    nextPage() // Ana again: headcount was withdrawn after Ben spoke
    expect(board()).not.toContain('Headcount stays flat')
    expect(board()).toContain('Budget frozen until Q3')
    expect(board()).not.toContain('NEW')
  })

  it('tags a line that moved a position or drew on sources, from what the engine recorded', async () => {
    const tagged = (): SimEvent[] => {
      seq = 0
      return [
        ev(0, 'sim.started', {}),
        ev(1, 'document.retrieved', { speaker: 'Ana Silva', passages: [{ chunk_id: 1, document_id: 'd', title: 'Survey', ordinal: 1 }] }, 'Ana Silva'),
        ev(1, 'agent.response', { speaker: 'Ana Silva', message: 'The survey says so.' }, 'Ana Silva'),
        ev(2, 'agent.response', { speaker: 'Ben Carter', message: 'Fine, I have moved.' }, 'Ben Carter'),
        ev(2, 'position.shift', { speaker: 'Ben Carter', sentences: ['I have moved.'] }, 'Ben Carter'),
        ev(2, 'sim.completed', {}),
      ]
    }
    vi.mocked(api.getRun).mockResolvedValue(run('complete') as never)
    vi.mocked(api.getEvents).mockResolvedValue(tagged())
    render(<Theatre runRef="r1" autoAdvance={false} />)
    fireEvent.click(await screen.findByRole('button', { name: /Start/ }))
    fireEvent.click(dialogue())
    expect(nameplate()).toMatch(/SOURCES/)
    expect(nameplate()).not.toMatch(/SHIFT/)
    nextPage()
    expect(nameplate()).toMatch(/SHIFT/)
    expect(nameplate()).not.toMatch(/SOURCES/)
  })

  it('holds a consultant\'s answer until the asker has walked out, and one press skips the walk', async () => {
    vi.mocked(api.getRun).mockResolvedValue(run('complete') as never)
    vi.mocked(api.getEvents).mockResolvedValue(events())
    render(<Theatre runRef="r1" autoAdvance={false} />)
    fireEvent.click(await screen.findByRole('button', { name: /Start/ }))
    const next = screen.getByRole('button', { name: 'Next page' })
    fireEvent.click(next)
    fireEvent.click(next) // the consultant's answer; Ben is walking to the SME door
    expect(nameplate()).toMatch(/answering Ben Carter/)
    await new Promise((r) => setTimeout(r, 120))
    expect(visibleLine().replace('▼', '').trim()).toBe('')
    fireEvent.click(dialogue())
    expect(visibleLine()).toContain('It shifts a fifth of deliveries.')
  })

  it('will not replay a run that is still going', async () => {
    vi.mocked(api.getRun).mockResolvedValue(run('running') as never)
    vi.mocked(api.getEvents).mockResolvedValue(events().slice(0, 2))
    render(<Theatre runRef="r1" autoAdvance={false} />)
    expect(await screen.findByText(/still in progress/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Start/ })).not.toBeInTheDocument()
  })

  it('says so when the run cannot be loaded', async () => {
    vi.mocked(api.getRun).mockRejectedValue(new Error('404: not found'))
    vi.mocked(api.getEvents).mockResolvedValue([])
    render(<Theatre runRef="nope" autoAdvance={false} />)
    expect(await screen.findByText(/Could not load this run: 404/)).toBeInTheDocument()
  })
})
