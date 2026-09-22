// SPDX-License-Identifier: Apache-2.0
//
// The ensemble view. Three behaviours are load-bearing rather than cosmetic.
//
// **"Settled" is not "reported".** `report_ready` says the aggregator MAY run; the last member
// flips its own status and then generates, in that order. So there is a real window where every
// conversation is finished and no report exists, and the view must call that "building" rather
// than presenting it as a finished state with nothing in it.
//
// **A missing member is shown.** A group of five that produced three runs must not read as
// three of three. That is the censoring the whole design is built against, and here it would
// arrive through a UI that filtered nulls.
//
// **Groups are never pooled.** Delegated to `ClaimTable`, which has its own tests; what is
// checked here is that the column order comes from the stored spec, so a group whose runs all
// failed still gets a column.
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { EnsembleView } from './EnsembleView'
import { api } from '../api'

vi.mock('../api', () => ({
  api: { getEnsemble: vi.fn(), generateEnsembleReport: vi.fn() },
}))

const mocked = api as unknown as {
  getEnsemble: ReturnType<typeof vi.fn>
  generateEnsembleReport: ReturnType<typeof vi.fn>
}

function member(id: string, cell: string, index: number, status = 'complete') {
  return {
    run_id: id,
    cell,
    index,
    run: {
      run_id: id,
      name: `${cell}-${index}`,
      description: null,
      slug: null,
      topic: 't',
      status,
      turn_count: 4,
      total_cost_usd: 0.12,
      created_at: 1,
      completed_at: 2,
      last_event_at: 2,
      parent_run_id: null,
      branch_turn: null,
    },
  }
}

function detail(over: Record<string, unknown> = {}) {
  return {
    ensemble_id: 'e1',
    name: 'renewal',
    description: 'd',
    topic: 'renewal renewal',
    status: 'running',
    created_at: 1,
    completed_at: null,
    spec: [{ label: 'base', n: 2, overrides: {} }],
    base_config: { max_messages: 4 },
    has_report: false,
    report: null,
    report_generated_at: null,
    report_cost_usd: null,
    report_error: null,
    members: [member('r1', 'base', 1), member('r2', 'base', 2)],
    cells: [{ cell: 'base', declared: 2, complete: 2, settled: 2 }],
    report_ready: true,
    ...over,
  }
}

const REPORT = {
  ensemble_id: 'e1',
  generated_at: 10,
  cells: [
    { cell: 'base', declared: 2, usable: 2, runs: [] },
    { cell: 'hybrid', declared: 2, usable: 2, runs: [] },
  ],
  missing_members: [],
  claims: [
    {
      claim: 'labwork required',
      kind: 'demand' as const,
      per_cell: {
        base: { held: 2, of: 2, tier: 'unanimous' as const, runs: ['base-1', 'base-2'] },
        hybrid: { held: 0, of: 2, tier: 'absent' as const, runs: [] },
      },
    },
  ],
  per_persona: {},
  agreements: {},
  synthesis: '## Across the runs\nIt held everywhere.',
  cost_usd: 0.0712,
  caveats: ['Claim counts are a FLOOR.', 'The largest cell has 2 usable run(s).'],
}

describe('EnsembleView', () => {
  beforeEach(() => vi.clearAllMocks())

  it('lists the groups with what each one varied', async () => {
    mocked.getEnsemble.mockResolvedValue(
      detail({
        spec: [
          { label: 'base', n: 2, overrides: {} },
          { label: 'hybrid', n: 2, overrides: { 'selection.method': 'hybrid' } },
        ],
      }),
    )
    render(<EnsembleView ensembleId="e1" onBack={() => {}} onOpenRun={() => {}} />)

    // Scoped to the Groups section: a bare `getByText('base')` is ambiguous here, and
    // legitimately so — a label appears as a group, as each member's column, and as a report
    // header.
    const groups = await waitFor(() =>
      screen.getByText('Groups').closest('section') as HTMLElement,
    )
    expect(groups).toHaveTextContent('base — 2 runs')
    expect(groups).toHaveTextContent('hybrid — 2 runs')
    // Saying "nothing varied" out loud, because that is the point of the default mode and a
    // blank space would read as missing information.
    expect(groups).toHaveTextContent('nothing varied')
    expect(groups).toHaveTextContent('selection.method=hybrid')
  })

  it('opens a member as an ordinary run', async () => {
    const onOpenRun = vi.fn()
    mocked.getEnsemble.mockResolvedValue(detail())
    render(<EnsembleView ensembleId="e1" onBack={() => {}} onOpenRun={onOpenRun} />)

    await waitFor(() => expect(screen.getByText('base-1')).toBeInTheDocument())
    fireEvent.click(screen.getByText('base-1'))
    expect(onOpenRun).toHaveBeenCalledWith('r1')
  })

  it('shows a member that was never created, and says the group is short', async () => {
    mocked.getEnsemble.mockResolvedValue(
      detail({
        members: [member('r1', 'base', 1), { run_id: 'r2', cell: 'base', index: 2, run: null }],
        cells: [{ cell: 'base', declared: 2, complete: 1, settled: 2 }],
      }),
    )
    render(<EnsembleView ensembleId="e1" onBack={() => {}} onOpenRun={() => {}} />)

    await waitFor(() => expect(screen.getByText(/never started/)).toBeInTheDocument())
    expect(screen.getByText(/this group is short/)).toBeInTheDocument()
  })

  it('calls a settled ensemble with no report "building", not finished', async () => {
    mocked.getEnsemble.mockResolvedValue(detail({ report_ready: true, has_report: false }))
    render(<EnsembleView ensembleId="e1" onBack={() => {}} onOpenRun={() => {}} />)

    await waitFor(() => expect(screen.getByText(/Building the report/)).toBeInTheDocument())
  })

  it('explains the wait while conversations are still running', async () => {
    // And does NOT offer a build button: the server refuses with 409, and a control that
    // always fails is worse than no control.
    mocked.getEnsemble.mockResolvedValue(
      detail({
        report_ready: false,
        members: [member('r1', 'base', 1), member('r2', 'base', 2, 'running')],
        cells: [{ cell: 'base', declared: 2, complete: 1, settled: 1 }],
      }),
    )
    render(<EnsembleView ensembleId="e1" onBack={() => {}} onOpenRun={() => {}} />)

    await waitFor(() =>
      expect(screen.getByText(/Waiting for 1 conversation to finish/)).toBeInTheDocument(),
    )
    expect(screen.getByText(/had not reached it yet/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Build it now/ })).not.toBeInTheDocument()
  })

  it('renders the report per group, using the spec for column order', async () => {
    mocked.getEnsemble.mockResolvedValue(
      detail({
        has_report: true,
        report: REPORT,
        report_cost_usd: 0.0712,
        spec: [
          { label: 'base', n: 2, overrides: {} },
          { label: 'hybrid', n: 2, overrides: { 'selection.method': 'hybrid' } },
        ],
      }),
    )
    const { container } = render(
      <EnsembleView ensembleId="e1" onBack={() => {}} onOpenRun={() => {}} />,
    )

    await waitFor(() => expect(screen.getByText('labwork required')).toBeInTheDocument())
    const headers = [...container.querySelectorAll('th')].map((h) => h.textContent)
    expect(headers).toEqual(['Claim', 'base', 'hybrid'])

    // Read off the claim row rather than the page: "2 of 2" also appears in the
    // "Conversations (2 of 2 finished)" heading, which is a different fact about the same
    // numbers.
    const cells = [...container.querySelectorAll('tbody tr td')].map((td) => td.textContent)
    expect(cells[1]).toMatch(/2 of 2 · every run/)
    expect(cells[2]).toMatch(/0 of 2 · none/)
  })

  it('shows the report own caveats alongside it', async () => {
    // Not behind a link or a disclosure. A reader who sees "1 of 5" needs to know the matcher
    // under-merges BEFORE they conclude a persona changed their mind.
    mocked.getEnsemble.mockResolvedValue(detail({ has_report: true, report: REPORT }))
    render(<EnsembleView ensembleId="e1" onBack={() => {}} onOpenRun={() => {}} />)

    await waitFor(() => expect(screen.getByText(/Claim counts are a FLOOR/)).toBeInTheDocument())
    expect(screen.getByText(/largest cell has 2 usable/)).toBeInTheDocument()
  })

  it('shows the synthesis and what the report cost', async () => {
    mocked.getEnsemble.mockResolvedValue(detail({ has_report: true, report: REPORT }))
    render(<EnsembleView ensembleId="e1" onBack={() => {}} onOpenRun={() => {}} />)

    await waitFor(() => expect(screen.getByText(/It held everywhere/)).toBeInTheDocument())
    expect(screen.getByText(/\$0\.0712/)).toBeInTheDocument()
  })

  it('surfaces a refused report with a retry', async () => {
    mocked.getEnsemble.mockResolvedValue(
      detail({ report_error: 'Only 1 of 5 member(s) produced a usable extraction' }),
    )
    mocked.generateEnsembleReport.mockResolvedValue({})
    render(<EnsembleView ensembleId="e1" onBack={() => {}} onOpenRun={() => {}} />)

    await waitFor(() => expect(screen.getByText(/usable extraction/)).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /Try again/ }))
    // Forced, because a recorded failure already holds the claim and an unforced retry would
    // be refused as "already claimed".
    await waitFor(() => expect(mocked.generateEnsembleReport).toHaveBeenCalledWith('e1', true))
  })

  it('treats a generate timeout as still building, not as a failure', async () => {
    // Expected against the deployed stack: the extraction pass plus a 20k-token synthesis
    // outlasts the gateway limit while the work continues server-side. Saying "failed" would
    // send the operator to retry something that is already running.
    mocked.getEnsemble.mockResolvedValue(detail())
    mocked.generateEnsembleReport.mockRejectedValue(new Error('504 Gateway Timeout'))
    render(<EnsembleView ensembleId="e1" onBack={() => {}} onOpenRun={() => {}} />)

    await waitFor(() => expect(screen.getByRole('button', { name: /Build it now/ })).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /Build it now/ }))

    await waitFor(() =>
      expect(screen.getByText(/still being built server-side/)).toBeInTheDocument(),
    )
  })

  it('reports a load failure instead of rendering an empty ensemble', async () => {
    // "This ensemble has nothing" and "the request failed" look identical otherwise, and the
    // first is a lie.
    mocked.getEnsemble.mockRejectedValue(new Error('network down'))
    render(<EnsembleView ensembleId="e1" onBack={() => {}} onOpenRun={() => {}} />)

    await waitFor(() => expect(screen.getByText(/network down/)).toBeInTheDocument())
  })
})
