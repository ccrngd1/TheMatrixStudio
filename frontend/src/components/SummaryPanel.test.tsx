// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { SummaryPanel } from './SummaryPanel'
import type { StoredSummary } from '../types'

const DEFAULT_INSTRUCTIONS =
  'You are a neutral analyst summarizing a finished multi-agent conversation. ' +
  'Read the transcript and produce a STRUCTURED analysis.'

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
        defaultInstructions={DEFAULT_INSTRUCTIONS}
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
        defaultInstructions={DEFAULT_INSTRUCTIONS}
        canGenerate
        onUpdated={() => {}}
      />,
    )
    expect(screen.getByText(/original \(imported\) summary/i)).toBeInTheDocument()
    expect(screen.getByText('Original legacy summary text.')).toBeInTheDocument()
    // Both coexist — the generated overview is still present.
    expect(screen.getByText(/pet-food reauthorization/i)).toBeInTheDocument()
  })

  it('regenerate reveals an editable prompt prefilled with the current/default instructions', () => {
    // No custom instructions on the current summary → editor prefills default.
    render(
      <SummaryPanel
        runId="r1"
        generated={generated}
        imported={null}
        defaultInstructions={DEFAULT_INSTRUCTIONS}
        canGenerate
        onUpdated={() => {}}
      />,
    )
    // No textarea until the user opens the regenerate editor.
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /regenerate/i }))
    const textarea = screen.getByRole('textbox') as HTMLTextAreaElement
    expect(textarea.value).toBe(DEFAULT_INSTRUCTIONS)
    // Helper text makes clear the guardrails are enforced and not editable.
    expect(screen.getByText(/not editable/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /reset to default/i })).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /regenerate with this prompt/i }),
    ).toBeInTheDocument()
  })

  it('prefills the editor with the custom prompt that created the summary', () => {
    const custom = 'You are a snarky debate coach.'
    render(
      <SummaryPanel
        runId="r1"
        generated={{ ...generated, instructions: custom }}
        imported={null}
        defaultInstructions={DEFAULT_INSTRUCTIONS}
        canGenerate
        onUpdated={() => {}}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: /regenerate/i }))
    const textarea = screen.getByRole('textbox') as HTMLTextAreaElement
    expect(textarea.value).toBe(custom)
  })
})
