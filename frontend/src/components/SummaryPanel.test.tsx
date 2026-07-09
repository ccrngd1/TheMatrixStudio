// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SummaryPanel } from './SummaryPanel'
import type { StoredSummary } from '../types'

const generated: StoredSummary = {
  id: 1,
  run_id: 'r1',
  kind: 'generated',
  payload: {
    overview: 'The group debated a pet-food reauthorization policy.',
    consensus: ['A vet sign-off gate is needed'],
    dissenters: [{ speaker: 'Dr. Webb', position: 'Liability is unresolved' }],
    key_ideas: ['Tiered reauthorization windows'],
    open_questions: ['Who owns the audit trail?'],
  },
  tokens_in: 1200,
  tokens_out: 300,
  cost_usd: 0.0042,
  created_at: 1,
  parsed: true,
}

const imported: StoredSummary = {
  id: 2,
  run_id: 'r1',
  kind: 'imported',
  payload: { overview: 'Original legacy summary text.' },
  tokens_in: 0,
  tokens_out: 0,
  cost_usd: 0,
  created_at: 0,
}

describe('SummaryPanel', () => {
  it('renders structured fields and labels analysis as model-generated', () => {
    render(
      <SummaryPanel
        runId="r1"
        generated={generated}
        imported={null}
        canGenerate
        onUpdated={() => {}}
      />,
    )
    expect(screen.getByText(/model-generated analysis/i)).toBeInTheDocument()
    expect(screen.getByText(/pet-food reauthorization/i)).toBeInTheDocument()
    expect(screen.getByText('A vet sign-off gate is needed')).toBeInTheDocument()
    expect(screen.getByText(/Dr\. Webb/)).toBeInTheDocument()
    expect(screen.getByText('Tiered reauthorization windows')).toBeInTheDocument()
    expect(screen.getByText('Who owns the audit trail?')).toBeInTheDocument()
    // Analysis cost is shown and flagged separate from the run.
    expect(screen.getByText(/counted\s+separately from the run/i)).toBeInTheDocument()
  })

  it('shows an imported original separately from a generated summary', () => {
    render(
      <SummaryPanel
        runId="r1"
        generated={generated}
        imported={imported}
        canGenerate
        onUpdated={() => {}}
      />,
    )
    expect(screen.getByText(/original \(imported\) summary/i)).toBeInTheDocument()
    expect(screen.getByText('Original legacy summary text.')).toBeInTheDocument()
    // Both coexist — the generated overview is still present.
    expect(screen.getByText(/pet-food reauthorization/i)).toBeInTheDocument()
  })
})
