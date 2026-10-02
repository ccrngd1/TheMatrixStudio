// SPDX-License-Identifier: Apache-2.0
// Hiding a run from the Runs screen, and finding it again.
//
// The server returns every run, hidden ones included, so everything here is about what the screen does with
// them: leave them out of every section and count, show them alone under the Hidden chip, and hide or show one
// from its card at once, putting it back if the server refuses. Every name and topic is invented.
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { History } from './History'
import { cached } from '../lib/listCache'
import type { RunSummary } from '../types'

vi.mock('../api', () => ({ api: { listRuns: vi.fn(), listEnsembles: vi.fn(), setRunHidden: vi.fn() } }))
import { api } from '../api'

const mocked = api as unknown as Record<'listRuns' | 'listEnsembles' | 'setRunHidden', ReturnType<typeof vi.fn>>
const NOW = Math.floor(Date.now() / 1000)

const run = (id: string, name: string, over: Partial<RunSummary> = {}): RunSummary => ({
  run_id: id, name, description: null, slug: name, topic: `${name} topic`, status: 'complete',
  turn_count: 2, total_cost_usd: 0.01, created_at: NOW, completed_at: NOW, last_event_at: NOW,
  parent_run_id: null, branch_turn: null, ensemble_id: null, ensemble_cell: null, hidden: false, ...over,
})

const ens = (id: string, name: string) => ({
  ensemble_id: id, name, description: `${name} brief`, topic: `${name} topic`, status: 'complete', created_at: 1,
  completed_at: null, spec: [{ label: 'base', n: 2, overrides: {} }], base_config: {}, has_report: false,
  report: null, report_generated_at: null, report_cost_usd: null, report_error: null,
})

function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => ((resolve = res), (reject = rej)))
  return { promise, resolve, reject }
}

function show(rows: RunSummary[], props: { onOpenEnsemble?: (id: string) => void } = {}) {
  mocked.listRuns.mockResolvedValue(rows)
  return render(<History onOpen={() => {}} {...props} />)
}

const hud = (container: HTMLElement) => container.querySelector('.cc-stats') as HTMLElement
const chip = () => screen.getByRole('button', { name: /Hidden \(/ })

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  mocked.listEnsembles.mockResolvedValue([])
  mocked.setRunHidden.mockResolvedValue({ run_id: 'x', hidden: true })
})

describe('hidden runs are left out by default', () => {
  it('from the sections and the HUD', async () => {
    const { container } = show([
      run('r1', 'amber-lantern', { total_cost_usd: 0.01 }),
      run('r2', 'birch-signal', { hidden: true, total_cost_usd: 0.5 }),
      run('r3', 'cedar-kite', { status: 'running', total_cost_usd: 0.02 }),
      run('r4', 'dune-relay', { status: 'running', hidden: true }),
    ])
    await waitFor(() => expect(screen.getByText('amber-lantern')).toBeInTheDocument())
    expect(screen.queryByText('birch-signal')).not.toBeInTheDocument()
    expect(screen.queryByText('dune-relay')).not.toBeInTheDocument()
    expect(screen.getByText('Finished & stopped').parentElement).toHaveTextContent('(1)')
    expect(screen.getByText('Live now', { selector: 'section span' }).parentElement).toHaveTextContent('(1)')

    const h = hud(container)
    expect(h).toHaveTextContent('2 runs listed')
    expect(h).toHaveTextContent('$0.03')
    expect(within(h).getByText('Finished').closest('div')?.parentElement).toHaveTextContent('01')
    expect(within(h).getByText('Live now').closest('div')?.parentElement).toHaveTextContent('01')
  })

  it('from search, on the client and from the server past the list cap', async () => {
    show([run('r1', 'amber-lantern'), run('r2', 'amber-signal', { hidden: true })])
    await waitFor(() => expect(screen.getByText('amber-lantern')).toBeInTheDocument())
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'signal' } })
    expect(screen.queryByText('amber-signal')).not.toBeInTheDocument()
    expect(screen.getByText('No individual conversation matches.')).toBeInTheDocument()
  })

  it('from the server search a full list falls back on', async () => {
    mocked.listRuns.mockResolvedValueOnce(Array.from({ length: 200 }, (_, i) => run(`r${i}`, `run-${i}`)))
    mocked.listRuns.mockResolvedValueOnce([
      run('old1', 'old-visible-run'),
      run('old2', 'old-hidden-run', { hidden: true }),
    ])
    render(<History onOpen={() => {}} />)
    await waitFor(() => expect(screen.getByText('run-0')).toBeInTheDocument())
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'old' } })
    await waitFor(() => expect(screen.getByText('old-visible-run')).toBeInTheDocument())
    expect(screen.queryByText('old-hidden-run')).not.toBeInTheDocument()
  })

  it('from the branches filter and its count', async () => {
    show([
      run('r1', 'amber-lantern'),
      run('b1', 'birch-branch', { parent_run_id: 'r1', branch_turn: 2 }),
      run('b2', 'cedar-branch', { parent_run_id: 'r1', branch_turn: 3, hidden: true }),
    ])
    await waitFor(() => expect(screen.getByText('amber-lantern')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /Branches only \(1\)/ }))
    expect(screen.getByText('birch-branch')).toBeInTheDocument()
    expect(screen.queryByText('cedar-branch')).not.toBeInTheDocument()
  })

  it('from the runs nested under an ensemble; an ensemble whose runs are all hidden stays, with nothing to unfold', async () => {
    mocked.listEnsembles.mockResolvedValue([ens('e1', 'harbour-study'), ens('e2', 'quarry-study')])
    show(
      [
        run('m1', 'harbour-one', { ensemble_id: 'e1', ensemble_cell: 'base' }),
        run('m2', 'harbour-two', { ensemble_id: 'e1', ensemble_cell: 'base', hidden: true }),
        run('m3', 'quarry-one', { ensemble_id: 'e2', ensemble_cell: 'base', hidden: true }),
        run('m4', 'quarry-two', { ensemble_id: 'e2', ensemble_cell: 'base', hidden: true }),
      ],
      { onOpenEnsemble: () => {} },
    )
    await waitFor(() => expect(screen.getByText('harbour-study')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText(/Show the conversations in harbour-study/))
    expect(screen.getByText('harbour-one')).toBeInTheDocument()
    expect(screen.queryByText('harbour-two')).not.toBeInTheDocument()

    expect(screen.getByText('quarry-study')).toBeInTheDocument()
    expect(screen.getByLabelText(/Show the conversations in quarry-study/)).toBeDisabled()
    // Nested ones are not repeated in the individual list either.
    expect(screen.queryByText('quarry-one')).not.toBeInTheDocument()
  })

  it('and when every run is hidden, the list says so rather than blaming ensembles', async () => {
    show([run('r1', 'amber-lantern', { hidden: true })])
    await waitFor(() => expect(chip()).toBeInTheDocument())
    expect(screen.getByText('Every conversation is hidden.')).toBeInTheDocument()
    expect(screen.queryByText(/No runs yet/)).not.toBeInTheDocument()
  })
})

describe('the Hidden chip', () => {
  it('is absent while nothing is hidden', async () => {
    show([run('r1', 'amber-lantern')])
    await waitFor(() => expect(screen.getByText('amber-lantern')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /Hidden/ })).not.toBeInTheDocument()
  })

  it('switches the page to the hidden runs alone, flat, and says so', async () => {
    mocked.listEnsembles.mockResolvedValue([ens('e1', 'harbour-study')])
    const { container } = show(
      [
        run('r1', 'amber-lantern'),
        run('r2', 'birch-signal', { hidden: true, total_cost_usd: 0.25 }),
        run('m1', 'harbour-one', { ensemble_id: 'e1', ensemble_cell: 'base' }),
        run('m2', 'harbour-two', { ensemble_id: 'e1', ensemble_cell: 'base', hidden: true }),
      ],
      { onOpenEnsemble: () => {} },
    )
    await waitFor(() => expect(screen.getByText('harbour-study')).toBeInTheDocument())
    expect(chip()).toHaveTextContent('Hidden (2)')
    expect(chip()).toHaveAttribute('aria-pressed', 'false')
    expect(screen.queryByText(/Showing hidden conversations only/)).not.toBeInTheDocument()

    fireEvent.click(chip())
    expect(chip()).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('status')).toHaveTextContent('Showing hidden conversations only.')
    expect(screen.getByText('birch-signal')).toBeInTheDocument()
    // A hidden member is listed with the rest, not folded under an ensemble that is not shown.
    expect(screen.getByText('harbour-two')).toBeInTheDocument()
    expect(screen.queryByText('harbour-study')).not.toBeInTheDocument()
    expect(screen.queryByText('amber-lantern')).not.toBeInTheDocument()
    expect(screen.getByText('Finished & stopped').parentElement).toHaveTextContent('(2)')
    expect(hud(container)).toHaveTextContent('2 hidden')
    expect(hud(container)).toHaveTextContent('$0.26')

    // Search keeps to the view.
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'birch' } })
    expect(screen.getByText('birch-signal')).toBeInTheDocument()
    expect(screen.queryByText('harbour-two')).not.toBeInTheDocument()
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'amber' } })
    expect(screen.getByText('No hidden conversation matches.')).toBeInTheDocument()
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: '' } })

    fireEvent.click(chip())
    expect(screen.getByText('amber-lantern')).toBeInTheDocument()
    expect(screen.queryByText('birch-signal')).not.toBeInTheDocument()
  })

  it('is remembered when the screen is left and come back to, but not stored', async () => {
    const { unmount } = show([run('r1', 'amber-lantern'), run('r2', 'birch-signal', { hidden: true })])
    await waitFor(() => expect(chip()).toBeInTheDocument())
    fireEvent.click(chip())
    unmount()

    show([run('r1', 'amber-lantern'), run('r2', 'birch-signal', { hidden: true })])
    expect(chip()).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText('birch-signal')).toBeInTheDocument()
    expect(Object.keys(localStorage).some((k) => /hidden/i.test(k))).toBe(false)
  })
})

describe('hiding from a card', () => {
  it('is a control of its own beside the card, named for the run', async () => {
    show([run('r1', 'amber-lantern')])
    const control = await screen.findByRole('button', { name: 'Hide amber-lantern' })
    const card = screen.getByText('amber-lantern').closest('button') as HTMLElement
    expect(card).not.toBe(control)
    expect(card.contains(control)).toBe(false)
  })

  it('takes the run out at once, and the cached list keeps it out', async () => {
    const answer = deferred<unknown>()
    mocked.setRunHidden.mockReturnValue(answer.promise)
    const { unmount } = show([run('r1', 'amber-lantern'), run('r2', 'birch-signal')])
    fireEvent.click(await screen.findByRole('button', { name: 'Hide amber-lantern' }))

    // Before the server has answered.
    expect(screen.queryByText('amber-lantern')).not.toBeInTheDocument()
    expect(chip()).toHaveTextContent('Hidden (1)')
    expect(mocked.setRunHidden).toHaveBeenCalledWith('r1', true)
    expect(cached<RunSummary[]>('runs')?.find((r) => r.run_id === 'r1')?.hidden).toBe(true)
    await act(async () => answer.resolve({ run_id: 'r1', hidden: true }))
    unmount()

    // Back again, with the refresh not yet answered: the list on screen is the cached one.
    mocked.listRuns.mockReturnValue(new Promise(() => {}))
    render(<History onOpen={() => {}} />)
    expect(screen.getByText('birch-signal')).toBeInTheDocument()
    expect(screen.queryByText('amber-lantern')).not.toBeInTheDocument()
    expect(chip()).toHaveTextContent('Hidden (1)')
  })

  it('puts the run back and says why when the server refuses, in the cache too', async () => {
    const answer = deferred<unknown>()
    mocked.setRunHidden.mockReturnValue(answer.promise)
    const { unmount } = show([run('r1', 'amber-lantern'), run('r2', 'birch-signal')])
    fireEvent.click(await screen.findByRole('button', { name: 'Hide amber-lantern' }))
    expect(screen.queryByText('amber-lantern')).not.toBeInTheDocument()

    await act(async () => answer.reject(new Error('500: Internal Server Error')))
    expect(screen.getByText('amber-lantern')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('Could not hide amber-lantern: 500: Internal Server Error')
    expect(screen.queryByRole('button', { name: /Hidden \(/ })).not.toBeInTheDocument()
    unmount()

    mocked.listRuns.mockReturnValue(new Promise(() => {}))
    render(<History onOpen={() => {}} />)
    expect(screen.getByText('amber-lantern')).toBeInTheDocument()
  })

  it('shows a run again from the Hidden view', async () => {
    show([run('r1', 'amber-lantern'), run('r2', 'birch-signal', { hidden: true })])
    await waitFor(() => expect(chip()).toBeInTheDocument())
    fireEvent.click(chip())
    fireEvent.click(screen.getByRole('button', { name: 'Show birch-signal' }))
    expect(mocked.setRunHidden).toHaveBeenCalledWith('r2', false)
    expect(screen.queryByText('birch-signal')).not.toBeInTheDocument()
    expect(screen.getByText('Nothing is hidden.')).toBeInTheDocument()
    // The chip stays while it is on, so the view can be left.
    expect(chip()).toHaveTextContent('Hidden (0)')
    fireEvent.click(chip())
    expect(screen.getByText('birch-signal')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Hide birch-signal' })).toBeInTheDocument()
  })

  it('is not undone by a refresh that was already in flight', async () => {
    // The list reads every run before it answers, so a refresh sent before the hide can answer after it.
    show([run('r1', 'amber-lantern'), run('r2', 'birch-signal')]).unmount()
    await waitFor(() => expect(cached('runs')).toBeDefined())
    const refresh = deferred<RunSummary[]>()
    mocked.listRuns.mockReturnValue(refresh.promise)
    const { unmount } = render(<History onOpen={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Hide amber-lantern' }))
    await act(async () => refresh.resolve([run('r1', 'amber-lantern'), run('r2', 'birch-signal')]))
    expect(screen.queryByText('Refreshing…')).not.toBeInTheDocument()
    expect(screen.queryByText('amber-lantern')).not.toBeInTheDocument()
    expect(cached<RunSummary[]>('runs')?.find((r) => r.run_id === 'r1')?.hidden).toBe(true)
    unmount()

    // A request sent after the hide is the server's word, and is taken as it comes.
    mocked.listRuns.mockResolvedValue([run('r1', 'amber-lantern'), run('r2', 'birch-signal')])
    render(<History onOpen={() => {}} />)
    await waitFor(() => expect(screen.getByText('amber-lantern')).toBeInTheDocument())
  })

  it('moves focus to the next card’s control, not the top of the page', async () => {
    show([run('r1', 'amber-lantern'), run('r2', 'birch-signal'), run('r3', 'cedar-kite')])
    const first = await screen.findByRole('button', { name: 'Hide amber-lantern' })
    first.focus()
    fireEvent.click(first)
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Hide birch-signal' }))

    // The last card hands focus back up the list; the only card left, to the Hidden chip.
    fireEvent.click(screen.getByRole('button', { name: 'Hide cedar-kite' }))
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Hide birch-signal' }))
    fireEvent.click(screen.getByRole('button', { name: 'Hide birch-signal' }))
    expect(document.activeElement).toBe(chip())
  })
})
