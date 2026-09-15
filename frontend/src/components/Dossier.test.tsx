// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { Dossier } from './Dossier'
import type { AgentView } from '../types'
import { api } from '../api'

vi.mock('../api', () => ({
  api: {
    getDossier: vi.fn(),
    getTurnTrace: vi.fn(),
  },
}))

const agent: AgentView = {
  name: 'Ada',
  persona: 'A cautious ethicist',
  goals: ['Raise risks'],
  portrait: null,
  portraitKey: null,
  portraitUrl: null,
  avatarResolved: true,
  messageCount: 1,
  tokensIn: 10,
  tokensOut: 5,
  costUsd: 0.001,
}

const feed = [{ turn: 1, seq: 2, speaker: 'Ada', content: 'Hello there' }]

describe('Dossier', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders real persona/goals/messages and an honest not-captured state for cognition-off runs', async () => {
    ;(api.getDossier as ReturnType<typeof vi.fn>).mockResolvedValue({
      run_id: 'r1', agent: 'Ada', persona: 'A cautious ethicist', goals: ['Raise risks'],
      memory_stream: [], beliefs: [], relationships: {},
      tokens_in: 10, tokens_out: 5, cost_usd: 0.001, portrait_b64: null,
    })
    render(<Dossier agent={agent} feed={feed} runId="r1" onClose={() => {}} />)

    expect(screen.getByText('A cautious ethicist')).toBeInTheDocument()
    expect(screen.getByText('Raise risks')).toBeInTheDocument()
    expect(screen.getByText('Hello there')).toBeInTheDocument()

    // Honesty gate: no fabricated cognition; explicit not-captured message.
    await waitFor(() =>
      expect(screen.getByText(/created without cognition/i)).toBeInTheDocument(),
    )
    // No "why?" trace button when there is no captured cognition.
    expect(screen.queryByText('why?')).not.toBeInTheDocument()
  })

  it('does NOT claim the run had cognition off when cognition was enabled but lost', async () => {
    // The message this replaces asserted "created without cognition" from an empty
    // memory stream. On run 2d2ac45b that was false for four of six personas:
    // cognition was on and a strict JSON parse was discarding its output.
    ;(api.getDossier as ReturnType<typeof vi.fn>).mockResolvedValue({
      run_id: 'r1', agent: 'Ada', persona: 'A cautious ethicist', goals: ['Raise risks'],
      memory_stream: [], beliefs: [], relationships: {},
      cognition_enabled: true, cognition_lost_turns: 3,
      tokens_in: 10, tokens_out: 5, cost_usd: 0.001, portrait_b64: null,
    })
    render(<Dossier agent={agent} feed={feed} runId="r1" onClose={() => {}} />)

    await waitFor(() =>
      expect(screen.getByText(/could not be read/i)).toBeInTheDocument(),
    )
    expect(screen.getByText(/3 of this persona/i)).toBeInTheDocument()
    expect(screen.queryByText(/created without cognition/i)).not.toBeInTheDocument()
  })

  it('says a quiet persona formed nothing, rather than blaming the run', async () => {
    ;(api.getDossier as ReturnType<typeof vi.fn>).mockResolvedValue({
      run_id: 'r1', agent: 'Ada', persona: 'A cautious ethicist', goals: ['Raise risks'],
      memory_stream: [], beliefs: [], relationships: {},
      cognition_enabled: true, cognition_lost_turns: 0,
      tokens_in: 10, tokens_out: 5, cost_usd: 0.001, portrait_b64: null,
    })
    render(<Dossier agent={agent} feed={feed} runId="r1" onClose={() => {}} />)

    await waitFor(() =>
      expect(screen.getByText(/did not form any memories/i)).toBeInTheDocument(),
    )
    expect(screen.queryByText(/created without cognition/i)).not.toBeInTheDocument()
  })

  it('renders the memory stream and a why-trace affordance when cognition was captured', async () => {
    ;(api.getDossier as ReturnType<typeof vi.fn>).mockResolvedValue({
      run_id: 'r1', agent: 'Ada', persona: 'A cautious ethicist', goals: ['Raise risks'],
      memory_stream: [
        { id: 'm1', content: 'the group values consent', importance: 0.8, tags: ['fact'], timestamp: 1 },
      ],
      beliefs: [{ id: 'b1', content: 'consent is the crux', importance: 0.9, tags: ['reflection'], timestamp: 2 }],
      relationships: { Ben: 'trusted ally' },
      tokens_in: 10, tokens_out: 5, cost_usd: 0.001, portrait_b64: null,
    })
    render(<Dossier agent={agent} feed={feed} runId="r1" onClose={() => {}} />)

    await waitFor(() =>
      expect(screen.getByText('the group values consent')).toBeInTheDocument(),
    )
    expect(screen.getByText('consent is the crux')).toBeInTheDocument()
    expect(screen.getByText('trusted ally')).toBeInTheDocument()
    // The "why did it say that?" affordance is present for a captured run.
    expect(screen.getByText('why?')).toBeInTheDocument()
  })

  // ------------------------------------------------------------------
  // The why-trace must not present a fallback as a decision
  // ------------------------------------------------------------------

  const capturedDossier = {
    run_id: 'r1', agent: 'Ada', persona: 'A cautious ethicist', goals: ['Raise risks'],
    memory_stream: [
      { id: 'm1', content: 'the group values consent', importance: 0.8, tags: ['fact'], timestamp: 1 },
    ],
    beliefs: [], relationships: {},
    tokens_in: 10, tokens_out: 5, cost_usd: 0.001, portrait_b64: null,
  }

  async function openTrace(trace: Record<string, unknown>) {
    ;(api.getDossier as ReturnType<typeof vi.fn>).mockResolvedValue(capturedDossier)
    ;(api.getTurnTrace as ReturnType<typeof vi.fn>).mockResolvedValue({
      run_id: 'r1', turn: 1, available: true, speaker: 'Ada', ...trace,
    })
    render(<Dossier agent={agent} feed={feed} runId="r1" onClose={() => {}} />)
    await waitFor(() => expect(screen.getByText('why?')).toBeInTheDocument())
    screen.getByText('why?').click()
  }

  it('says nothing chose the speaker when selection fell back', async () => {
    // A drawn name with a plausible-looking reason beside it is exactly the confusion
    // `selection_fallback` exists to end: the reason describes what the moderator wanted,
    // and on this turn the moderator never answered.
    await openTrace({
      selection_fallback: 'call_failed',
      selection_reason: null,
      rationale: 'I wanted to raise consent',
    })
    await waitFor(() =>
      expect(screen.getByText(/drawn at random/i)).toBeInTheDocument(),
    )
    expect(screen.getByText(/selection call failed/i)).toBeInTheDocument()
    expect(screen.queryByText('Chosen because')).not.toBeInTheDocument()
  })

  it('names the unresolved case differently from a failed call', async () => {
    await openTrace({
      selection_fallback: 'unresolved',
      selection_reason: 'the CFO should answer',
      rationale: 'I wanted to raise consent',
    })
    await waitFor(() =>
      expect(screen.getByText(/named nobody in the cast/i)).toBeInTheDocument(),
    )
    // The moderator's reason is NOT shown as the reason this speaker was chosen — it
    // asked for somebody who does not exist.
    expect(screen.queryByText(/the CFO should answer/)).not.toBeInTheDocument()
  })

  it('shows the moderator’s reason on a healthy turn', async () => {
    await openTrace({
      selection_reason: 'Ada was addressed directly',
      rationale: 'I wanted to raise consent',
    })
    await waitFor(() =>
      expect(screen.getByText('Ada was addressed directly')).toBeInTheDocument(),
    )
    expect(screen.queryByText(/drawn at random/i)).not.toBeInTheDocument()
  })

  // ------------------------------------------------------------------
  // Phase 6 — convictions, and the leakage guard that matters most
  // ------------------------------------------------------------------

  const structuredPayload = {
    role: 'Head of Distribution',
    background: {
      tenure_years: 9,
      formative_events: [
        { year: 2023, event: 'A quickstart needing a vector database', lesson: 'Extra services cost you users' },
      ],
    },
    preferences: {
      optimises_for: ['time-to-first-run'],
      dismisses: ['retrieval answer quality'],
      persuaded_by: ['a clean-machine install'],
    },
    viewpoints: [
      {
        position: 'No feature may add a stateful external service',
        formed_by: 'The 2023 product that stalled at the install step',
        firmness: 'firm' as const,
        evidence_that_shifts: ['an embedded index that is a file'],
      },
    ],
  }

  const baseDossier = {
    run_id: 'r1', agent: 'Ada', persona: 'A cautious ethicist', goals: ['Raise risks'],
    memory_stream: [], beliefs: [], relationships: {},
    tokens_in: 10, tokens_out: 5, cost_usd: 0.001, portrait_b64: null,
  }

  it('renders convictions with firmness and the exit condition', async () => {
    ;(api.getDossier as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...baseDossier, structured: structuredPayload,
    })
    render(<Dossier agent={agent} feed={feed} runId="r1" onClose={() => {}} />)

    await waitFor(() =>
      expect(screen.getByText('No feature may add a stateful external service')).toBeInTheDocument(),
    )
    expect(screen.getByText('firm')).toBeInTheDocument()
    expect(screen.getByText(/an embedded index that is a file/)).toBeInTheDocument()
    expect(screen.getByText(/The 2023 product that stalled/)).toBeInTheDocument()
    expect(screen.getByText(/time-to-first-run/)).toBeInTheDocument()
    expect(screen.getByText(/retrieval answer quality/)).toBeInTheDocument()
    expect(screen.getByText(/Extra services cost you users/)).toBeInTheDocument()
  })

  it('NEVER renders a withheld concern or a validity note, even if the API sends them', async () => {
    // The backend strips both fields. This asserts the UI is a second line of
    // defence rather than trusting that: drawing the real concern out in
    // conversation is the whole exercise, and an operator who can read it off a
    // panel has been handed the answer. `validity` is the operator's private
    // calibration note and must never be displayed either.
    ;(api.getDossier as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...baseDossier,
      structured: {
        ...structuredPayload,
        viewpoints: [
          {
            ...structuredPayload.viewpoints[0],
            underlying_concern: 'I OWN THE FAILURE WHEN A CUSTOMER NEVER GETS A WORKING RUN',
            validity: 'overgeneralised',
          },
        ],
      },
    })
    render(<Dossier agent={agent} feed={feed} runId="r1" onClose={() => {}} />)

    await waitFor(() =>
      expect(screen.getByText('No feature may add a stateful external service')).toBeInTheDocument(),
    )
    expect(screen.queryByText(/I OWN THE FAILURE/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/overgeneralised/i)).not.toBeInTheDocument()
  })

  it('flags a defended position with no exit condition as unfalsifiable', async () => {
    // An authoring gap the operator is the only one who can fix, so it is surfaced
    // rather than hidden.
    ;(api.getDossier as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...baseDossier,
      structured: {
        viewpoints: [
          { position: 'Security signs off first', firmness: 'requires-escalation' as const },
        ],
      },
    })
    render(<Dossier agent={agent} feed={feed} runId="r1" onClose={() => {}} />)

    await waitFor(() => expect(screen.getByText('requires-escalation')).toBeInTheDocument())
    expect(screen.getByText(/no exit condition named/i)).toBeInTheDocument()
  })

  it('renders nothing for a run that used no structured personas', async () => {
    ;(api.getDossier as ReturnType<typeof vi.fn>).mockResolvedValue({ ...baseDossier })
    render(<Dossier agent={agent} feed={feed} runId="r1" onClose={() => {}} />)

    await waitFor(() => expect(screen.getByText('A cautious ethicist')).toBeInTheDocument())
    expect(screen.queryByText(/Convictions/i)).not.toBeInTheDocument()
  })
})