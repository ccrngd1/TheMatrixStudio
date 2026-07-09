// SPDX-License-Identifier: Apache-2.0
import { useState } from 'react'
import { api } from '../api'
import type { StoredSummary } from '../types'

interface Props {
  runId: string
  generated: StoredSummary | null
  imported: StoredSummary | null
  // The default analyst-role framing (from GET summary). Prefills the editor and
  // powers "reset to default" when the current summary used the default (NULL).
  defaultInstructions: string
  // Only completed runs can (re)generate a summary.
  canGenerate: boolean
  onUpdated: (s: StoredSummary) => void
}

// Post-run analysis summary. Everything here is model-generated ANALYSIS of the
// transcript — labeled as such, visually distinct from the canonical run, and
// never presented as ground truth (honesty gate). An imported original (from a
// legacy run) is shown separately and is never overwritten.
export function SummaryPanel({
  runId,
  generated,
  imported,
  defaultInstructions,
  canGenerate,
  onUpdated,
}: Props) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // When the editor is open, the user can alter the summarization PROMPT (the
  // analyst-role framing) before regenerating. The guardrails (JSON structure +
  // no-fabrication) are enforced server-side and are NOT editable here.
  const [editing, setEditing] = useState(false)
  const [prompt, setPrompt] = useState('')

  // The prompt that created the current summary: its stored instructions when a
  // custom prompt was used, otherwise the default framing.
  const currentPrompt = generated?.instructions ?? defaultInstructions

  const openEditor = () => {
    setPrompt(currentPrompt)
    setError(null)
    setEditing(true)
  }

  const generate = async () => {
    setBusy(true)
    setError(null)
    try {
      // Only send custom instructions when the user diverged from the default;
      // an unchanged/default prompt sends none → server uses the default.
      const trimmed = prompt.trim()
      const body =
        trimmed && trimmed !== defaultInstructions.trim()
          ? { instructions: trimmed }
          : undefined
      const res = await api.generateSummary(runId, body)
      onUpdated(res.generated)
      setEditing(false)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const p = generated?.payload

  return (
    <section className="rounded-lg border border-matrix-border bg-matrix-panel p-4">
      <div className="mb-2 flex items-center justify-between">
        <div>
          <h2 className="text-sm font-semibold uppercase tracking-wide text-matrix-accent">
            Summary
          </h2>
          <p className="text-[11px] text-slate-500">
            Model-generated analysis of the transcript — not the conversation itself.
          </p>
        </div>
        {canGenerate && !editing && (
          <button
            onClick={openEditor}
            disabled={busy}
            className="rounded bg-matrix-accent/20 px-3 py-1 text-xs text-matrix-accent hover:bg-matrix-accent/30 disabled:opacity-40"
          >
            {generated ? '↻ Regenerate' : 'Generate summary'}
          </button>
        )}
      </div>

      {canGenerate && editing && (
        <div className="mb-3 rounded border border-matrix-border bg-black/20 p-3">
          <label className="mb-1 block text-[11px] font-semibold uppercase tracking-wide text-slate-400">
            Summarization prompt
          </label>
          <textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            rows={5}
            disabled={busy}
            className="w-full rounded border border-matrix-border bg-matrix-panel p-2 text-xs text-slate-200 focus:border-matrix-accent focus:outline-none disabled:opacity-40"
            placeholder={defaultInstructions}
          />
          <p className="mt-1 text-[11px] text-slate-500">
            This replaces the analyst-role framing only. The JSON structure and the
            no-fabrication (base strictly on the transcript) rules are enforced
            automatically and are not editable.
          </p>
          <div className="mt-2 flex items-center gap-3">
            <button
              onClick={generate}
              disabled={busy}
              className="rounded bg-matrix-accent/20 px-3 py-1 text-xs text-matrix-accent hover:bg-matrix-accent/30 disabled:opacity-40"
            >
              {busy ? 'Analyzing…' : 'Regenerate with this prompt'}
            </button>
            <button
              onClick={() => setPrompt(defaultInstructions)}
              disabled={busy}
              className="text-[11px] text-slate-400 underline hover:text-slate-200 disabled:opacity-40"
            >
              Reset to default
            </button>
            <button
              onClick={() => setEditing(false)}
              disabled={busy}
              className="ml-auto text-[11px] text-slate-500 hover:text-slate-300 disabled:opacity-40"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {error && (
        <p className="mb-2 rounded bg-red-950/50 p-2 text-xs text-red-300">{error}</p>
      )}

      {imported && (
        <div className="mb-3 rounded border border-amber-800/50 bg-amber-950/20 p-3">
          <h3 className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-amber-400">
            Original (imported) summary
          </h3>
          <p className="whitespace-pre-wrap text-sm text-slate-300">
            {imported.payload.overview || JSON.stringify(imported.payload)}
          </p>
        </div>
      )}

      {!generated && !imported && (
        <p className="text-sm text-slate-500">
          {canGenerate
            ? 'No summary yet — generate one from the transcript.'
            : 'No summary available.'}
        </p>
      )}

      {p && (
        <div className="space-y-3">
          {generated && generated.parsed === false && (
            <p className="rounded bg-amber-950/30 p-2 text-[11px] text-amber-400">
              The model did not return structured JSON; showing a plain-text overview.
            </p>
          )}
          {p.overview && (
            <Block title="Overview">
              <p className="whitespace-pre-wrap text-sm text-slate-300">{p.overview}</p>
            </Block>
          )}
          {p.consensus && p.consensus.length > 0 && (
            <Block title="Consensus">
              <List items={p.consensus} />
            </Block>
          )}
          {p.dissenters && p.dissenters.length > 0 && (
            <Block title="Dissenters">
              <ul className="space-y-1 text-sm text-slate-300">
                {p.dissenters.map((d, i) => (
                  <li key={i}>
                    <span className="font-semibold text-slate-200">{d.speaker || 'Someone'}</span>
                    {d.position ? ` — ${d.position}` : ''}
                  </li>
                ))}
              </ul>
            </Block>
          )}
          {p.key_ideas && p.key_ideas.length > 0 && (
            <Block title="Key ideas">
              <List items={p.key_ideas} />
            </Block>
          )}
          {p.open_questions && p.open_questions.length > 0 && (
            <Block title="Open questions">
              <List items={p.open_questions} />
            </Block>
          )}
          {generated && (
            <p className="text-[11px] text-slate-500">
              Analysis cost ${generated.cost_usd.toFixed(4)} ·{' '}
              {(generated.tokens_in + generated.tokens_out).toLocaleString()} tokens · counted
              separately from the run.
            </p>
          )}
        </div>
      )}
    </section>
  )
}

function Block({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <h3 className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
        {title}
      </h3>
      {children}
    </div>
  )
}

function List({ items }: { items: string[] }) {
  return (
    <ul className="list-inside list-disc space-y-1 text-sm text-slate-300">
      {items.map((it, i) => (
        <li key={i}>{it}</li>
      ))}
    </ul>
  )
}
