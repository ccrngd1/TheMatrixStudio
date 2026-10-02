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
    overview: 'The group debated a member-food renewal policy.',
    consensus: ['A provider sign-off gate is needed'],
    dissenters: [{ speaker: 'Dr. Webb', position: 'Liability is unresolved' }],
    key_ideas: ['Tiered renewal windows'],
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
  it('shows what would settle it, with unstated columns marked as gaps', () => {
    const withPlan: StoredSummary = {
      ...generated,
      payload: {
        ...generated.payload,
        conditional_recommendation: 'If churn is under 5%, launch; if not, hold. Lean hold.',
        evidence_plan: [
          {
            data: 'Pilot churn',
            asked_by: 'Dana',
            decision: 'launch or hold',
            moves_them: 'under 5%',
            best_guess: 'not stated',
            cheapest_way: 'a two-week pilot',
          },
        ],
      },
    }
    render(
      <SummaryPanel
        runId="r1"
        generated={withPlan}
        imported={null}
        defaultInstructions={DEFAULT_INSTRUCTIONS}
        canGenerate
        onUpdated={() => {}}
      />,
    )
    expect(screen.getByText('What would settle it')).toBeInTheDocument()
    expect(screen.getByText(/Lean hold/)).toBeInTheDocument()
    expect(screen.getByText('Pilot churn')).toBeInTheDocument()
    expect(screen.getByText('a two-week pilot')).toBeInTheDocument()
    expect(screen.getByText('not stated')).toHaveClass('italic')
  })

  // The analysis reads each persona's underlying concerns in both modes (owner decision, 2026-10-02).
  const withConcerns = (concerns_withheld: boolean): StoredSummary => ({
    ...generated,
    payload: {
      ...generated.payload,
      concerns_withheld,
      concerns: [
        { speaker: 'Dana', concern: 'My team takes the pager if this slips', surfaced: 'partly',
          where: 'turn 4: “someone has to carry it”', addressed: 'no' },
        { speaker: 'Ravi', concern: 'The budget line has my name on it', surfaced: 'no',
          where: 'not stated', addressed: 'no' },
      ],
    },
  })
  const renderSummary = (g: StoredSummary) =>
    render(
      <SummaryPanel runId="r1" generated={g} imported={null} defaultInstructions={DEFAULT_INSTRUCTIONS}
        canGenerate onUpdated={() => {}} />,
    )

  it('reveals a withheld run’s concerns, labelled as hidden during the run', () => {
    renderSummary(withConcerns(true))
    expect(screen.getByText('Underlying concerns (hidden during the run)')).toBeInTheDocument()
    expect(screen.getByText(/kept these to themselves during the run/)).toBeInTheDocument()
    expect(screen.getByText(/My team takes the pager if this slips/)).toBeInTheDocument()
    expect(screen.getByText('turn 4: “someone has to carry it”')).toBeInTheDocument()
    // A concern that never came up is the gap it is.
    expect(screen.getByText('not stated')).toHaveClass('italic')
  })

  it('lists a plain run’s concerns without saying they were hidden', () => {
    renderSummary(withConcerns(false))
    expect(screen.getByText('Underlying concerns')).toBeInTheDocument()
    expect(screen.queryByText(/hidden during the run/)).not.toBeInTheDocument()
    expect(screen.getByText(/The budget line has my name on it/)).toBeInTheDocument()
    expect(screen.getAllByText('addressed')).toHaveLength(2)
  })

  it('shows no concerns block for a summary without any', () => {
    renderSummary(generated)
    expect(screen.queryByText(/Underlying concerns/)).not.toBeInTheDocument()
  })

  // A reply that left the overview out is stored with overview "" (and, since 2026-10-01, `omitted`).
  // It used to make the Overview block vanish, so the panel looked like a summary without one.
  it.each([
    ['flagged as omitted', { overview: '', omitted: ['overview'] }],
    ['stored before the flag existed', { overview: '' }],
  ])('says an overview the reply left out is not stated (%s)', (_, missing) => {
    render(
      <SummaryPanel
        runId="r1"
        generated={{ ...generated, payload: { ...generated.payload, ...missing } }}
        imported={null}
        defaultInstructions={DEFAULT_INSTRUCTIONS}
        canGenerate
        onUpdated={() => {}}
      />,
    )
    expect(screen.getByText('Overview')).toBeInTheDocument()
    expect(screen.getByText('not stated')).toHaveClass('italic')
    // The rest of the summary is still shown.
    expect(screen.getByText('A provider sign-off gate is needed')).toBeInTheDocument()
  })

  it('shows no Overview block when the overview was not requested', () => {
    render(
      <SummaryPanel
        runId="r1"
        generated={{ ...generated, payload: { consensus: ['A provider sign-off gate is needed'] } }}
        imported={null}
        defaultInstructions={DEFAULT_INSTRUCTIONS}
        canGenerate
        onUpdated={() => {}}
      />,
    )
    expect(screen.queryByText('Overview')).not.toBeInTheDocument()
    expect(screen.queryByText('not stated')).not.toBeInTheDocument()
  })

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
    expect(screen.getByText(/member-food renewal/i)).toBeInTheDocument()
    expect(screen.getByText('A provider sign-off gate is needed')).toBeInTheDocument()
    expect(screen.getByText(/Dr\. Webb/)).toBeInTheDocument()
    expect(screen.getByText('Tiered renewal windows')).toBeInTheDocument()
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
    expect(screen.getByText(/member-food renewal/i)).toBeInTheDocument()
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
