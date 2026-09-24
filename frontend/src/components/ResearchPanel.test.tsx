// SPDX-License-Identifier: Apache-2.0
//
// `docs/PERSONA-RESEARCH.md` §5.3. Nobody watches a research pass, so this panel is the whole
// of an operator's visibility into it — and the thing it must not do is merge outcomes that
// imply different actions.
//
// §5.2 names three that all look like "no results" if you render them carelessly:
//
//   found-nothing  the search ran and the subject has no authority on the open web. HONEST.
//   unavailable    no search key is configured. Fix the deployment; retrying changes nothing.
//   failed         the pass broke. Retrying might work.
//
// Most of the assertions below are that those stay distinguishable. The other load-bearing one
// is `embed_error`: documents stored and not embedded means the corpus EXISTS and no turn can
// read it, which is the failure this project keeps rediscovering — and every other symptom of
// it looks like a researcher that found nothing useful.
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ResearchPanel } from './ResearchPanel'
import type { ResearchRecord } from '../types'

const RESEARCHED: ResearchRecord = {
  status: 'researched',
  provider: 'tavily',
  cost_usd: 0.2581,
  batch: '5dcb80bc0034',
  scopes: [
    {
      scope: 'shared',
      queries: 6,
      documents: 20,
      controlling: 4,
      unreadable: 10,
      negative: false,
      written: 20,
      embedded: 710,
      kb_id: 'ee36f0cf6798',
    },
    {
      scope: 'Quinn',
      queries: 2,
      documents: 9,
      controlling: 0,
      unreadable: 0,
      negative: true,
      written: 10,
      embedded: 216,
      kb_id: '08d2553096d6',
    },
  ],
}

describe('ResearchPanel', () => {
  it('renders nothing for a run that did not research', () => {
    // Every run created before this feature existed, and every run that declined it. An empty
    // panel would add a section to every conversation in the system to say nothing happened.
    const { container } = render(<ResearchPanel research={null} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('shows the per-scope counts and the collection each went to', () => {
    render(<ResearchPanel research={RESEARCHED} />)

    expect(screen.getByText('Researched')).toBeInTheDocument()
    expect(screen.getByText(/tavily/)).toBeInTheDocument()
    expect(screen.getByText(/\$0\.2581/)).toBeInTheDocument()
    expect(screen.getByText('Quinn')).toBeInTheDocument()
    expect(screen.getByText('ee36f0cf6798')).toBeInTheDocument()
    // Totals across scopes, so a reader does not have to add up seven rows.
    expect(screen.getByText('29')).toBeInTheDocument() // sources
    expect(screen.getByText('926')).toBeInTheDocument() // embedded
  })

  it('breaks out the controlling count, because that is the tier the floor reserves for', () => {
    // A pass that found ninety sources and no controlling authority did not answer the
    // question the personas were asking, and that has to be legible at a glance rather than
    // buried in a per-row column.
    render(<ResearchPanel research={RESEARCHED} />)
    expect(screen.getByText(/controlling/)).toBeInTheDocument()
  })

  it('marks a scope whose absence of authority was recorded as a finding', () => {
    render(<ResearchPanel research={RESEARCHED} />)
    // §4: "nobody looked" and "we looked and there is nothing" are different facts, and only
    // the second is reusable. The marker is what says which one this was.
    expect(screen.getByText(/documented negative/)).toBeInTheDocument()
  })

  it('distinguishes found-nothing from failed', () => {
    const { unmount } = render(
      <ResearchPanel
        research={{ status: 'found-nothing', scopes: [{ scope: 'shared', documents: 0 }] }}
      />,
    )
    expect(screen.getByText(/nothing found/)).toBeInTheDocument()
    // The wording has to say this is an answer rather than a breakage.
    expect(screen.getByText(/real answer/)).toBeInTheDocument()
    unmount()

    render(<ResearchPanel research={{ status: 'failed', error: 'provider timed out' }} />)
    expect(screen.getByText('Failed')).toBeInTheDocument()
    expect(screen.getByText(/provider timed out/)).toBeInTheDocument()
    // And that the conversation survived it, which is §5.2's whole point.
    expect(screen.getByText(/never fails a run/)).toBeInTheDocument()
  })

  it('says a missing search key is configuration, not something to retry', () => {
    render(
      <ResearchPanel
        research={{ status: 'unavailable', error: 'No search provider configured. Set one of: …' }}
      />,
    )
    expect(screen.getByText('Unavailable')).toBeInTheDocument()
    expect(screen.getByText(/fixed by configuration, not by retrying/)).toBeInTheDocument()
  })

  it('shows the reason for a skip, because skips mean different things', () => {
    // "An ensemble researches once" and "research is not enabled" are both skips and imply
    // entirely different things to whoever is reading.
    render(
      <ResearchPanel
        research={{
          status: 'skipped',
          error: 'an ensemble researches once, before its members, so replicates stay replicates',
        }}
      />,
    )
    expect(screen.getByText(/an ensemble researches once/)).toBeInTheDocument()
  })

  it('says NOT RETRIEVABLE when documents were stored and could not be embedded', () => {
    // The corpus exists and no turn can see it. Reporting the embedded count as 0 would read
    // as "found nothing", which is a judgement about the researcher rather than a broken step.
    render(
      <ResearchPanel
        research={{
          status: 'researched',
          scopes: [
            {
              scope: 'shared',
              documents: 12,
              written: 12,
              embedded: 0,
              embed_error: 'index built with another model',
              kb_id: 'kb1',
            },
          ],
        }}
      />,
    )
    expect(screen.getByText('not retrievable')).toBeInTheDocument()
  })

  it('says REFUSED when a scope could not be stored', () => {
    render(
      <ResearchPanel
        research={{
          status: 'researched',
          scopes: [
            {
              scope: 'Casey',
              documents: 5,
              refused: 'kb-x is not owned by this run’s owner',
            },
          ],
        }}
      />,
    )
    expect(screen.getByText('refused')).toBeInTheDocument()
  })

  it('survives a record with no scopes at all', () => {
    // Every pre-search failure produces exactly this: a status, maybe an error, nothing else.
    render(<ResearchPanel research={{ status: 'unavailable' }} />)
    expect(screen.getByText('Unavailable')).toBeInTheDocument()
  })
})
