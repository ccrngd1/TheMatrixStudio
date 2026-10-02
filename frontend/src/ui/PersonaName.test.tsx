// SPDX-License-Identifier: Apache-2.0
// The simulated-persona marker (`PersonaName.tsx`): one component, on every surface a persona's or a consultant's
// name is shown, accessible, and never on an operator's injected message. Surfaces that need the API mocked are
// tested beside their own component (asides, dossier, library, ensemble view, theatre, the new-run form).
import { describe, expect, it } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { BOT_PREFIX, BotMark, PersonaName, SIMULATED_LABEL, botLabel } from './PersonaName'
import { ConversationFeed } from '../components/ConversationFeed'
import { CastCard } from '../components/CastCard'
import { FaceStrip } from '../components/run/RunChrome'
import { MessageContext } from '../components/run/MessageContext'
import { ParticipationPanel } from '../components/ParticipationPanel'
import { ResearchPanel } from '../components/ResearchPanel'
import { SummaryPanel } from '../components/SummaryPanel'
import { RoomMap, StanceBasisList } from '../components/run/Stance'
import type { AgentView, FeedMessage, StoredSummary } from '../types'

const marks = (el: HTMLElement = document.body) => within(el).queryAllByRole('img', { name: SIMULATED_LABEL })

const agent = (name: string): AgentView => ({
  name, persona: `${name}'s persona`, goals: [], portrait: null, portraitKey: null, portraitUrl: null,
  avatarResolved: true, messageCount: 1, tokensIn: 1, tokensOut: 1, costUsd: 0,
})

describe('PersonaName', () => {
  it('draws the robot glyph before the name, labelled for a screen reader, with the name its own text', () => {
    render(<PersonaName name="Ruth" />)
    const glyph = screen.getByRole('img', { name: 'simulated persona' })
    expect(glyph.tagName.toLowerCase()).toBe('svg')
    // Not an emoji: nothing but the name is text, so a machine without an emoji font shows no empty box.
    expect(screen.getByText('Ruth').textContent).toBe('Ruth')
    expect(glyph.parentElement).toBe(screen.getByText('Ruth'))
  })

  it('has a plain-text form for an option or a canvas, and leaves the name itself untouched', () => {
    expect(botLabel('Ruth')).toBe('(bot) Ruth')
    expect(BOT_PREFIX).toBe('(bot) ')
  })

  it('offers the glyph alone, for beside a field the name is typed into', () => {
    render(<BotMark />)
    expect(screen.getByRole('img', { name: 'simulated persona' })).toBeInTheDocument()
  })
})

describe('the marker on each surface', () => {
  it('feed: a persona, a consultant and the persona who asked are marked; an injected message is not', () => {
    const feed: FeedMessage[] = [
      { turn: 1, seq: 1, speaker: 'Ruth', content: 'Opening.' },
      { turn: 1, seq: 2, speaker: 'Kim (consultant)', content: 'It is 7%.',
        consultant: { expert: 'Kim', askedBy: 'Ruth', question: 'What is churn?' } },
      { turn: 2, seq: 3, speaker: 'Residents survey', content: 'Most want it.', injected: true },
      { turn: 3, seq: 4, speaker: 'Ruth', content: 'Noted.' },
    ]
    const { container } = render(
      <ConversationFeed feed={feed} agents={{ Ruth: agent('Ruth') }} activeSpeaker="Ruth" thinking />,
    )
    const msgs = container.querySelectorAll('.cc-msg')
    expect(marks(msgs[0] as HTMLElement)).toHaveLength(1)
    expect(marks(msgs[1] as HTMLElement)).toHaveLength(2) // the consultant, and Ruth who asked
    expect(marks(container.querySelector('.cc-signal') as HTMLElement)).toHaveLength(0)
    // The operator's message still names its sender, unmarked.
    expect(within(container.querySelector('.cc-signal') as HTMLElement).getByText('Residents survey')).toBeInTheDocument()
    expect(marks(screen.getByRole('status'))).toHaveLength(1) // "Ruth composing"
  })

  it('cast card: the name carries the marker, and the row’s accessible name says simulated', () => {
    render(<CastCard agent={agent('Ruth')} active={false} thinking={false} onClick={() => {}} />)
    const row = screen.getByRole('button', { name: 'Ruth, simulated persona: open dossier' })
    expect(marks(row)).toHaveLength(1)
  })

  it('face strip: the tokens show initials, and each one’s accessible name says simulated', () => {
    const { container } = render(<FaceStrip order={['Ruth', 'Sam Okafor']} next={null} onOpen={() => {}} />)
    expect(screen.getByRole('button', { name: 'Sam Okafor, simulated persona: open dossier' })).toHaveTextContent('SO')
    // One decorative glyph for the strip, hidden: the tokens' names already say it.
    expect(container.querySelectorAll('svg[aria-hidden="true"]').length).toBeGreaterThan(0)
    expect(marks(container)).toHaveLength(0)
  })

  it('room map: each node is named as a simulated persona, its label drawn with the glyph', () => {
    const feed: FeedMessage[] = [{ turn: 1, seq: 1, speaker: 'Ruth', content: 'x' }]
    const { container } = render(<RoomMap order={['Ruth', 'Sam']} feed={feed} stance={null} next={null} onOpen={() => {}} />)
    expect(screen.getByRole('button', { name: /^Ruth, simulated persona: 1 turns/ })).toBeInTheDocument()
    expect(container.querySelectorAll('.cc-node svg')).toHaveLength(2)
  })

  it('message context: the speaker, the neighbours and who asked a consultant', () => {
    const feed: FeedMessage[] = [
      { turn: 1, seq: 1, speaker: 'Ruth', content: 'One.' },
      { turn: 2, seq: 2, speaker: 'Sam', content: 'Two.' },
    ]
    render(
      <MessageContext message={feed[1]} feed={feed} events={[]} assumptions={[]} order={['Ruth', 'Sam']} runId="r1"
        onOpenDossier={() => {}} onJump={() => {}} />,
    )
    expect(within(screen.getByText('Sam', { selector: '.cc-pname' })).getByRole('img', { name: SIMULATED_LABEL }))
      .toBeInTheDocument()
    expect(marks(screen.getByRole('button', { name: /Came after/ }))).toHaveLength(1)
  })

  it('participation: every row', () => {
    const feed: FeedMessage[] = [{ turn: 1, seq: 1, speaker: 'Ruth', content: 'x' }]
    render(<ParticipationPanel feed={feed} order={['Ruth', 'Sam']} onJump={() => {}} />)
    expect(marks()).toHaveLength(2)
  })

  it('where the room ended: each persona with their stance', () => {
    render(<StanceBasisList order={['Ruth']} basis={{ Ruth: { stance: 'support', source: 'closing', class: 'accepts', quote: 'I sign' } }} />)
    expect(marks(screen.getByRole('listitem'))).toHaveLength(1)
  })

  it('research: a corpus searched for a persona is named as one; the shared corpus is not', () => {
    render(<ResearchPanel research={{
      status: 'researched', provider: 'tavily', cost_usd: 0.1, batch: 'b',
      scopes: [
        { scope: 'shared', queries: 1, documents: 1, controlling: 0, unreadable: 0, negative: false, written: 1, embedded: 1, kb_id: 'k1' },
        { scope: 'Ruth', queries: 1, documents: 1, controlling: 0, unreadable: 0, negative: false, written: 1, embedded: 1, kb_id: 'k2' },
      ],
    }} />)
    expect(marks()).toHaveLength(1)
  })

  it('summary: a dissenter, which the analyst names, is rendered as a name; the prose is not touched', () => {
    const generated: StoredSummary = {
      id: 1, run_id: 'r1', kind: 'generated', tokens_in: 0, tokens_out: 0, cost_usd: 0, created_at: 1, parsed: true,
      payload: { overview: 'Ruth objected throughout.', dissenters: [{ speaker: 'Ruth', position: 'Too soon' }] },
    }
    render(<SummaryPanel runId="r1" generated={generated} imported={null} defaultInstructions="" canGenerate onUpdated={() => {}} />)
    expect(marks()).toHaveLength(1)
    expect(screen.getByText('Ruth objected throughout.')).toBeInTheDocument()
  })
})
