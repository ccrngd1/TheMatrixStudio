// SPDX-License-Identifier: Apache-2.0
import { useEffect, useRef, useState } from 'react'
import { api, type EnsembleSummary } from '../api'
import type { RunSummary } from '../types'
import { isStalled } from '../lib/runStatus'

// How long a load may take before the view says why it is still waiting.
//
// The deployed API is a container-image Lambda: warm requests answer in ~56 ms, but a
// cold start costs ~5.7 s of init, and when init overruns Lambda's hard 10 s limit it is
// aborted and RETRIED — one measured cold start took 24.8 s. It returned 200; the run
// list was simply not on screen for 25 seconds, which reads as a broken app. Naming the
// cause is the difference between waiting and reloading.
const SLOW_AFTER_MS = 3000

interface Props {
  onOpen: (runId: string) => void
  onNew: () => void
  /** Optional so existing tests that render History alone keep working; the button is
   *  simply absent without it rather than rendering a control that does nothing. */
  onKnowledgeBases?: () => void
  /** Optional for the same reason. Without it the ensembles section is not rendered at
   *  all — a list of ensembles nobody can open would be worse than no list. */
  onOpenEnsemble?: (ensembleId: string) => void
}

export function History({ onOpen, onNew, onKnowledgeBases, onOpenEnsemble }: Props) {
  const [runs, setRuns] = useState<RunSummary[]>([])
  // Ensembles are listed ABOVE the runs rather than mixed into them. They are a different
  // kind of thing — no transcript, no turn count — and interleaving them by date would put a
  // row with no turns and no cost in a list whose columns are turns and cost.
  const [ensembles, setEnsembles] = useState<EnsembleSummary[]>([])
  const [q, setQ] = useState('')
  const [loading, setLoading] = useState(true)
  const [slow, setSlow] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = (query?: string) => {
    setLoading(true)
    setSlow(false)
    const slowTimer = setTimeout(() => setSlow(true), SLOW_AFTER_MS)
    api
      .listRuns(query)
      .then((rows) => {
        setRuns(rows)
        setError(null)
      })
      .catch((err) => {
        // A failure used to be swallowed into an empty list, which renders as "No runs
        // yet" — indistinguishable from an account with no runs. On a cold start that
        // times out, that tells the user their history is gone when it is not.
        setRuns([])
        setError(err instanceof Error ? err.message : String(err))
      })
      .finally(() => {
        clearTimeout(slowTimer)
        setLoading(false)
        setSlow(false)
      })
  }

  // One fetch on mount, then debounced fetches as the query changes.
  //
  // This used to be two effects, and both fired on mount: an immediate `load()` and the
  // debounced one 250 ms later with an identical query. Two identical requests is
  // ordinarily just waste, but against a cold Lambda each one starts its OWN sandbox and
  // pays its own ~5.7 s init — the deployed logs show three concurrent cold starts for
  // one page load. The ref keeps the first load immediate while removing the duplicate.
  const firstLoad = useRef(true)
  useEffect(() => {
    if (firstLoad.current) {
      firstLoad.current = false
      load()
      return
    }
    const id = setTimeout(() => load(q || undefined), 250)
    return () => clearTimeout(id)
  }, [q])

  // Ensembles load once and are not searched. Separate from `load` on purpose: the run list
  // must not depend on this request succeeding, because an older deployment has no
  // `/api/ensembles` route and folding the two together would make a 404 there empty the run
  // list. A failure here leaves the section absent, which is the honest degradation.
  useEffect(() => {
    if (!onOpenEnsemble) return
    let live = true
    api
      .listEnsembles()
      .then((rows) => {
        if (live) setEnsembles(rows)
      })
      .catch(() => {
        if (live) setEnsembles([])
      })
    return () => {
      live = false
    }
  }, [onOpenEnsemble])

  return (
    <div className="mx-auto max-w-4xl p-6">
      <div className="mb-4 flex items-center justify-between">
        <h1 className="text-2xl font-bold text-slate-100">TheMatrix Simulation Studio</h1>
        <div className="flex items-center gap-2">
          {onKnowledgeBases && (
            <button
              onClick={onKnowledgeBases}
              className="rounded-lg border border-matrix-border px-3 py-2 text-sm text-slate-300 hover:text-slate-100"
            >
              Knowledge bases
            </button>
          )}
          <button
            onClick={onNew}
            className="rounded-lg bg-matrix-accent px-4 py-2 font-semibold text-matrix-bg hover:bg-sky-400"
          >
            + New run
          </button>
        </div>
      </div>

      <input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Search by name, description, or topic…"
        className="mb-4 w-full rounded-lg border border-matrix-border bg-matrix-panel p-2 text-sm"
      />

      {onOpenEnsemble && ensembles.length > 0 && (
        <section className="mb-6">
          <h2 className="mb-2 text-xs uppercase tracking-wide text-slate-500">Ensembles</h2>
          <div className="space-y-2">
            {ensembles.map((e) => {
              const runs = (e.spec ?? []).reduce((n, c) => n + (c.n ?? 0), 0)
              return (
                <button
                  key={e.ensemble_id}
                  onClick={() => onOpenEnsemble(e.ensemble_id)}
                  className="flex w-full items-center justify-between rounded-lg border border-matrix-border bg-matrix-panel p-3 text-left hover:border-matrix-accent"
                >
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-semibold text-matrix-accent">
                        {e.name ?? e.ensemble_id.slice(0, 8)}
                      </span>
                      <span className="rounded bg-matrix-border px-2 py-0.5 text-[10px] uppercase tracking-wide text-slate-300">
                        {runs} run{runs === 1 ? '' : 's'}
                        {(e.spec ?? []).length > 1 ? `, ${e.spec.length} groups` : ''}
                      </span>
                      {/* Three states, not two. "No report yet" and "a report was refused"
                          are different, and only one of them is worth opening to retry. */}
                      {e.report_error ? (
                        <span className="rounded bg-rose-900/40 px-2 py-0.5 text-[10px] uppercase tracking-wide text-rose-200">
                          report failed
                        </span>
                      ) : e.has_report ? (
                        <span className="rounded bg-emerald-900/40 px-2 py-0.5 text-[10px] uppercase tracking-wide text-emerald-200">
                          report ready
                        </span>
                      ) : (
                        <span className="rounded bg-matrix-border px-2 py-0.5 text-[10px] uppercase tracking-wide text-slate-400">
                          {e.status ?? 'pending'}
                        </span>
                      )}
                    </div>
                    <p className="truncate text-sm text-slate-300">
                      {e.description ?? e.topic}
                    </p>
                  </div>
                  <div className="ml-3 whitespace-nowrap text-right text-xs text-slate-500">
                    {e.report_cost_usd != null && <div>${e.report_cost_usd.toFixed(4)} report</div>}
                  </div>
                </button>
              )
            })}
          </div>
        </section>
      )}

      {loading ? (
        <div className="text-slate-500">
          <p>Loading…</p>
          {slow && (
            <p className="mt-2 text-xs text-slate-500">
              Still waiting on the API. The first request after an idle period starts a
              new Lambda sandbox and can take up to 30 seconds; it is not stuck.
            </p>
          )}
        </div>
      ) : error ? (
        <div className="rounded-lg border border-red-900/60 bg-red-900/20 p-4">
          <p className="text-sm text-red-300">Could not load your runs: {error}</p>
          <button
            onClick={() => load(q || undefined)}
            className="mt-3 rounded border border-matrix-border px-3 py-1 text-xs text-slate-300 hover:text-slate-100"
          >
            Try again
          </button>
        </div>
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
                  {/* `lastEventAt` and `createdAt` were never passed, so the
                      staleness branch inside StatusPill could not fire: a run orphaned
                      by a restart rendered as a healthy "running" for ever. The API has
                      supplied `last_event_at` for exactly this since it was added. */}
                  <StatusPill
                    status={r.status}
                    lastEventAt={r.last_event_at}
                    createdAt={r.created_at}
                  />
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

// A run marked "running" whose most recent event is older than this is treated
// as stalled/orphaned (server up, but no live stream and no recent activity).
// Generous so a slow multi-agent turn is never mislabelled.
const STALL_SECONDS = 120

function StatusPill({
  status,
  lastEventAt,
  createdAt,
}: {
  status: string
  lastEventAt?: number | null
  createdAt?: number | null
}) {
  // Item 2: a "running" row in history has no live stream by definition; if it
  // also has no recent events, show it as stalled rather than falsely live.
  //
  // `pending` is included, and that is the more important half now. A run is created
  // `pending` and its first turn flips it to `running` — so a run stuck at `pending` is
  // one whose execution never started, which is a real and silent failure mode
  // (`start_execution` returning None, a denied StartExecution, a state machine that is
  // not there). Without this it renders as "pending" for ever, indistinguishable from a
  // run that is about to begin.
  const stalled = isStalled(status, lastEventAt, createdAt, STALL_SECONDS)
  const shown = stalled ? 'stalled' : status
  const color =
    shown === 'complete'
      ? 'bg-matrix-live/20 text-matrix-live'
      : shown === 'running'
        ? 'bg-matrix-accent/20 text-matrix-accent'
        : shown === 'failed'
          ? 'bg-red-900/40 text-red-300'
          : shown === 'stalled' || shown === 'interrupted'
            ? 'bg-amber-900/40 text-amber-300'
            : 'bg-matrix-border text-slate-400'
  const title = stalled
    ? status === 'pending'
      ? 'Created but never started generating — its execution may have failed to start'
      : 'Marked running but no recent events — likely orphaned by a server restart mid-run'
    : undefined
  return (
    <span
      title={title}
      className={`rounded px-2 py-0.5 text-[10px] uppercase tracking-wide ${color}`}
    >
      {shown}
    </span>
  )
}
