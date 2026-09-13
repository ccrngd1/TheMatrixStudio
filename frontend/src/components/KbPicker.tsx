// SPDX-License-Identifier: Apache-2.0
// Choosing which knowledge bases a run — or one persona in it — may search.
//
// This is the last step that makes Phase 6 usable end to end. Collections could be
// created, filled and shared, and there was no way to bring one into a conversation
// without hand-posting JSON.
//
// ## Why a shared collection is offered, and what that costs
//
// A KB shared WITH you is bindable: that is the entire point of sharing. But a binding is
// not permission — the server intersects bindings with grants on every turn — so between
// picking a collection here and the run starting, the owner can revoke and creation
// returns a 422 naming it. "Shared with me" and "bindable" are therefore *almost* the
// same set, and the form must be able to say which one failed rather than showing a
// generic error.
//
// ## Why bindings are not documents
//
// The form already has a per-persona document box. A pasted document is indexed for that
// run alone; a bound collection is indexed once and searchable from every conversation
// that binds it. Presenting them as one control would hide the difference that Phase 6
// exists to create, so they are separate sections with the distinction stated.

import { useEffect, useState } from 'react'
import { api } from '../api'
import type { KnowledgeBase } from '../types'

interface Props {
  /** Currently bound KB ids. */
  selected: string[]
  onChange: (ids: string[]) => void
  /** Run level binds for every persona; persona level binds for one. */
  level: 'run' | 'persona'
  /** Shown in the empty state so the label matches the level. */
  personaName?: string
}

export function KbPicker({ selected, onChange, level, personaName }: Props) {
  const [rows, setRows] = useState<KnowledgeBase[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    // Wrapped, because `.catch` only covers a REJECTED promise. A `listKnowledgeBases`
    // that throws synchronously — absent, or an api module shaped differently from the
    // one this expects — threw inside an effect and took the whole new-run form down
    // with it. A picker that cannot load its options must degrade to "no options", never
    // stop somebody starting a run.
    const load = async () => {
      try {
        return await api.listKnowledgeBases()
      } catch (err) {
        throw err instanceof Error ? err : new Error(String(err))
      }
    }
    load()
      .then((body) => {
        if (alive) {
          setRows(body?.knowledge_bases ?? [])
          setError(null)
        }
      })
      .catch((err) => {
        // Surfaced, not swallowed into an empty list: "you have no collections" and
        // "the request failed" are indistinguishable otherwise, and the first would
        // make a user create a duplicate of something they already have.
        if (alive) setError(err instanceof Error ? err.message : String(err))
      })
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
  }, [])

  const toggle = (id: string) =>
    onChange(selected.includes(id) ? selected.filter((k) => k !== id) : [...selected, id])

  if (loading) {
    return <p className="text-xs text-slate-500">Loading collections…</p>
  }
  if (error) {
    return (
      <p className="text-xs text-amber-300">
        Could not load your collections ({error}). You can still start the run; it simply
        will not search any.
      </p>
    )
  }
  if (rows.length === 0) {
    return (
      <p className="text-xs text-slate-500">
        No knowledge bases yet. Create one from “Knowledge bases” on the history screen,
        then bind it here.
      </p>
    )
  }

  return (
    <div className="space-y-1">
      {rows.map((kb) => (
        <label
          key={kb.id}
          className="flex cursor-pointer items-center gap-2 text-sm text-slate-300"
        >
          <input
            type="checkbox"
            checked={selected.includes(kb.id)}
            onChange={() => toggle(kb.id)}
            aria-label={
              level === 'run'
                ? `Bind ${kb.name} to every persona`
                : `Bind ${kb.name} to ${personaName || 'this persona'}`
            }
          />
          <span className="truncate">{kb.name}</span>
          {kb.shared && (
            <span
              title="Someone else owns this and shared it with you. Bindable — but if they revoke the grant before the run starts, creation is refused."
              className="rounded bg-matrix-border px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-slate-400"
            >
              shared
            </span>
          )}
          <span className="ml-auto whitespace-nowrap text-xs text-slate-500">
            {kb.document_count ?? 0} doc(s)
          </span>
        </label>
      ))}
    </div>
  )
}
