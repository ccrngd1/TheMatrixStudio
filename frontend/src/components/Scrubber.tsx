// SPDX-License-Identifier: Apache-2.0
import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import type { Persona, SimEvent } from '../types'
import { deriveState, initialState } from '../lib/simState'
import { CastBoard } from './CastBoard'
import { ConversationFeed } from './ConversationFeed'

interface Props {
  runId: string
  // Highest turn available (parent's turn count). The slider spans 0..maxTurn.
  maxTurn: number
  cast: Persona[]
  // Called with the current scrubber turn when the user forks "from here".
  onBranch: (fromTurn: number) => void
  branching?: boolean
}

// Checkpoint scrubber (Phase 2a): a read-only turn slider that shows the cast
// board + conversation feed AS OF turn N. State is reconstructed by replaying
// the run's own event log up to the selected turn — the same fold the live view
// uses — so it works identically for a completed run, an imported run, or a
// branch. Nothing here mutates the run; the only write action is "Branch from
// here", which forks a NEW run and leaves this one untouched.
export function Scrubber({ runId, maxTurn, cast, onBranch, branching = false }: Props) {
  const [events, setEvents] = useState<SimEvent[]>([])
  const [turn, setTurn] = useState(maxTurn)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    setLoading(true)
    api
      .getEvents(runId)
      .then((evts) => {
        setEvents(evts)
        setTurn(maxTurn)
      })
      .catch(() => setEvents([]))
      .finally(() => setLoading(false))
  }, [runId, maxTurn])

  // Fold only the events up to and including the selected turn.
  const state = useMemo(() => {
    const upto = events.filter((e) => e.turn <= turn)
    return deriveState(initialState(cast), upto)
  }, [events, turn, cast])

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-matrix-border bg-matrix-panel px-4 py-3">
        <div className="mb-1 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold text-slate-200">Checkpoint scrubber</span>
            <span className="rounded bg-matrix-border px-2 py-0.5 text-[10px] uppercase tracking-wide text-slate-400">
              read-only
            </span>
          </div>
          <span className="text-xs text-slate-400">
            turn {turn} / {maxTurn}
          </span>
        </div>
        <div className="flex items-center gap-3">
          <input
            type="range"
            min={0}
            max={maxTurn}
            value={turn}
            aria-label="checkpoint turn"
            onChange={(e) => setTurn(Number(e.target.value))}
            className="flex-1 accent-matrix-accent"
          />
          <button
            onClick={() => onBranch(turn)}
            disabled={branching}
            title="Fork a new run that resumes forward from this turn — the original is preserved"
            className="whitespace-nowrap rounded bg-matrix-accent px-3 py-1 text-sm font-semibold text-matrix-bg hover:bg-sky-400 disabled:opacity-40"
          >
            {branching ? 'Branching…' : '⑂ Branch from here'}
          </button>
        </div>
        <p className="mt-1 text-[11px] text-slate-500">
          Viewing state as of turn {turn}. Branching forks a new run resuming from here; this run is
          never modified.
        </p>
      </div>

      <div className="grid flex-1 grid-cols-1 gap-4 overflow-hidden p-4 lg:grid-cols-[300px_1fr]">
        <aside className="overflow-y-auto">
          {loading ? (
            <p className="text-sm text-slate-500">Loading checkpoints…</p>
          ) : (
            <CastBoard state={state} onSelect={() => {}} />
          )}
        </aside>
        <main className="overflow-hidden rounded-lg border border-matrix-border bg-matrix-panel">
          <ConversationFeed
            feed={state.feed}
            agents={state.agents}
            activeSpeaker={null}
            thinking={false}
          />
        </main>
      </div>
    </div>
  )
}
