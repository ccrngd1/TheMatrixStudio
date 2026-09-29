// SPDX-License-Identifier: Apache-2.0
// Hash routes (docs/MOBILE-UI.md §3), so the back button and deep links work. The app had no router — views
// switched with useState, so reloading a run's page or sharing its link landed on the run list. Hash-based
// because the SPA is served from CloudFront with no server-side rewrite rule, and a path route would 404.
import { useEffect, useState } from 'react'

export type RunTab = 'conversation' | 'cast' | 'analysis'
export const RUN_TABS: RunTab[] = ['conversation', 'cast', 'analysis']

export type Route =
  | { name: 'runs' }
  | { name: 'ensembles' }
  | { name: 'knowledge'; kbId?: string }
  | { name: 'library' }
  | { name: 'new'; step?: number; fromRunId?: string }
  | { name: 'run'; runId: string; tab: RunTab }
  | { name: 'scrub'; runId: string }
  | { name: 'ensemble'; ensembleId: string }

/** Parse `#/run/abc/cast`-style hashes. Anything unrecognised is the run list, never an error page. */
export function parseHash(hash: string): Route {
  const [path, query = ''] = hash.replace(/^#\/?/, '').split('?')
  const parts = path.split('/').filter(Boolean).map(decodeURIComponent)
  const params = new URLSearchParams(query)
  switch (parts[0]) {
    case 'ensembles':
      return { name: 'ensembles' }
    case 'knowledge':
      return parts[1] ? { name: 'knowledge', kbId: parts[1] } : { name: 'knowledge' }
    case 'library':
      return { name: 'library' }
    case 'new': {
      const step = Number(parts[1])
      return {
        name: 'new',
        ...(Number.isInteger(step) && step >= 1 && step <= 5 ? { step } : {}),
        ...(params.get('from') ? { fromRunId: params.get('from')! } : {}),
      }
    }
    case 'run':
      if (!parts[1]) return { name: 'runs' }
      if (parts[2] === 'scrub') return { name: 'scrub', runId: parts[1] }
      return {
        name: 'run',
        runId: parts[1],
        tab: (RUN_TABS as string[]).includes(parts[2] ?? '') ? (parts[2] as RunTab) : 'conversation',
      }
    case 'ensemble':
      return parts[1] ? { name: 'ensemble', ensembleId: parts[1] } : { name: 'ensembles' }
    default:
      return { name: 'runs' }
  }
}

/** The hash for a route: the inverse of `parseHash`. */
export function hrefOf(r: Route): string {
  const e = encodeURIComponent
  switch (r.name) {
    case 'runs':
      return '#/runs'
    case 'ensembles':
      return '#/ensembles'
    case 'knowledge':
      return r.kbId ? `#/knowledge/${e(r.kbId)}` : '#/knowledge'
    case 'library':
      return '#/library'
    case 'new':
      return `#/new${r.step ? `/${r.step}` : ''}${r.fromRunId ? `?from=${e(r.fromRunId)}` : ''}`
    case 'run':
      return `#/run/${e(r.runId)}${r.tab && r.tab !== 'conversation' ? `/${r.tab}` : ''}`
    case 'scrub':
      return `#/run/${e(r.runId)}/scrub`
    case 'ensemble':
      return `#/ensemble/${e(r.ensembleId)}`
  }
}

export function navigate(r: Route, { replace = false } = {}) {
  const href = hrefOf(r)
  if (replace) window.history.replaceState(null, '', href)
  else if (window.location.hash !== href) window.location.hash = href
  // `replaceState` does not fire `hashchange`, so tell listeners ourselves.
  if (replace) window.dispatchEvent(new HashChangeEvent('hashchange'))
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash))
  useEffect(() => {
    const on = () => setRoute(parseHash(window.location.hash))
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])
  return route
}
