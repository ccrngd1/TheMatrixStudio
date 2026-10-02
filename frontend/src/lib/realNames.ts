// SPDX-License-Identifier: Apache-2.0
// Personas never carry a real person's name (the owner's rule, 2026-10-02). The server enforces it: every path a
// cast enters by checks each persona's and consultant's name, and switches a real public figure's to a clearly
// fictional sound-alike. This is the form's half: asking as names are entered, so the switch happens where the
// user typed, with the reason beside it, rather than surfacing only in the run that starts.
//
// Advisory, never a gate. A check that fails (offline, an older server) leaves the form exactly as it was, and
// the server applies the same check at creation regardless.
import { useCallback, useRef } from 'react'
import { api } from '../api'
import type { Renamed } from '../types'

/** The most names the endpoint takes in one request. */
const MAX_PER_REQUEST = 40

/** The form's cache key for a name: the same person however it was spaced or capitalised. */
export const nameKey = (name: string) => name.trim().replace(/\s+/g, ' ').toLowerCase()

/** What the notice on a renamed persona or consultant says. */
export function renamedMessage(r: Renamed, kind: 'persona' | 'consultant' = 'persona'): string {
  const who = kind === 'persona' ? 'Personas' : 'Consultants'
  return `'${r.from}' is a real public figure, so this ${kind} is '${r.to}'. ${who} never use real people's names.`
}

/**
 * Ask about names, once each. Resolves to the hits among `names` (by `nameKey`) and never rejects.
 *
 * Results are cached per name for the life of the form, so a blur, a re-render or a re-ordered cast does not ask
 * again — and each fictional replacement is cached as fine, so the switch does not prompt a second request. A
 * failed request caches nothing, so the next blur tries again.
 */
export function useRealNameCheck() {
  const known = useRef(new Map<string, Renamed | null>())
  const asking = useRef(new Set<string>())
  return useCallback(async (names: readonly string[]): Promise<Map<string, Renamed>> => {
    const byKey = new Map<string, string>()
    for (const n of names) if (n.trim()) byKey.set(nameKey(n), n.trim())
    const ask = [...byKey.keys()].filter((k) => !known.current.has(k) && !asking.current.has(k))
    for (let i = 0; i < ask.length; i += MAX_PER_REQUEST) {
      const batch = ask.slice(i, i + MAX_PER_REQUEST)
      batch.forEach((k) => asking.current.add(k))
      try {
        // Through a promise chain, so a client without the route degrades to "no check" rather than throwing.
        const res = await Promise.resolve().then(() => api.checkNames(batch.map((k) => byKey.get(k) as string)))
        const hits = new Map((res?.renamed ?? []).map((r) => [nameKey(r.from), r]))
        for (const k of batch) known.current.set(k, hits.get(k) ?? null)
        for (const r of hits.values()) known.current.set(nameKey(r.to), null)
      } catch {
        // Not cached: see above.
      } finally {
        batch.forEach((k) => asking.current.delete(k))
      }
    }
    const out = new Map<string, Renamed>()
    for (const k of byKey.keys()) {
      const r = known.current.get(k)
      if (r) out.set(k, r)
    }
    return out
  }, [])
}

/**
 * Switch every row whose name came back renamed, and record why on that row. Rows `skip` says to leave alone (the
 * one being typed in) are untouched. Returns the same array when nothing changed, so a state update is a no-op.
 */
export function applyRenames<T extends { name: string; renamed?: Renamed }>(
  rows: T[], hits: Map<string, Renamed>, skip: (i: number) => boolean = () => false,
): T[] {
  let changed = false
  const next = rows.map((row, i) => {
    const r = skip(i) ? undefined : hits.get(nameKey(row.name))
    if (!r) return row
    changed = true
    return { ...row, name: r.to, renamed: r }
  })
  return changed ? next : rows
}

/**
 * Mark the rows a server response already renamed (the wizard's draft, a loaded template). Their names are the
 * fictional ones by then, so asking again would find nothing; the response's `renamed` list is what says why.
 */
export function attachRenamed<T extends { name: string; renamed?: Renamed }>(rows: T[], renamed?: Renamed[]): T[] {
  if (!renamed?.length) return rows
  return rows.map((row) => {
    const r = renamed.find((x) => nameKey(x.to) === nameKey(row.name))
    return r ? { ...row, renamed: r } : row
  })
}

/** Whether a row's notice still applies: it goes once the user changes the name to something else. */
export const showsRenamed = (row: { name: string; renamed?: Renamed }) =>
  !!row.renamed && nameKey(row.name) === nameKey(row.renamed.to)
