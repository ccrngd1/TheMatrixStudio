// SPDX-License-Identifier: Apache-2.0
// The ensembles section of the history list.
//
// The behaviour worth pinning is the failure mode, not the happy path: `/api/ensembles` does
// not exist on an older deployment, and this list must degrade to "no ensembles section"
// rather than to "no runs". Folding the two requests together is the obvious tidy and would
// mean a 404 on the new route empties the list of conversations the operator actually has.
//
// The status badge also has to be three states, not two. "No report yet" and "the report was
// refused" are different, and only the second is worth opening to retry.

import { describe, expect, it, vi, beforeEach } from 'vitest'
import { act, render, screen, waitFor, fireEvent } from '@testing-library/react'
import { useState } from 'react'
import { History } from './History'

vi.mock('../api', () => ({ api: { listRuns: vi.fn(), listEnsembles: vi.fn() } }))
import { api } from '../api'

const mocked = api as unknown as {
  listRuns: ReturnType<typeof vi.fn>
  listEnsembles: ReturnType<typeof vi.fn>
}

function ens(over: Record<string, unknown> = {}) {
  return {
    ensemble_id: 'e1',
    name: 'renewal',
    description: 'annual renewal proposal',
    topic: 'renewal policy',
    status: 'running',
    created_at: 1,
    completed_at: null,
    spec: [{ label: 'base', n: 5, overrides: {} }],
    base_config: {},
    has_report: false,
    report: null,
    report_generated_at: null,
    report_cost_usd: null,
    report_error: null,
    ...over,
  }
}

describe('History ensembles', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocked.listRuns.mockResolvedValue([])
  })

  it('lists ensembles with their run count', async () => {
    mocked.listEnsembles.mockResolvedValue([ens()])
    render(<History onOpen={() => {}} onNew={() => {}} onOpenEnsemble={() => {}} />)

    await waitFor(() => expect(screen.getByText('renewal')).toBeInTheDocument())
    expect(screen.getByText(/5 runs/)).toBeInTheDocument()
  })

  it('counts groups when there is more than one', async () => {
    mocked.listEnsembles.mockResolvedValue([
      ens({
        spec: [
          { label: 'base', n: 5, overrides: {} },
          { label: 'hybrid', n: 3, overrides: { 'selection.method': 'hybrid' } },
        ],
      }),
    ])
    render(<History onOpen={() => {}} onNew={() => {}} onOpenEnsemble={() => {}} />)

    await waitFor(() => expect(screen.getByText(/8 runs, 2 groups/)).toBeInTheDocument())
  })

  it('distinguishes no-report-yet from a refused report', async () => {
    mocked.listEnsembles.mockResolvedValue([
      ens({ ensemble_id: 'a', name: 'pending-one', status: 'running' }),
      ens({ ensemble_id: 'b', name: 'good-one', has_report: true }),
      ens({ ensemble_id: 'c', name: 'bad-one', report_error: 'only 1 usable run' }),
    ])
    render(<History onOpen={() => {}} onNew={() => {}} onOpenEnsemble={() => {}} />)

    await waitFor(() => expect(screen.getByText('report ready')).toBeInTheDocument())
    expect(screen.getByText('report failed')).toBeInTheDocument()
    expect(screen.getByText('running')).toBeInTheDocument()
  })

  it('opens an ensemble by its own id', async () => {
    const onOpenEnsemble = vi.fn()
    mocked.listEnsembles.mockResolvedValue([ens()])
    render(<History onOpen={() => {}} onNew={() => {}} onOpenEnsemble={onOpenEnsemble} />)

    await waitFor(() => expect(screen.getByText('renewal')).toBeInTheDocument())
    fireEvent.click(screen.getByText('renewal'))
    expect(onOpenEnsemble).toHaveBeenCalledWith('e1')
  })

  it('does not fetch ensembles when there is nowhere to open them', async () => {
    // A list of ensembles nobody can click is worse than no list.
    render(<History onOpen={() => {}} onNew={() => {}} />)
    await waitFor(() => expect(mocked.listRuns).toHaveBeenCalled())
    expect(mocked.listEnsembles).not.toHaveBeenCalled()
  })

  it('still shows the runs when the ensembles route is missing', async () => {
    // An older deployment has no `/api/ensembles`. The run list must not be collateral.
    mocked.listRuns.mockResolvedValue([
      {
        run_id: 'r1', name: 'quiet-harbour', description: 'd', slug: 'quiet-harbour',
        topic: 't', status: 'complete', turn_count: 2, total_cost_usd: 0.01,
        created_at: 1, completed_at: 2, last_event_at: 2,
        parent_run_id: null, branch_turn: null,
      },
    ])
    mocked.listEnsembles.mockRejectedValue(new Error('404 Not Found'))
    render(<History onOpen={() => {}} onNew={() => {}} onOpenEnsemble={() => {}} />)

    await waitFor(() => expect(screen.getByText('quiet-harbour')).toBeInTheDocument())
    expect(screen.queryByText('Ensembles')).not.toBeInTheDocument()
  })

  it('shows no section at all when there are none', async () => {
    mocked.listEnsembles.mockResolvedValue([])
    render(<History onOpen={() => {}} onNew={() => {}} onOpenEnsemble={() => {}} />)

    await waitFor(() => expect(mocked.listEnsembles).toHaveBeenCalled())
    expect(screen.queryByText('Ensembles')).not.toBeInTheDocument()
  })

  it('fetches once even when the parent re-renders with a new callback', async () => {
    // The bug this is for: `onOpenEnsemble` is an inline arrow in App.tsx, so it is a new
    // reference on every render. With it in the effect's dependency array, each re-render tore
    // down the effect — setting `live = false` on the in-flight request — and started another.
    // The runs list loading is itself a re-render, so the first ensembles response was discarded,
    // and against a cold Lambda the section could simply never appear.
    let bump = () => {}
    function Wrapper() {
      const [, setN] = useState(0)
      bump = () => setN((n) => n + 1)
      // A NEW closure each render, exactly as App.tsx passes.
      return (
        <History onOpen={() => {}} onNew={() => {}} onOpenEnsemble={(id) => void id} />
      )
    }

    mocked.listEnsembles.mockResolvedValue([ens()])
    render(<Wrapper />)
    await waitFor(() => expect(screen.getByText('renewal')).toBeInTheDocument())

    act(() => bump())
    act(() => bump())
    await waitFor(() => expect(screen.getByText('renewal')).toBeInTheDocument())

    expect(mocked.listEnsembles).toHaveBeenCalledTimes(1)
  })
})

describe('History — ensembles fold their own conversations', () => {
  const run = (id: string, name: string, over: Record<string, unknown> = {}) => ({
    run_id: id, name, description: 'd', slug: name, topic: 't', status: 'complete',
    turn_count: 2, total_cost_usd: 0.01, created_at: 1, completed_at: 2, last_event_at: 2,
    parent_run_id: null, branch_turn: null, ensemble_id: null, ensemble_cell: null, ...over,
  })

  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    mocked.listRuns.mockResolvedValue([
      run('r0', 'solo-run'),
      run('r1', 'renewal-base-1', { ensemble_id: 'e1', ensemble_cell: 'base' }),
      run('r2', 'renewal-base-2', { ensemble_id: 'e1', ensemble_cell: 'base' }),
    ])
    mocked.listEnsembles.mockResolvedValue([ens()])
  })

  it('keeps members out of the individual list, folded under their ensemble', async () => {
    render(<History onOpen={() => {}} onNew={() => {}} onOpenEnsemble={() => {}} />)
    await waitFor(() => expect(screen.getByText('solo-run')).toBeInTheDocument())
    await waitFor(() => expect(screen.getByText('renewal')).toBeInTheDocument())
    expect(screen.queryByText('renewal-base-1')).not.toBeInTheDocument()
    expect(screen.getByText('Individual conversations').parentElement).toHaveTextContent('(1)')

    fireEvent.click(screen.getByLabelText(/Show the conversations in renewal/))
    expect(screen.getByText('renewal-base-1')).toBeInTheDocument()
    expect(screen.getByText('renewal-base-2')).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText(/Hide the conversations in renewal/))
    expect(screen.queryByText('renewal-base-1')).not.toBeInTheDocument()
  })

  it('opens a nested member as an ordinary run, and unfolding does not open the ensemble', async () => {
    const onOpen = vi.fn()
    const onOpenEnsemble = vi.fn()
    render(<History onOpen={onOpen} onNew={() => {}} onOpenEnsemble={onOpenEnsemble} />)
    await waitFor(() => expect(screen.getByText('renewal')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText(/Show the conversations in renewal/))
    expect(onOpenEnsemble).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText('renewal-base-2'))
    expect(onOpen).toHaveBeenCalledWith('r2')
  })

  it('folds each section, and remembers it', async () => {
    const { unmount } = render(<History onOpen={() => {}} onNew={() => {}} onOpenEnsemble={() => {}} />)
    await waitFor(() => expect(screen.getByText('solo-run')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Individual conversations'))
    expect(screen.queryByText('solo-run')).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('Ensembles'))
    expect(screen.queryByText('renewal')).not.toBeInTheDocument()
    unmount()

    render(<History onOpen={() => {}} onNew={() => {}} onOpenEnsemble={() => {}} />)
    await waitFor(() => expect(mocked.listRuns).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.getByText('Individual conversations')).toBeInTheDocument())
    expect(screen.queryByText('solo-run')).not.toBeInTheDocument()
    expect(screen.queryByText('renewal')).not.toBeInTheDocument()
  })

  it('leaves members in the individual list when their ensemble is not listed', async () => {
    // A failed ensembles request must not make its runs unreachable.
    mocked.listEnsembles.mockRejectedValue(new Error('404 Not Found'))
    render(<History onOpen={() => {}} onNew={() => {}} onOpenEnsemble={() => {}} />)
    await waitFor(() => expect(screen.getByText('renewal-base-1')).toBeInTheDocument())
    expect(screen.getByText('solo-run')).toBeInTheDocument()
  })
})
