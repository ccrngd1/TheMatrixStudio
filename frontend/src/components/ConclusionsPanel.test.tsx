// SPDX-License-Identifier: Apache-2.0
//
// "What did the runs decide, and how consistently?" — four states, and the point of these tests is
// that they stay distinguishable. In particular a report built BEFORE conclusions existed must not
// read as "no run concluded anything", which is a finding.
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ConclusionsPanel } from './ConclusionsPanel'
import type { EnsembleClaim, EnsembleReport } from '../api'

const concl = (claim: string, cells: Record<string, [number, number]>): EnsembleClaim => ({
  claim,
  kind: 'conclusion',
  per_cell: Object.fromEntries(
    Object.entries(cells).map(([c, [held, of]]) => [
      c,
      { held, of, tier: held === of ? 'unanimous' : held === 1 ? 'rare' : 'split', runs: [] },
    ]),
  ),
})

const report = (over: Partial<EnsembleReport>): EnsembleReport =>
  ({
    ensemble_id: 'e1', generated_at: 1, cells: [], missing_members: [], per_persona: {},
    agreements: {}, synthesis: '', cost_usd: 0, clustered: true,
    claims: [
      { claim: 'labwork required', kind: 'demand', per_cell: {
        base: { held: 5, of: 5, tier: 'unanimous', runs: [] },
        hybrid: { held: 0, of: 4, tier: 'absent', runs: [] } } },
    ],
    ...over,
  }) as EnsembleReport

describe('ConclusionsPanel', () => {
  it('shows conclusions counted per group, with no pooled total', () => {
    render(<ConclusionsPanel cells={['base', 'hybrid']} report={report({
      conclusions: [concl('exclude California', { base: [4, 5], hybrid: [1, 4] })] })} />)
    expect(screen.getByText('exclude California')).toBeInTheDocument()
    expect(screen.getByText(/4 of 5/)).toBeInTheDocument()
    expect(screen.getByText(/1 of 4/)).toBeInTheDocument()
    expect(screen.queryByText(/5 of 9/)).not.toBeInTheDocument()
    expect(screen.queryByText(/No conclusion recurred/)).not.toBeInTheDocument()
  })

  it('says a report predates conclusions, and does NOT say no run concluded', () => {
    render(<ConclusionsPanel cells={['base']} report={report({ conclusions: undefined })} />)
    expect(screen.getByText(/built before conclusions were extracted/)).toBeInTheDocument()
    expect(screen.queryByText(/No run reached a conclusion/)).not.toBeInTheDocument()
  })

  it('reports no conclusions at all as the finding', () => {
    render(<ConclusionsPanel cells={['base']} report={report({ conclusions: [] })} />)
    expect(screen.getByText(/No run reached a conclusion/)).toBeInTheDocument()
  })

  it('reports divergence rather than promoting one conclusion to "the" conclusion', () => {
    render(<ConclusionsPanel cells={['base']} report={report({ conclusions: [
      concl('exclude CA', { base: [1, 5] }), concl('launch everywhere', { base: [1, 5] })] })} />)
    expect(screen.getByText(/No conclusion recurred/)).toBeInTheDocument()
  })

  it('lists what every run in a group agreed on, and where', () => {
    render(<ConclusionsPanel cells={['base', 'hybrid']} report={report({ conclusions: [] })} />)
    expect(screen.getByText('labwork required')).toBeInTheDocument()
    expect(screen.getByText(/base: all 5 runs/)).toBeInTheDocument()
    expect(screen.queryByText(/hybrid: all/)).not.toBeInTheDocument()
  })

  it('labels itself as model analysis', () => {
    render(<ConclusionsPanel cells={['base']} report={report({ conclusions: [] })} />)
    expect(screen.getByText(/Model-generated analysis/)).toBeInTheDocument()
  })
})

describe('ConclusionsPanel — recurring vs single-run', () => {
  it('shows recurring conclusions first and collapses the single-run ones, labelled rare', () => {
    render(<ConclusionsPanel cells={['base', 'hybrid']} report={report({ conclusions: [
      concl('exclude net-new', { base: [2, 5], hybrid: [2, 3] }),
      concl('file Ohio certification today', { base: [1, 5] })] })} />)
    expect(screen.getByText('Reached in two or more runs of a group')).toBeInTheDocument()
    const summary = screen.getByText(/Reached in a single run only \(1\) — rare, not a finding/)
    expect(summary.closest('details')).not.toHaveAttribute('open')
  })
})

