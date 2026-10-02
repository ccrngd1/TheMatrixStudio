// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { Dossier } from './Dossier'
import type { AgentView, StanceBasisEntry, StanceState } from '../types'
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

// The dossier is four tabs (docs/MOBILE-UI.md §4.4) and renders only the open one, so a test opens the tab
// its content lives on before asserting on it.
function openTab(name: 'Convictions' | 'Memory' | 'Threads' | 'Why?') {
  fireEvent.click(screen.getByRole('tab', { name }))
}
const tabPanel = () => screen.getByRole('tabpanel')

describe('Dossier', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('heads the dossier with the persona’s name and the simulated-persona marker', async () => {
    ;(api.getDossier as ReturnType<typeof vi.fn>).mockResolvedValue({
      run_id: 'r1', agent: 'Ada', persona: 'A cautious ethicist', goals: [], memory_stream: [], beliefs: [],
      relationships: {}, tokens_in: 0, tokens_out: 0, cost_usd: 0, portrait_b64: null,
    })
    render(<Dossier agent={agent} feed={feed} runId="r1" onClose={() => {}} />)
    const heading = screen.getByText('Ada', { selector: '.cc-disp .cc-pname' })
    expect(within(heading).getByRole('img', { name: 'simulated persona' })).toBeInTheDocument()
    await waitFor(() => expect(api.getDossier).toHaveBeenCalled())
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
    // The messages, and the "why?" buttons this test says are absent, are on the Why? tab.
    openTab('Why?')
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
    openTab('Memory')

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
    openTab('Memory')

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
    openTab('Memory')

    await waitFor(() =>
      expect(screen.getByText('the group values consent')).toBeInTheDocument(),
    )
    expect(screen.getByText('consent is the crux')).toBeInTheDocument()
    openTab('Threads')
    expect(screen.getByText('trusted ally')).toBeInTheDocument()
    // The "why did it say that?" affordance is present for a captured run.
    openTab('Why?')
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
    openTab('Why?')
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
    // The HUD shows the firmness as well; this is the position's own tag, on the Convictions tab.
    expect(within(tabPanel()).getByText('firm')).toBeInTheDocument()
    expect(screen.getByText(/an embedded index that is a file/)).toBeInTheDocument()
    expect(screen.getByText(/The 2023 product that stalled/)).toBeInTheDocument()
    expect(screen.getByText(/time-to-first-run/)).toBeInTheDocument()
    expect(screen.getByText(/retrieval answer quality/)).toBeInTheDocument()
    expect(screen.getByText(/Extra services cost you users/)).toBeInTheDocument()
  })

  // A withheld run: hidden agendas, or any run from before 2026-10-02. `{}` is a backend that does not send
  // the flag at all, which ran every conversation withheld, so absent must read as withheld.
  it.each([
    ['says it withheld them', { withhold_concerns: true }],
    ['does not say (an older backend)', {}],
  ])('NEVER renders a withheld concern or a validity note, even if the API sends them, when the run %s', async (_, mode) => {
    // The backend strips both fields from a withheld run. This asserts the UI is a second line of
    // defence rather than trusting that: drawing the real concern out in conversation is the whole
    // exercise, and an operator who can read it off a panel has been handed the answer. `validity` is
    // the operator's private calibration note and must never be displayed in any mode.
    ;(api.getDossier as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...baseDossier,
      ...mode,
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
    expect(screen.queryByText('CONCERN')).not.toBeInTheDocument()
    // And it says the concern is hidden, rather than leaving a gap.
    expect(within(tabPanel()).getByText('Hidden')).toBeInTheDocument()
  })

  it('shows a plainly stated concern under its position, and never a validity note', async () => {
    // New runs since 2026-10-02 state concerns plainly, so the concern is part of the position.
    ;(api.getDossier as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...baseDossier,
      withhold_concerns: false,
      structured: {
        ...structuredPayload,
        viewpoints: [
          {
            ...structuredPayload.viewpoints[0],
            underlying_concern: 'I own the failure when a customer never gets a working run',
            validity: 'overgeneralised',
          },
        ],
      },
    })
    render(<Dossier agent={agent} feed={feed} runId="r1" onClose={() => {}} />)

    const concern = await screen.findByText('I own the failure when a customer never gets a working run')
    const flag = concern.closest('.cc-flag')!
    expect(within(flag as HTMLElement).getByText('CONCERN')).toBeInTheDocument()
    // Inside its position's panel, after the position.
    const position = screen.getByText('No feature may add a stateful external service')
    expect(position.compareDocumentPosition(concern) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.queryByText(/overgeneralised/i)).not.toBeInTheDocument()
    // Nothing is hidden, so nothing says it is.
    expect(within(tabPanel()).queryByText('Hidden')).not.toBeInTheDocument()
    expect(within(tabPanel()).queryByText(/withheld/i)).not.toBeInTheDocument()
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

    await waitFor(() => expect(within(tabPanel()).getByText('requires-escalation')).toBeInTheDocument())
    expect(screen.getByText(/no exit condition named/i)).toBeInTheDocument()
  })

  it('renders nothing for a run that used no structured personas', async () => {
    ;(api.getDossier as ReturnType<typeof vi.fn>).mockResolvedValue({ ...baseDossier })
    render(<Dossier agent={agent} feed={feed} runId="r1" onClose={() => {}} />)

    // Waits for the dossier itself: the persona prose is on the agent and renders before it arrives.
    expect(await screen.findByText('no structured persona')).toBeInTheDocument()
    expect(screen.getByText('A cautious ethicist')).toBeInTheDocument()
    // The tab is always called Convictions; nothing else may be, and nothing a structured block draws may
    // appear under it.
    expect(screen.queryByText(/Convictions/i, { ignore: '[role=tab]' })).not.toBeInTheDocument()
    expect(within(tabPanel()).queryByText(/position/i)).not.toBeInTheDocument()
    expect(within(tabPanel()).queryByText(/withheld/i)).not.toBeInTheDocument()
    expect(within(tabPanel()).queryByText('Hidden')).not.toBeInTheDocument()
  })
})
// PERSONA-RESEARCH.md §5.1. The floor reserves a slot per COLLECTION, not per kind of thing in
// one — so once research writes into a curated collection, the operator's own document competes
// with the searcher's finds. On run 602ddffe a persona's hand-picked source material lost all
// three slots to researched passages, and by the floor's accounting nothing went wrong.
//
// The decision was to accept that and make it VISIBLE rather than add a third floor, so these
// assert the visibility — including that a run WITHOUT research looks exactly as it did.
describe('Dossier — researched vs curated passages', () => {
  beforeEach(() => vi.clearAllMocks())

  function withRetrievals(retrievals: unknown[]) {
    ;(api.getDossier as ReturnType<typeof vi.fn>).mockResolvedValue({
      run_id: 'r1', agent: 'Ada', persona: 'A cautious ethicist', goals: ['Raise risks'],
      memory_stream: [], beliefs: [], relationships: {},
      tokens_in: 10, tokens_out: 5, cost_usd: 0.001, portrait_b64: null,
      document_retrievals: retrievals,
    })
    render(<Dossier agent={agent} runId="r1" feed={feed} onClose={vi.fn()} />)
    openTab('Memory')
  }

  const passage = (over: Record<string, unknown> = {}) => ({
    chunk_id: 1, document_id: 'd1', title: 'Iowa Admin Code', ordinal: 0,
    score: 0.42, chars: 100, ...over,
  })

  it('says how many passages research supplied when it supplied any', async () => {
    withRetrievals([{
      turn: 1, query: 'q', total_chars: 300, researched_passages: 3,
      passages: [
        passage({ chunk_id: 1, origin: 'researched', authority: 'commentary' }),
        passage({ chunk_id: 2, origin: 'researched', authority: 'persuasive' }),
        passage({ chunk_id: 3, origin: 'researched', authority: 'controlling' }),
      ],
    }])
    // "3 of 3" is the whole finding: the persona's own material reached this prompt not at all.
    expect(await screen.findByText(/3 of 3 researched/)).toBeInTheDocument()
  })

  it('marks controlling authority, because that is the tier the floor reserves for', async () => {
    withRetrievals([{
      turn: 1, query: 'q', total_chars: 100, researched_passages: 1,
      passages: [passage({ origin: 'researched', authority: 'controlling' })],
    }])
    expect(await screen.findByText(/\[controlling\]/)).toBeInTheDocument()
  })

  it('distinguishes a found passage from one the operator provided', async () => {
    withRetrievals([{
      turn: 1, query: 'q', total_chars: 200, researched_passages: 1,
      passages: [
        passage({ chunk_id: 1, origin: 'researched' }),
        passage({ chunk_id: 2, title: 'Source material — Ada' }),
      ],
    }])
    expect(await screen.findByText(/\[found\]/)).toBeInTheDocument()
    expect(screen.getByText(/\[yours\]/)).toBeInTheDocument()
  })

  it('adds NO badges at all to a run without research', async () => {
    // Otherwise every conversation in the system gains a `[yours]` tag on every passage to
    // say nothing. The point is to make a DIFFERENCE visible, and here there is none.
    withRetrievals([{
      turn: 1, query: 'q', total_chars: 100,
      passages: [passage({ title: 'spec.pdf' })],
    }])
    expect(await screen.findByText(/spec.pdf/)).toBeInTheDocument()
    expect(screen.queryByText(/\[yours\]/)).not.toBeInTheDocument()
    expect(screen.queryByText(/\[found\]/)).not.toBeInTheDocument()
    expect(screen.queryByText(/researched/)).not.toBeInTheDocument()
  })
})

describe('Dossier knowledge bases', () => {
  it('names the collections a persona searches, and marks one it can no longer read', async () => {
    ;(api.getDossier as ReturnType<typeof vi.fn>).mockResolvedValue({
      run_id: 'r1', agent: 'Ada', persona: 'p', goals: [], memory_stream: [], beliefs: [], relationships: {},
      tokens_in: 0, tokens_out: 0, cost_usd: 0, portrait_b64: null,
      knowledge_bases: [
        { id: 'k1', name: 'statutes', scope: 'run', readable: true },
        { id: 'k2', name: null, scope: 'persona', readable: false },
      ],
    })
    render(<Dossier agent={agent} feed={feed} runId="r1" onClose={() => {}} />)
    openTab('Memory')
    expect(await screen.findByText('Knowledge bases searched (2)')).toBeInTheDocument()
    expect(screen.getByText('statutes')).toBeInTheDocument()
    expect(screen.getByText('A collection you can no longer read')).toBeInTheDocument()
    expect(screen.getByText('whole cast')).toBeInTheDocument()
  })
})

// docs/MOBILE-UI.md §4.4: a HUD of turns, stance and firmness over four tabs. Everything the dossier showed as
// one column has to still be reachable on some tab, and the withheld concern on none of them.
describe('Dossier — HUD and tabs', () => {
  beforeEach(() => vi.clearAllMocks())

  const shift = {
    sentences: ['I have changed my mind'],
    credits: [{ kind: 'persona', name: 'Bo' }],
    conditions: [],
    matched_conditions: [],
    no_listed_condition: false,
  }
  const longFeed = [
    { turn: 1, seq: 2, speaker: 'Ada', content: 'Hello there' },
    { turn: 2, seq: 3, speaker: 'Bo', content: 'Not one of hers' },
    { turn: 3, seq: 4, speaker: 'Ada', content: 'I have changed my mind on the pilot', shift },
    { turn: 4, seq: 5, speaker: 'Ada', content: 'Words the operator wrote', injected: true },
  ]

  const full = {
    run_id: 'r1', agent: 'Ada', persona: 'A cautious ethicist', goals: ['Raise risks'],
    memory_stream: [
      { id: 'm1', content: 'Bo wants a pilot first', importance: 0.5, tags: ['fact'], timestamp: 1 },
    ],
    beliefs: [{ id: 'b1', content: 'a pilot is only a delay', importance: 0.9, tags: ['reflection'], timestamp: 2 }],
    relationships: { Bo: 'wary but listening' },
    pending_threads: [
      { id: 't1', description: 'Promised a cost table', thread_type: 'promise', origin_turn: 1,
        status: 'open', resolved_turn: null, stale: true },
      { id: 't2', description: 'Asked who signs off', thread_type: 'deferred-consequence', origin_turn: 3,
        status: 'resolved', resolved_turn: 4, stale: false },
    ],
    documents: [
      { document_id: 'd1', title: 'handbook.md', media_type: 'text/markdown', char_count: 1200,
        chunk_count: 3, cast_wide: false },
    ],
    knowledge_bases: [{ id: 'k1', name: 'policies', scope: 'persona', readable: true }],
    document_retrievals: [
      { turn: 3, query: 'q', total_chars: 100,
        passages: [{ chunk_id: 1, document_id: 'd1', title: 'handbook.md', ordinal: 2, score: 0.73, chars: 100 }] },
    ],
    structured: {
      role: 'Operations lead',
      background: { formative_events: [{ year: 2021, event: 'A winter short of staff', lesson: 'Rotas break first' }] },
      preferences: { dismisses: ['morale surveys'], optimises_for: ['cover on every shift'], persuaded_by: ['a pilot'] },
      viewpoints: [
        { position: 'No change without a rota', formed_by: 'Ran the desk alone for a month',
          firmness: 'firm' as const, evidence_that_shifts: ['a tested rota'] },
        { position: 'Legal must sign first', firmness: 'requires-escalation' as const,
          evidence_that_shifts: ['a signed waiver'] },
      ],
    },
    cognition_enabled: true, cognition_lost_turns: 0,
    tokens_in: 10, tokens_out: 5, cost_usd: 0.001, portrait_b64: null,
  }

  function renderFull(stance?: StanceState, dossier: Record<string, unknown> = full, basis?: StanceBasisEntry) {
    ;(api.getDossier as ReturnType<typeof vi.fn>).mockResolvedValue(dossier)
    render(<Dossier agent={agent} feed={longFeed} runId="r1" stance={stance} basis={basis} onClose={() => {}} />)
  }
  const hud = () => within(screen.getByRole('dialog').querySelector('.cc-hudstrip') as HTMLElement)

  it('has four tabs, opens on Convictions, and switches on a tap and on the arrow keys', async () => {
    renderFull()
    const tabs = screen.getAllByRole('tab')
    expect(tabs.map((t) => t.textContent)).toEqual(['Convictions', 'Memory', 'Threads', 'Why?'])
    expect(screen.getByRole('tab', { name: 'Convictions' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tabpanel', { name: 'Convictions' })).toBeInTheDocument()
    expect(await screen.findByText('No change without a rota')).toBeInTheDocument()

    openTab('Memory')
    expect(screen.getByRole('tab', { name: 'Memory' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tab', { name: 'Convictions' })).toHaveAttribute('aria-selected', 'false')
    expect(screen.getByRole('tabpanel', { name: 'Memory' })).toBeInTheDocument()
    // Only the open tab renders.
    expect(screen.queryByText('No change without a rota')).not.toBeInTheDocument()

    // Roving focus, as a tablist is expected to behave: arrows move along it and wrap at the ends.
    fireEvent.keyDown(screen.getByRole('tab', { name: 'Memory' }), { key: 'ArrowRight' })
    expect(screen.getByRole('tab', { name: 'Threads' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tab', { name: 'Threads' })).toHaveFocus()
    fireEvent.keyDown(screen.getByRole('tab', { name: 'Threads' }), { key: 'End' })
    expect(screen.getByRole('tab', { name: 'Why?' })).toHaveAttribute('aria-selected', 'true')
    fireEvent.keyDown(screen.getByRole('tab', { name: 'Why?' }), { key: 'ArrowRight' })
    expect(screen.getByRole('tab', { name: 'Convictions' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tab', { name: 'Memory' })).toHaveAttribute('tabindex', '-1')
  })

  it('HUD: counts turns taken, not words put in their mouth, and shows the firmest position', async () => {
    renderFull('holding')
    // Three messages carry Ada's name; the operator wrote one of them.
    expect(hud().getByText('02')).toBeInTheDocument()
    // Stance in a glyph and a word, never colour alone.
    expect(hud().getByText('▼ holding out')).toBeInTheDocument()
    // `requires-escalation` outranks `firm` (matrix_studio/personas.py orders them), and is shown as itself.
    expect(await hud().findByText('requires-escalation')).toBeInTheDocument()
    expect(hud().getByText('firmest of 2')).toBeInTheDocument()
  })

  it('HUD says "not yet" for stance when the run has none, and a dash when there is no structured persona', async () => {
    renderFull(undefined, { ...full, structured: null })
    expect(hud().getByText('not yet')).toBeInTheDocument()
    expect(hud().queryByText(/support|holding|not stated/)).not.toBeInTheDocument()
    expect(await hud().findByText('no structured persona')).toBeInTheDocument()
    expect(hud().getByText('—')).toBeInTheDocument()
  })

  it.each([
    ['support' as const, '▲ support'],
    ['conditional' as const, '◐ with conditions'],
    ['unstated' as const, '◆ not stated'],
  ])('HUD shows the %s stance by glyph and word', (stance, label) => {
    renderFull(stance)
    expect(hud().getByText(label)).toBeInTheDocument()
    expect(hud().queryByText('not yet')).not.toBeInTheDocument()
  })

  it('says where the stance came from: the closing statement, quoted', async () => {
    renderFull('conditional', full, {
      stance: 'conditional', source: 'closing', class: 'accepts_with_conditions',
      quote: 'I sign, provided the rota is tested first',
    })
    expect(hud().getByText('◐ with conditions')).toBeInTheDocument()
    expect(hud().getByText('from closing statement')).toBeInTheDocument()
    const c = within(tabPanel())
    expect(c.getByText('Where they ended')).toBeInTheDocument()
    expect(c.getByText(/From their closing statement/)).toBeInTheDocument()
    expect(c.getByText('“I sign, provided the rota is tested first”')).toBeInTheDocument()
    expect(await screen.findByText('No change without a rota')).toBeInTheDocument()
  })

  it('says when the summary decided instead, in whose words, and why the closing statement did not', () => {
    renderFull('holding', full, {
      stance: 'holding', source: 'summary', class: 'unclear', quote: 'Objects to the start date',
      fallback: 'unverified_quote', claimed: 'accepts',
    })
    expect(hud().getByText('from summary')).toBeInTheDocument()
    const c = within(tabPanel())
    expect(c.getByText(/In the summary's words/)).toBeInTheDocument()
    expect(c.getByText('“Objects to the start date”')).toBeInTheDocument()
    expect(c.getByText(/the sentence the classifier quoted is not in their statement/)).toBeInTheDocument()
  })

  it('shows no basis panel for a run summarised before stances carried one', () => {
    renderFull('support')
    expect(screen.queryByText('Where they ended')).not.toBeInTheDocument()
    expect(hud().queryByText(/from (closing statement|summary)/)).not.toBeInTheDocument()
  })

  it('renders every tab’s content', async () => {
    renderFull()

    // Convictions: position, firmness, FORMED BY, WOULD MOVE, will not weigh, the hidden note, the move.
    expect(await screen.findByText('No change without a rota')).toBeInTheDocument()
    const c = within(tabPanel())
    expect(screen.getByText('Operations lead')).toBeInTheDocument()
    expect(c.getByText('Position 1')).toBeInTheDocument()
    expect(c.getByText('firm')).toBeInTheDocument()
    expect(c.getByText('FORMED BY')).toBeInTheDocument()
    expect(c.getByText('Ran the desk alone for a month')).toBeInTheDocument()
    expect(c.getAllByText('WOULD MOVE')).toHaveLength(2)
    expect(c.getByText('a tested rota')).toBeInTheDocument()
    expect(c.getByText('morale surveys')).toBeInTheDocument()
    expect(c.getByText('cover on every shift')).toBeInTheDocument()
    expect(c.getByText('a pilot')).toBeInTheDocument()
    expect(c.getByText(/Rotas break first/)).toBeInTheDocument()
    expect(c.getByText('Withheld concern')).toBeInTheDocument()
    expect(c.getByText('Hidden')).toBeInTheDocument()
    expect(c.getByText('MOVED @03')).toBeInTheDocument()
    expect(c.getByText('A cautious ethicist')).toBeInTheDocument()
    expect(c.getByText('Raise risks')).toBeInTheDocument()

    // Memory: the last three things said (latest first), memory, reflections, passages, documents, usage.
    openTab('Memory')
    const m = within(tabPanel())
    const said = m.getByText('Last said').parentElement!.querySelectorAll('li')
    expect([...said].map((li) => li.textContent)).toEqual([
      '#04 · injected “Words the operator wrote”',
      '#03 “I have changed my mind on the pilot”',
      '#01 “Hello there”',
    ])
    expect(m.getByText('Bo wants a pilot first')).toBeInTheDocument()
    expect(m.getByText('a pilot is only a delay')).toBeInTheDocument()
    expect(m.getByText('handbook.md #2')).toBeInTheDocument()
    expect(m.getByText('score 0.73')).toBeInTheDocument()
    expect(m.getByText('handbook.md')).toBeInTheDocument()
    expect(m.getByText('Knowledge bases searched (1)')).toBeInTheDocument()
    expect(m.getByText('policies')).toBeInTheDocument()
    expect(m.getByText('Messages')).toBeInTheDocument()
    expect(m.getByText('$0.0010')).toBeInTheDocument()
    expect(m.getByText('10 in · 5 out')).toBeInTheDocument()

    // Threads: what they opened, its state in words, and the relationships.
    openTab('Threads')
    const t = within(tabPanel())
    expect(t.getByText('Pending threads (1 open)')).toBeInTheDocument()
    expect(t.getByText('Promised a cost table')).toBeInTheDocument()
    expect(t.getByText('#01 · promise · open, dangling')).toBeInTheDocument()
    expect(t.getByText('#03 · deferred consequence · resolved @04')).toBeInTheDocument()
    expect(t.getByText('wary but listening')).toBeInTheDocument()

    // Why?: every message, latest first, each with its trace button.
    openTab('Why?')
    const w = within(tabPanel())
    expect(w.getByText('Messages (3)')).toBeInTheDocument()
    expect(w.getAllByText('why?')).toHaveLength(3)
    const order = ['Words the operator wrote', 'I have changed my mind on the pilot', 'Hello there'].map((text) =>
      w.getByText(text),
    )
    expect(order[0].compareDocumentPosition(order[1]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(order[1].compareDocumentPosition(order[2]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('says threads need cognition when the run had it off, rather than that none were opened', async () => {
    renderFull(undefined, {
      ...full, pending_threads: [], memory_stream: [], beliefs: [], relationships: {}, cognition_enabled: false,
    })
    openTab('Threads')
    expect(await screen.findByText(/tracked only when a run has cognition on/)).toBeInTheDocument()
    expect(screen.queryByText(/No threads opened/)).not.toBeInTheDocument()
  })

  it.each([
    ['says it withheld them', { withhold_concerns: true }],
    ['does not say (an older backend)', {}],
  ])('NEVER renders a withheld concern or a validity note on any tab, when the run %s', async (_, mode) => {
    // The same guard as above, across the whole dossier rather than only the tab it opens on.
    renderFull(undefined, {
      ...full,
      ...mode,
      structured: {
        ...full.structured,
        viewpoints: full.structured.viewpoints.map((vp) => ({
          ...vp, underlying_concern: 'THE REAL WORRY IS A BUDGET CUT', validity: 'overgeneralised',
        })),
      },
    })
    expect(await screen.findByText('No change without a rota')).toBeInTheDocument()
    for (const name of ['Convictions', 'Memory', 'Threads', 'Why?'] as const) {
      openTab(name)
      expect(screen.queryByText(/THE REAL WORRY/i)).not.toBeInTheDocument()
      expect(screen.queryByText(/overgeneralised/i)).not.toBeInTheDocument()
    }
  })

  it('renders a plainly stated concern on Convictions, and a validity note on no tab', async () => {
    // The mirror of the guard above: a run that stated its concerns does show them, each under its own
    // position, and only there.
    renderFull(undefined, {
      ...full,
      withhold_concerns: false,
      structured: {
        ...full.structured,
        viewpoints: [
          { ...full.structured.viewpoints[0], underlying_concern: 'I cover every gap myself', validity: 'overgeneralised' },
          { ...full.structured.viewpoints[1], validity: 'sound' },
        ],
      },
    })
    expect(await screen.findByText('I cover every gap myself')).toBeInTheDocument()
    const c = within(tabPanel())
    // One concern authored, so one flag: the second position has none and shows none.
    expect(c.getAllByText('CONCERN')).toHaveLength(1)
    expect(c.queryByText('Withheld concern')).not.toBeInTheDocument()
    for (const name of ['Convictions', 'Memory', 'Threads', 'Why?'] as const) {
      openTab(name)
      expect(screen.queryByText(/overgeneralised|\bsound\b/i)).not.toBeInTheDocument()
      if (name !== 'Convictions') expect(screen.queryByText('I cover every gap myself')).not.toBeInTheDocument()
    }
  })
})
