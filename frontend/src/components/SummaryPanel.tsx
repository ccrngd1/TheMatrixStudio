// SPDX-License-Identifier: Apache-2.0
import { useState } from 'react'
import { api } from '../api'
import type { StoredSummary } from '../types'

interface Props {
  runId: string
  generated: StoredSummary | null
  imported: StoredSummary | null
  // Only completed runs can (re)generate a summary.
  canGenerate: boolean
  onUpdated: (s: StoredSummary) => void
}

// Post-run analysis summary. Everything here is model-generated ANALYSIS of the
// transcript — labeled as such, visually distinct from the canonical run, and
// never presented as ground truth (honesty gate). An imported original (from a
// legacy run) is shown separately and is never overwritten.
export function SummaryPanel({ runId, generated, imported, canGenerate, onUpdated }: Props) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const generate = async () => {
    setBusy(true)
    setError(null)
    try {
      const res = await api.generateSummary(runId)
      onUpdated(res.generated)
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
        {canGenerate && (
          <button
            onClick={generate}
            disabled={busy}
            className="rounded bg-matrix-accent/20 px-3 py-1 text-xs text-matrix-accent hover:bg-matrix-accent/30 disabled:opacity-40"
          >
            {busy ? 'Analyzing…' : generated ? '↻ Regenerate' : 'Generate summary'}
          </button>
        )}
      </div>

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
