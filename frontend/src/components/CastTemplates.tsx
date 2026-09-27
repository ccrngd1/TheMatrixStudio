// SPDX-License-Identifier: Apache-2.0
import { useEffect, useState } from 'react'
import { api, type CastTemplateSummary, type CreateRunBody } from '../api'
import { parseCast } from '../lib/importSetup'
import type { DraftPersona } from '../views/newRunTypes'

interface Props {
  /** The cast as the API takes it; empty when no persona is complete yet. */
  getCast: () => CreateRunBody['cast']
  /** Replace the form's cast with a template's. */
  onLoad: (cast: DraftPersona[], warnings: string[]) => void
  /** Whether the form already has a cast that loading would replace. */
  hasCast: boolean
}

// Save the cast under a name, and start a later conversation from it.
//
// The cast is the slowest part of a setup to get right — convictions, what would change each
// persona's mind, which collections each may search — and it was rebuilt from nothing every time.
// A template keeps exactly that. Pasted documents are left out server-side (they belong to one
// run); the save says how many, so nobody discovers it at launch.
export function CastTemplates({ getCast, onLoad, hasCast }: Props) {
  const [templates, setTemplates] = useState<CastTemplateSummary[] | null>(null)
  const [saving, setSaving] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [clash, setClash] = useState(false)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<{ text: string; error?: boolean } | null>(null)

  // Through a promise chain, so a deployment without the route shows no templates rather than
  // breaking the form around it.
  const refresh = () =>
    Promise.resolve()
      .then(() => api.listCastTemplates())
      .then(setTemplates)
      .catch(() => setTemplates([]))

  useEffect(() => {
    void refresh()
  }, [])

  const load = async (templateName: string) => {
    if (!templateName) return
    if (hasCast && !window.confirm(`Replace the current cast with “${templateName}”?`)) return
    setBusy(true)
    setMessage(null)
    try {
      const t = await api.getCastTemplate(templateName)
      const { cast, warnings } = parseCast(t.cast)
      onLoad(cast, warnings)
      setMessage({ text: `Loaded “${t.name}” — ${cast.length} persona${cast.length === 1 ? '' : 's'}.` })
    } catch (e) {
      setMessage({ text: `Could not load it: ${(e as Error).message}`, error: true })
    } finally {
      setBusy(false)
    }
  }

  const save = async (overwrite: boolean) => {
    const cast = getCast()
    if (!cast.length) {
      setMessage({ text: 'Add at least one persona with a name and description first.', error: true })
      return
    }
    setBusy(true)
    setMessage(null)
    try {
      const res = await api.saveCastTemplate({
        name: name.trim(),
        description: description.trim() || undefined,
        cast,
        overwrite,
      })
      setSaving(false)
      setClash(false)
      setName('')
      setDescription('')
      setMessage({
        text:
          `Saved “${res.name}”.` +
          (res.dropped_documents
            ? ` ${res.dropped_documents} pasted document${res.dropped_documents === 1 ? ' was' : 's were'} ` +
              'not kept — bind a knowledge base to reuse a document.'
            : ''),
      })
      void refresh()
    } catch (e) {
      const text = (e as Error).message
      if (text.startsWith('409')) setClash(true)
      else setMessage({ text: `Could not save it: ${text}`, error: true })
    } finally {
      setBusy(false)
    }
  }

  const remove = async (templateName: string) => {
    if (!window.confirm(`Delete the template “${templateName}”? Conversations already started from it are unaffected.`)) return
    try {
      await api.deleteCastTemplate(templateName)
      void refresh()
    } catch (e) {
      setMessage({ text: `Could not delete it: ${(e as Error).message}`, error: true })
    }
  }

  const btn =
    'rounded border border-matrix-border px-2 py-1 text-xs hover:border-matrix-accent disabled:opacity-50'

  return (
    <div className="mb-2 space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        {templates && templates.length > 0 && (
          <select
            aria-label="Load a cast template"
            value=""
            disabled={busy}
            onChange={(e) => void load(e.target.value)}
            className="rounded border border-matrix-border bg-matrix-bg px-2 py-1 text-xs"
          >
            <option value="">Load template…</option>
            {templates.map((t) => (
              <option key={t.name} value={t.name}>
                {t.name} ({t.personas.length})
              </option>
            ))}
          </select>
        )}
        <button className={btn} disabled={busy} onClick={() => setSaving((s) => !s)}>
          Save as template
        </button>
        {templates && templates.length > 0 && (
          <details className="text-xs text-slate-500">
            <summary className="cursor-pointer hover:text-slate-300">Manage</summary>
            <ul className="mt-1 space-y-1">
              {templates.map((t) => (
                <li key={t.name} className="flex items-center gap-2">
                  <span className="text-slate-300">{t.name}</span>
                  <span className="truncate">{t.personas.join(', ')}</span>
                  <button
                    onClick={() => void remove(t.name)}
                    className="ml-auto text-rose-300 hover:underline"
                    aria-label={`Delete template ${t.name}`}
                  >
                    delete
                  </button>
                </li>
              ))}
            </ul>
          </details>
        )}
      </div>
      {saving && (
        <div className="flex flex-wrap items-center gap-2 rounded border border-matrix-border p-2">
          <input
            value={name}
            onChange={(e) => {
              setName(e.target.value)
              setClash(false)
            }}
            placeholder="Template name"
            maxLength={80}
            className="w-48 rounded border border-matrix-border bg-matrix-bg p-1 text-xs"
          />
          <input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="What this cast is for (optional)"
            maxLength={500}
            className="min-w-0 flex-1 rounded border border-matrix-border bg-matrix-bg p-1 text-xs"
          />
          {clash ? (
            <>
              <span className="text-xs text-amber-300">“{name.trim()}” exists.</span>
              <button className={btn} disabled={busy} onClick={() => void save(true)}>
                Replace it
              </button>
            </>
          ) : (
            <button
              className={btn}
              disabled={busy || !name.trim() || name.includes('/')}
              onClick={() => void save(false)}
            >
              Save
            </button>
          )}
        </div>
      )}
      {message && (
        <p className={`text-xs ${message.error ? 'text-rose-300' : 'text-slate-400'}`}>{message.text}</p>
      )}
    </div>
  )
}
