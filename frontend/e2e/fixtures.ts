// SPDX-License-Identifier: Apache-2.0
// A run, served to the page by intercepting its API calls. No backend, no AWS, and the transcript
// is fixed, which is what makes the pixel assertions in these tests meaningful.

import { test as base, type Page } from '@playwright/test'
import type { SimEvent } from '../src/types'

export const CAST = [
  'Ana Silva', 'Ben Carter', 'Chloe Wu', 'Dev Patel',
  'Eve Morgan', 'Farah Haddad', 'Gus Lindqvist', 'Hana Sato',
] as const

export const EXPERT = 'Employment lawyer'
export const OUTSIDER = 'Customer email'

let seq = 0
const ev = (turn: number, type: SimEvent['event_type'], payload: Record<string, unknown>, agent: string | null = null): SimEvent =>
  ({ run_id: 'demo', turn, seq: seq++, event_type: type, agent_name: agent, payload })

/**
 * The transcript these tests are written against. Lines are numbered from 1 in the UI:
 *
 *   1  Eve speaks            5  the customer email arrives (a messenger)
 *   2  Ana speaks            6  the consultant answers Farah
 *   3  Chloe speaks          7  Gus speaks, having read a source
 *   4  Ben speaks            8  Chloe speaks, and her position shifts
 */
export function runEvents({ research = false }: { research?: boolean } = {}): SimEvent[] {
  seq = 0
  const out: SimEvent[] = [ev(0, 'sim.started', {})]
  out.push(ev(0, 'assumption.made', { id: 'a1', statement: 'Headcount stays flat' }))
  const say = (turn: number, who: string, message: string) => {
    out.push(ev(turn, 'speaker.selected', { speaker: who }, who))
    out.push(ev(turn, 'agent.response', { speaker: who, message }, who))
  }
  say(1, 'Eve Morgan', 'Let us frame it: how would we know a four-day week worked?')
  say(2, 'Ana Silva', 'Two of my teams could pilot it with rotating Fridays.')
  say(3, 'Chloe Wu', 'Our customers have response-time commitments on every business day.')
  say(4, 'Ben Carter', 'I am neutral as long as headcount stays flat.')
  out.push(ev(5, 'agent.response', { speaker: OUTSIDER, message: 'Our renewal is next month and we will be reviewing response times.', injected: true }))
  out.push(ev(6, 'expert.answered', {
    expert: EXPERT, asked_by: 'Farah Haddad', question: 'Can we change hours without consultation?',
    answer: 'Not in most jurisdictions you operate in. A pilot is still a change.',
  }))
  out.push(ev(7, 'document.retrieved', { speaker: 'Gus Lindqvist', passages: [{ chunk_id: 3, document_id: 'd1', title: 'Pilot playbook', ordinal: 2 }] }, 'Gus Lindqvist'))
  say(7, 'Gus Lindqvist', 'Then measure it: resolution time, satisfaction and attrition.')
  say(8, 'Chloe Wu', 'Show me those holding and I will move.')
  out.push(ev(8, 'position.shift', { speaker: 'Chloe Wu', sentences: ['I will move.'] }, 'Chloe Wu'))
  out.push(ev(8, 'sim.completed', {}))
  return out.map((e) => (research ? e : e))
}

export function runDetail({ research = false, branchTurn = null as number | null } = {}) {
  return {
    run_id: 'demo', name: 'trusted-robot', description: null, slug: null,
    topic: 'Should the support org pilot a four-day week?', status: 'complete',
    cast: CAST.map((name) => ({ name, persona: 'Participant', goals: [] })),
    config: {}, result: null,
    ...(research
      ? { research: { status: 'researched', scopes: [{ scope: 'shared', documents: 9, controlling: 3 }] } }
      : {}),
    ...(branchTurn == null
      ? {}
      : { lineage: { parent: { run_id: 'p1', name: 'quiet-harbor', branch_turn: branchTurn }, branches: [] } }),
  }
}

/** Answer every call the page makes, so nothing reaches a real backend. */
export async function stubApi(page: Page, opts: { research?: boolean; branchTurn?: number | null } = {}) {
  const json = (body: unknown) => ({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  await page.route('**/config.json', (r) => r.fulfill(json({ authRequired: false })))
  // One handler for every API path, not a route each: Playwright gives the most recently
  // registered route priority, so a catch-all added last swallowed the events call and the page
  // loaded with an undefined transcript.
  await page.route('**/api/**', (r) => {
    const path = new URL(r.request().url()).pathname
    if (/\/api\/runs\/[^/]+\/events$/.test(path)) return r.fulfill(json({ run_id: 'demo', events: runEvents() }))
    if (/\/api\/runs\/[^/]+$/.test(path)) return r.fulfill(json(runDetail(opts)))
    return r.fulfill(json({}))
  })
}

export const test = base.extend<{ theatre: Page }>({
  /** The theatre, loaded on its title card, with playback paused so pixels are stable. */
  theatre: async ({ page }, use) => {
    await stubApi(page)
    await page.goto('/theatre.html?run=demo')
    await use(page)
  },
})

export { expect } from '@playwright/test'
