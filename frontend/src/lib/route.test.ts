// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from 'vitest'
import { hrefOf, parseHash, type Route } from './route'

describe('hash routes', () => {
  const cases: [string, Route][] = [
    ['#/runs', { name: 'runs' }],
    ['#/ensembles', { name: 'ensembles' }],
    ['#/knowledge', { name: 'knowledge' }],
    ['#/knowledge/kb%201', { name: 'knowledge', kbId: 'kb 1' }],
    ['#/library', { name: 'library' }],
    ['#/new', { name: 'new' }],
    ['#/new/3?from=r1', { name: 'new', step: 3, fromRunId: 'r1' }],
    ['#/new/2?cast=Board%20review', { name: 'new', step: 2, castTemplate: 'Board review' }],
    ['#/new/2?pack=finance-lead', { name: 'new', step: 2, packId: 'finance-lead' }],
    ['#/new?cast=c1&pack=p1', { name: 'new', castTemplate: 'c1', packId: 'p1' }],
    ['#/run/abc', { name: 'run', runId: 'abc', tab: 'conversation' }],
    ['#/run/abc/cast', { name: 'run', runId: 'abc', tab: 'cast' }],
    ['#/run/abc/analysis', { name: 'run', runId: 'abc', tab: 'analysis' }],
    ['#/run/abc/scrub', { name: 'scrub', runId: 'abc' }],
    ['#/ensemble/e1', { name: 'ensemble', ensembleId: 'e1' }],
  ]
  it.each(cases)('%s parses and round-trips', (hash, route) => {
    expect(parseHash(hash)).toEqual(route)
    expect(parseHash(hrefOf(route))).toEqual(route)
  })

  it('round-trips a saved-cast name whatever characters it has', () => {
    // Template names are free text; a `&`, `?`, `+` or `#` in one must not split it or leak into another param.
    for (const castTemplate of ['Q3 & Q4 + more?', 'a#b/c', '50% off', 'Ünïcode']) {
      const route: Route = { name: 'new', step: 2, castTemplate }
      expect(parseHash(hrefOf(route))).toEqual(route)
    }
  })

  it('an empty cast or pack param is no param', () => {
    expect(parseHash('#/new/2?cast=&pack=')).toEqual({ name: 'new', step: 2 })
  })

  it('anything unrecognised is the run list, never an error page', () => {
    for (const h of ['', '#', '#/nope', '#/run', '#/run/x/bogus-tab', '#/new/9']) {
      const r = parseHash(h)
      expect(['runs', 'run', 'new']).toContain(r.name)
    }
    expect(parseHash('#/run/x/bogus-tab')).toEqual({ name: 'run', runId: 'x', tab: 'conversation' })
    expect(parseHash('#/new/9')).toEqual({ name: 'new' })
  })
})
