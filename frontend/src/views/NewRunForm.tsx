// SPDX-License-Identifier: Apache-2.0
import { useEffect, useState } from 'react'
import { api } from '../api'

interface Props {
  onStarted: (runId: string) => void
  onCancel: () => void
}

interface DraftPersona {
  name: string
  persona: string
  goals: string // newline/semicolon separated in the form
}

const EXAMPLE = {
  topic: 'The merits and drawbacks of artificial intelligence in creative work',
  cast: [
    {
      name: 'Maya',
      persona:
        'A traditional artist who values human creativity and emotional authenticity. Skeptical of AI in art but open to thoughtful discussion.',
      goals: 'Express concerns about AI replacing human artists\nAdvocate for human experience in art',
    },
    {
      name: 'Alex',
      persona:
        'A tech-optimist and AI researcher who sees AI as a tool for expanding creative possibilities. Pragmatic and forward-thinking.',
      goals: 'Demonstrate how AI can augment human creativity',
    },
  ],
}

export function NewRunForm({ onStarted, onCancel }: Props) {
  const [topic, setTopic] = useState('')
  const [cast, setCast] = useState<DraftPersona[]>([{ name: '', persona: '', goals: '' }])
  const [maxMessages, setMaxMessages] = useState(10)
  const [avatars, setAvatars] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [model, setModel] = useState('')
  const [models, setModels] = useState<string[]>([])
  const [suggesting, setSuggesting] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.getModels().then((m) => {
      setModels(m.models)
      setModel(m.default)
    }).catch(() => undefined)
  }, [])

  const suggest = async () => {
    if (!topic.trim()) return
    setSuggesting(true)
    try {
      const s = await api.suggestName(topic)
      setName(s.name)
      setDescription(s.description)
    } catch {
      /* naming never blocks — leave fields for the user */
    } finally {
      setSuggesting(false)
    }
  }

  const loadExample = () => {
    setTopic(EXAMPLE.topic)
    setCast(EXAMPLE.cast.map((c) => ({ ...c })))
    setMaxMessages(12)
  }

  const updatePersona = (i: number, patch: Partial<DraftPersona>) =>
    setCast((prev) => prev.map((p, idx) => (idx === i ? { ...p, ...patch } : p)))

  const submit = async () => {
    setError(null)
    const validCast = cast
      .filter((c) => c.name.trim() && c.persona.trim())
      .map((c) => ({
        name: c.name.trim(),
        persona: c.persona.trim(),
        goals: c.goals
          .split(/[\n;]+/)
          .map((g) => g.trim())
          .filter(Boolean),
      }))
    if (!topic.trim() || validCast.length === 0) {
      setError('A topic and at least one persona (name + persona) are required.')
      return
    }
    setSubmitting(true)
    try {
      const res = await api.createRun({
        topic: topic.trim(),
        cast: validCast,
        config: { max_messages: maxMessages, generate_avatars: avatars },
        model: model || undefined,
        name: name.trim() || undefined,
        description: description.trim() || undefined,
      })
      onStarted(res.run_id)
    } catch (e) {
      setError((e as Error).message)
      setSubmitting(false)
    }
  }

  return (
    <div className="mx-auto max-w-3xl p-6">
      <div className="mb-4 flex items-center justify-between">
        <h1 className="text-2xl font-bold text-slate-100">New simulation</h1>
        <div className="flex gap-2">
          <button onClick={loadExample} className="rounded border border-matrix-border px-3 py-1 text-sm hover:border-matrix-accent">
            Load example
          </button>
          <button onClick={onCancel} className="rounded border border-matrix-border px-3 py-1 text-sm hover:border-matrix-accent">
            Cancel
          </button>
        </div>
      </div>

      {error && <p className="mb-3 rounded bg-red-950/50 p-2 text-sm text-red-300">{error}</p>}

      <label className="block text-sm font-semibold text-slate-300">Topic</label>
      <textarea
        value={topic}
        onChange={(e) => setTopic(e.target.value)}
        rows={2}
        className="mt-1 w-full rounded border border-matrix-border bg-matrix-bg p-2 text-sm"
        placeholder="What should the cast discuss?"
      />

      <div className="mt-4 grid grid-cols-1 gap-3 rounded-lg border border-matrix-border p-3 sm:grid-cols-[1fr_auto]">
        <div>
          <label className="block text-sm font-semibold text-slate-300">Run name (codename)</label>
          <div className="mt-1 flex gap-2">
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="w-full rounded border border-matrix-border bg-matrix-bg p-2 text-sm"
              placeholder="auto-generated (editable)"
            />
            <button
              onClick={suggest}
              disabled={suggesting || !topic.trim()}
              className="whitespace-nowrap rounded bg-matrix-accent/20 px-3 py-1 text-sm text-matrix-accent hover:bg-matrix-accent/30 disabled:opacity-40"
              title="Generate a topical codename"
            >
              {suggesting ? '…' : '🎲 Re-roll'}
            </button>
          </div>
          <input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            className="mt-2 w-full rounded border border-matrix-border bg-matrix-bg p-2 text-sm"
            placeholder="one-line description (auto-suggested, editable)"
          />
        </div>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-4">
        <label className="flex items-center gap-2 text-sm text-slate-300">
          Max messages
          <input
            type="number"
            min={1}
            max={100}
            value={maxMessages}
            onChange={(e) => setMaxMessages(Number(e.target.value))}
            className="w-20 rounded border border-matrix-border bg-matrix-bg p-1 text-sm"
          />
        </label>
        <label className="flex items-center gap-2 text-sm text-slate-300">
          <input type="checkbox" checked={avatars} onChange={(e) => setAvatars(e.target.checked)} />
          Generate avatars
        </label>
        {models.length > 0 && (
          <label className="flex items-center gap-2 text-sm text-slate-300">
            Model
            <select
              value={model}
              onChange={(e) => setModel(e.target.value)}
              className="rounded border border-matrix-border bg-matrix-bg p-1 text-sm"
            >
              {models.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      <div className="mt-5">
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-300">Cast</h2>
          <button
            onClick={() => setCast((p) => [...p, { name: '', persona: '', goals: '' }])}
            className="rounded border border-matrix-border px-2 py-1 text-xs hover:border-matrix-accent"
          >
            + Add persona
          </button>
        </div>
        <div className="space-y-3">
          {cast.map((p, i) => (
            <div key={i} className="rounded-lg border border-matrix-border p-3">
              <div className="flex items-center gap-2">
                <input
                  value={p.name}
                  onChange={(e) => updatePersona(i, { name: e.target.value })}
                  placeholder="Name"
                  className="w-40 rounded border border-matrix-border bg-matrix-bg p-2 text-sm"
                />
                {cast.length > 1 && (
                  <button
                    onClick={() => setCast((prev) => prev.filter((_, idx) => idx !== i))}
                    className="ml-auto text-xs text-slate-500 hover:text-red-400"
                  >
                    remove
                  </button>
                )}
              </div>
              <textarea
                value={p.persona}
                onChange={(e) => updatePersona(i, { persona: e.target.value })}
                placeholder="Persona description"
                rows={2}
                className="mt-2 w-full rounded border border-matrix-border bg-matrix-bg p-2 text-sm"
              />
              <textarea
                value={p.goals}
                onChange={(e) => updatePersona(i, { goals: e.target.value })}
                placeholder="Goals (one per line)"
                rows={2}
                className="mt-2 w-full rounded border border-matrix-border bg-matrix-bg p-2 text-sm"
              />
            </div>
          ))}
        </div>
      </div>

      <button
        onClick={submit}
        disabled={submitting}
        className="mt-6 w-full rounded-lg bg-matrix-accent py-3 font-semibold text-matrix-bg hover:bg-sky-400 disabled:opacity-50"
      >
        {submitting ? 'Starting…' : '▶ Run simulation'}
      </button>
    </div>
  )
}
