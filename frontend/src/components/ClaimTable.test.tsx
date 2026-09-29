// SPDX-License-Identifier: Apache-2.0
//
// The claim table exists to make one rendering impossible, so most of these tests assert an
// absence.
//
// `docs/ENSEMBLE-CONVERSATIONS.md` §4: the same "5 of 9" is a strong method-dependent finding
// or a coin flip depending on how it splits across groups. A total column would be the one
// number that destroys that distinction, so there must not be one — and "we just didn't build
// it" is not a guarantee, which is why it is pinned.
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ClaimTable } from './ClaimTable'
import type { EnsembleClaim } from '../api'

const METHOD_DEPENDENT: EnsembleClaim[] = [
  {
    claim: 'labwork required',
    kind: 'demand',
    per_cell: {
      base: { held: 5, of: 5, tier: 'unanimous', runs: ['b1', 'b2', 'b3', 'b4', 'b5'] },
      hybrid: { held: 0, of: 4, tier: 'absent', runs: [] },
    },
  },
]

describe('ClaimTable', () => {
  it('shows one column per group and no total', () => {
    render(<ClaimTable claims={METHOD_DEPENDENT} cells={['base', 'hybrid']} />)

    expect(screen.getByText('base')).toBeInTheDocument()
    expect(screen.getByText('hybrid')).toBeInTheDocument()
    // The counts a reader must see, per group.
    expect(screen.getByText(/5 of 5/)).toBeInTheDocument()
    expect(screen.getByText(/0 of 4/)).toBeInTheDocument()
    // And the one they must not: pooled, this claim is 5 of 9, which reads as a weak split
    // and calls for the opposite decision.
    expect(screen.queryByText(/5 of 9/)).not.toBeInTheDocument()
    expect(screen.queryByText(/total/i)).not.toBeInTheDocument()
  })

  it('keeps the declared column order', () => {
    const { container } = render(
      <ClaimTable claims={METHOD_DEPENDENT} cells={['base', 'hybrid']} />,
    )
    const headers = [...container.querySelectorAll('th')].map((h) => h.textContent)
    expect(headers).toEqual(['Claim', 'base', 'hybrid'])
  })

  it('renders an empty group as a dash, never as zero of N', () => {
    // A group that produced no usable conversation has no opinion. "0 of 2" would say every
    // run in it declined the claim, which is a different and false statement.
    render(
      <ClaimTable
        claims={[
          {
            claim: 'video required',
            kind: 'demand',
            per_cell: {
              base: { held: 2, of: 2, tier: 'unanimous', runs: ['b1', 'b2'] },
              hybrid: null,
            },
          },
        ]}
        cells={['base', 'hybrid']}
      />,
    )
    expect(screen.getByText('—')).toBeInTheDocument()
    expect(screen.queryByText(/0 of/)).not.toBeInTheDocument()
  })

  it('labels a single-run claim by replication, not by weakness', () => {
    // `rare` means nothing reproduced it — a claim any group size supports. Calling it "weak"
    // would invite the 3-of-5 against 2-of-5 comparison five runs cannot support.
    render(
      <ClaimTable
        claims={[
          {
            claim: 'indemnification in writing',
            kind: 'refusal',
            per_cell: { base: { held: 1, of: 5, tier: 'rare', runs: ['b3'] } },
          },
        ]}
        cells={['base']}
      />,
    )
    expect(screen.getByText(/1 run only/)).toBeInTheDocument()
    expect(screen.queryByText(/weak/i)).not.toBeInTheDocument()
  })

  it('gives 3 of 5 and 2 of 5 the same label', () => {
    // The property §8.2 demands: at this N those are the same finding, so the table must not
    // distinguish them.
    render(
      <ClaimTable
        claims={[
          {
            claim: 'a',
            kind: 'demand',
            per_cell: { base: { held: 3, of: 5, tier: 'split', runs: [] } },
          },
          {
            claim: 'b',
            kind: 'demand',
            per_cell: { base: { held: 2, of: 5, tier: 'split', runs: [] } },
          },
        ]}
        cells={['base']}
      />,
    )
    expect(screen.getAllByText(/some runs/)).toHaveLength(2)
  })

  it('says which runs held a claim', () => {
    render(<ClaimTable claims={METHOD_DEPENDENT} cells={['base', 'hybrid']} />)
    // Behind a tap-to-open ⓘ now, not a hover title a phone cannot reach (docs/MOBILE-UI.md §2).
    const tips = screen.getAllByRole('tooltip').map((t) => t.textContent)
    expect(tips).toContain('b1, b2, b3, b4, b5')
  })

  it('marks whether a claim is a demand or a refusal', () => {
    render(<ClaimTable claims={METHOD_DEPENDENT} cells={['base']} />)
    expect(screen.getByText('demand')).toBeInTheDocument()
  })

  it('says so plainly when nothing was extracted', () => {
    render(<ClaimTable claims={[]} cells={['base']} />)
    expect(screen.getByText(/No demands or refusals were extracted/)).toBeInTheDocument()
  })

  it('withholds the table when the counts came from text matching', () => {
    // Measured: text matching put 128 of 128 claims in their own group, so every row read
    // "1 run only" while the synthesis found four conclusions in 5 of 5. That is not a rough
    // approximation of the truth, it is the opposite of it — and a warning ABOVE a table of
    // numbers loses to the table.
    render(<ClaimTable claims={METHOD_DEPENDENT} cells={['base', 'hybrid']} clustered={false} />)

    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(screen.getByText(/not available for this report/)).toBeInTheDocument()
    expect(screen.getByText(/would read as total disagreement/)).toBeInTheDocument()
  })

  it('renders normally when clustered, and when the field is absent', () => {
    // Absent means a report stored before the field existed; it still renders.
    const { unmount } = render(
      <ClaimTable claims={METHOD_DEPENDENT} cells={['base']} clustered={true} />,
    )
    expect(screen.getByRole('table')).toBeInTheDocument()
    unmount()

    render(<ClaimTable claims={METHOD_DEPENDENT} cells={['base']} />)
    expect(screen.getByRole('table')).toBeInTheDocument()
  })

  it('shows what was grouped, so a merge can be disputed', () => {
    render(
      <ClaimTable
        claims={[
          {
            claim: 'verified weight before approval',
            kind: 'demand',
            per_cell: { base: { held: 3, of: 3, tier: 'unanimous', runs: ['m1', 'm2', 'm3'] } },
            variants: [
              { text: 'a verified weight before approval', runs: ['m1'] },
              { text: 'a confirmed weight is required first', runs: ['m2'] },
              { text: 'weight must be verified before we approve', runs: ['m3'] },
            ],
          },
        ]}
        cells={['base']}
      />,
    )
    expect(screen.getByText('3 phrasings grouped')).toBeInTheDocument()
    expect(screen.getByText(/a confirmed weight is required first/)).toBeInTheDocument()
  })

  it('does not offer an audit trail when nothing was merged', () => {
    // A cluster of one has nothing to dispute; a disclosure there is noise on every row.
    render(
      <ClaimTable
        claims={[
          {
            claim: 'only this one',
            kind: 'demand',
            per_cell: { base: { held: 1, of: 3, tier: 'rare', runs: ['m1'] } },
            variants: [{ text: 'only this one', runs: ['m1'] }],
          },
        ]}
        cells={['base']}
      />,
    )
    expect(screen.queryByText(/phrasings grouped/)).not.toBeInTheDocument()
  })
})
