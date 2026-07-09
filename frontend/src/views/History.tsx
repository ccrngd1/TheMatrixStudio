// SPDX-License-Identifier: Apache-2.0
import { useEffect, useState } from 'react'
import { api } from '../api'
import type { RunSummary } from '../types'

interface Props {
  onOpen: (runId: string) => void
  onNew: () => void
}

export function History({ onOpen, onNew }: Props) {
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [q, setQ] = useState('')
  const [loading, setLoading] = useState(true)

  const load = (query?: string) => {
    setLoading(true)
    api
      .listRuns(query)
      .then(setRuns)
      .catch(() => setRuns([]))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    load()
  }, [])

  // Debounced search by name/description/topic.
  useEffect(() => {
    const id = setTimeout(() => load(q || undefined), 250)
    return () => clearTimeout(id)
  }, [q])

  return (
    <div className="mx-auto max-w-4xl p-6">
      <div className="mb-4 flex items-center justify-between">
        <h1 className="text-2xl font-bold text-slate-100">TheMatrix Simulation Studio</h1>
        <button
          onClick={onNew}
          className="rounded-lg bg-matrix-accent px-4 py-2 font-semibold text-matrix-bg hover:bg-sky-400"
        >
          + New run
        </button>
      </div>

      <input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Search by name, description, or topic…"
        className="mb-4 w-full rounded-lg border border-matrix-border bg-matrix-panel p-2 text-sm"
      />

      {loading ? (
        <p className="text-slate-500">Loading…</p>
      ) : runs.length === 0 ? (
        <p className="text-slate-500">No runs yet. Start one with “+ New run”.</p>
      ) : (
        <div className="space-y-2">
          {runs.map((r) => (
            <button
              key={r.run_id}
              onClick={() => onOpen(r.run_id)}
              className="flex w-full items-center justify-between rounded-lg border border-matrix-border bg-matrix-panel p-3 text-left hover:border-matrix-accent"
            >
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="font-semibold text-matrix-accent">{r.name ?? r.run_id.slice(0, 8)}</span>
                  <StatusPill status={r.status} />
                  {r.parent_run_id && (
                    <span
                      title={`Branched @ turn ${r.branch_turn}`}
                      className="rounded bg-matrix-border px-2 py-0.5 text-[10px] uppercase tracking-wide text-slate-300"
                    >
                      ⑂ branch @ {r.branch_turn}
                    </span>
                  )}
                </div>
                <p className="truncate text-sm text-slate-300">{r.description ?? r.topic}</p>
                <p className="truncate text-xs text-slate-500">{r.topic}</p>
              </div>
              <div className="ml-3 whitespace-nowrap text-right text-xs text-slate-500">
                <div>{r.turn_count} turns</div>
                <div>${(r.total_cost_usd ?? 0).toFixed(4)}</div>
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function StatusPill({ status }: { status: string }) {
  const color =
    status === 'complete'
      ? 'bg-matrix-live/20 text-matrix-live'
      : status === 'running'
        ? 'bg-matrix-accent/20 text-matrix-accent'
        : status === 'failed'
          ? 'bg-red-900/40 text-red-300'
          : 'bg-matrix-border text-slate-400'
  return <span className={`rounded px-2 py-0.5 text-[10px] uppercase tracking-wide ${color}`}>{status}</span>
}
