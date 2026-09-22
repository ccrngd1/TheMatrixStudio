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
    const { container } = render(
      <ClaimTable claims={METHOD_DEPENDENT} cells={['base', 'hybrid']} />,
    )
    const badge = [...container.querySelectorAll('span[title]')].find((s) =>
      s.textContent?.includes('5 of 5'),
    )
    expect(badge?.getAttribute('title')).toBe('b1, b2, b3, b4, b5')
  })

  it('marks whether a claim is a demand or a refusal', () => {
    render(<ClaimTable claims={METHOD_DEPENDENT} cells={['base']} />)
    expect(screen.getByText('demand')).toBeInTheDocument()
  })

  it('says so plainly when nothing was extracted', () => {
    render(<ClaimTable claims={[]} cells={['base']} />)
    expect(screen.getByText(/No demands or refusals were extracted/)).toBeInTheDocument()
  })
})
