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
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
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
    description: 'renewal',
    topic: 'renewal',
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
})
