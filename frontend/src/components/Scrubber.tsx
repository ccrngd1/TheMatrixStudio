// SPDX-License-Identifier: Apache-2.0
import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import type { Persona, SimEvent } from '../types'
import { deriveState, initialState } from '../lib/simState'
import { CastBoard } from './CastBoard'
import { ConversationFeed } from './ConversationFeed'

interface Props {
  runId: string
  maxTurn: number
  cast: Persona[]
  // The run's original turn budget — the default length of a new discussion
  // round after an injected message.
  defaultBudget?: number
  // Selectable models + the default (inherited from the page's model picker).
  models?: { id: string; label: string }[]
  defaultModel?: string
  // Phase 2b: optional mutation + optional model override forwarded to the
  // branch; no mutation = plain fork.
  onBranch: (fromTurn: number, mutation?: Record<string, unknown>, model?: string) => void
  branching?: boolean
}

// Checkpoint scrubber (Phase 2a+2b): read-only turn slider + Phase 2b intervention panel.
export function Scrubber({ runId, maxTurn, cast, defaultBudget, models = [], defaultModel, onBranch, branching = false }: Props) {
  const [events, setEvents] = useState<SimEvent[]>([])
  const [turn, setTurn] = useState(maxTurn)
  const [loading, setLoading] = useState(true)
  const [showIntervene, setShowIntervene] = useState(false)
  const [mutKind, setMutKind] = useState<string>('inject_message')
  const [injectSpeaker, setInjectSpeaker] = useState('')
  const [injectContent, setInjectContent] = useState('')
  // Model for the intervention branch; defaults to the page's selected model.
  const [branchModel, setBranchModel] = useState<string>(defaultModel ?? '')
  // Length of the new discussion round after an injected message; defaults to
  // the run's original budget.
  const [injectTurns, setInjectTurns] = useState<number>(defaultBudget ?? maxTurn ?? 20)
  const [addBudget, setAddBudget] = useState(5)
  const [editPersona, setEditPersona] = useState('')
  const [editGoals, setEditGoals] = useState('')
  const [addName, setAddName] = useState('')
  const [addPersonaText, setAddPersonaText] = useState('')
  const [addGoals, setAddGoals] = useState('')
  const [removePersona, setRemovePersona] = useState('')

  useEffect(() => {
    setLoading(true)
    api.getEvents(runId).then((evts) => { setEvents(evts); setTurn(maxTurn) })
      .catch(() => setEvents([])).finally(() => setLoading(false))
  }, [runId, maxTurn])

  const state = useMemo(() => {
    const upto = events.filter((e) => e.turn <= turn)
    return deriveState(initialState(cast), upto)
  }, [events, turn, cast])

  const castNames = Object.keys(state.agents)

  const buildMutation = (): Record<string, unknown> | undefined => {
    if (mutKind === 'inject_message')
      return {
        kind: 'inject_message',
        speaker: injectSpeaker.trim(),
        content: injectContent.trim(),
        add_budget: injectTurns,
      }
    if (mutKind === 'continue') return { kind: 'continue', add_budget: addBudget }
    if (mutKind === 'edit_goal')
      return { kind: 'edit_goal', persona_name: editPersona,
               goals: editGoals.split('\n').map((g) => g.trim()).filter(Boolean) }
    if (mutKind === 'add_persona')
      return { kind: 'add_persona', name: addName.trim(), persona: addPersonaText.trim(),
               goals: addGoals.split('\n').map((g) => g.trim()).filter(Boolean) }
    if (mutKind === 'remove_persona') return { kind: 'remove_persona', persona_name: removePersona }
    return undefined
  }

  const handleBranch = (withMutation: boolean) =>
    onBranch(turn, withMutation ? buildMutation() : undefined, branchModel || undefined)

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-matrix-border bg-matrix-panel px-4 py-3">
        <div className="mb-1 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold text-slate-200">Checkpoint scrubber</span>
            <span className="rounded bg-matrix-border px-2 py-0.5 text-[10px] uppercase tracking-wide text-slate-400">read-only</span>
          </div>
          <span className="text-xs text-slate-400">turn {turn} / {maxTurn}</span>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <input type="range" min={0} max={maxTurn} value={turn} aria-label="checkpoint turn"
            onChange={(e) => setTurn(Number(e.target.value))}
            className="flex-1 min-w-[120px] accent-matrix-accent" />
          <button onClick={() => handleBranch(false)} disabled={branching}
            title="Fork a new run from this turn — original preserved"
            className="whitespace-nowrap rounded bg-matrix-accent px-3 py-1 text-sm font-semibold text-matrix-bg hover:bg-sky-400 disabled:opacity-40">
            {branching ? 'Branching…' : '⑂ Branch from here'}
          </button>
          <button onClick={() => setShowIntervene((v) => !v)}
            title="Branch with an intervention applied at this turn"
            className={`whitespace-nowrap rounded border px-3 py-1 text-sm ${showIntervene ? 'border-matrix-accent text-matrix-accent' : 'border-matrix-border text-slate-400 hover:border-sky-500 hover:text-sky-400'}`}>
            ⚡ Intervene
          </button>
        </div>

        {showIntervene && (
          <div className="mt-3 rounded border border-matrix-border bg-matrix-bg p-3 text-sm space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <label className="text-xs text-slate-400">Mutation</label>
              <select value={mutKind} onChange={(e) => setMutKind(e.target.value)}
                className="rounded border border-matrix-border bg-matrix-panel px-2 py-1 text-xs text-slate-200">
                <option value="inject_message">💬 Inject message</option>
                <option value="continue">▶ Continue (+N turns)</option>
                <option value="edit_goal">🎯 Edit goal</option>
                <option value="add_persona">➕ Add persona</option>
                <option value="remove_persona">➖ Remove persona</option>
              </select>
              {models.length > 0 && (
                <>
                  <label className="ml-auto text-xs text-slate-400">Model</label>
                  <select value={branchModel} onChange={(e) => setBranchModel(e.target.value)}
                    title="Model the branched discussion generates with (defaults to the page's model)"
                    className="max-w-[12rem] rounded border border-matrix-border bg-matrix-panel px-2 py-1 text-xs text-slate-200">
                    {models.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
                  </select>
                </>
              )}
            </div>

            {mutKind === 'inject_message' && (<>
              <input placeholder="Speaker name (can be new, e.g. Moderator)"
                value={injectSpeaker} onChange={(e) => setInjectSpeaker(e.target.value)}
                className="w-full rounded border border-matrix-border bg-matrix-bg px-2 py-1 text-xs text-slate-200" />
              <textarea placeholder="Message content…" value={injectContent} rows={3}
                onChange={(e) => setInjectContent(e.target.value)}
                className="w-full rounded border border-matrix-border bg-matrix-bg px-2 py-1 text-xs text-slate-200" />
              <div className="flex items-center gap-2">
                <label className="text-xs text-slate-400">New discussion turns</label>
                <input type="number" min={1} value={injectTurns}
                  onChange={(e) => setInjectTurns(Number(e.target.value))}
                  title="How many turns the group talks after your injected message (defaults to the original run's budget)"
                  className="w-24 rounded border border-matrix-border bg-matrix-bg px-2 py-1 text-xs text-slate-200" />
                <span className="text-[10px] text-slate-500">default = original budget</span>
              </div>
            </>)}

            {mutKind === 'continue' && (
              <div className="flex items-center gap-2">
                <label className="text-xs text-slate-400">Add turns</label>
                <input type="number" min={1} value={addBudget}
                  onChange={(e) => setAddBudget(Number(e.target.value))}
                  className="w-24 rounded border border-matrix-border bg-matrix-bg px-2 py-1 text-xs text-slate-200" />
              </div>
            )}

            {mutKind === 'edit_goal' && (<>
              <select value={editPersona} onChange={(e) => setEditPersona(e.target.value)}
                className="w-full rounded border border-matrix-border bg-matrix-panel px-2 py-1 text-xs text-slate-200">
                <option value="">Select persona…</option>
                {castNames.map((n) => <option key={n} value={n}>{n}</option>)}
              </select>
              <textarea placeholder="New goals, one per line" value={editGoals} rows={3}
                onChange={(e) => setEditGoals(e.target.value)}
                className="w-full rounded border border-matrix-border bg-matrix-bg px-2 py-1 text-xs text-slate-200" />
            </>)}

            {mutKind === 'add_persona' && (<>
              <input placeholder="Name" value={addName} onChange={(e) => setAddName(e.target.value)}
                className="w-full rounded border border-matrix-border bg-matrix-bg px-2 py-1 text-xs text-slate-200" />
              <textarea placeholder="Persona description" value={addPersonaText} rows={2}
                onChange={(e) => setAddPersonaText(e.target.value)}
                className="w-full rounded border border-matrix-border bg-matrix-bg px-2 py-1 text-xs text-slate-200" />
              <textarea placeholder="Goals, one per line (optional)" value={addGoals} rows={2}
                onChange={(e) => setAddGoals(e.target.value)}
                className="w-full rounded border border-matrix-border bg-matrix-bg px-2 py-1 text-xs text-slate-200" />
            </>)}

            {mutKind === 'remove_persona' && (
              <select value={removePersona} onChange={(e) => setRemovePersona(e.target.value)}
                className="w-full rounded border border-matrix-border bg-matrix-panel px-2 py-1 text-xs text-slate-200">
                <option value="">Select persona to remove…</option>
                {castNames.map((n) => <option key={n} value={n}>{n}</option>)}
              </select>
            )}

            <div className="flex justify-end">
              <button onClick={() => handleBranch(true)} disabled={branching}
                className="rounded bg-matrix-accent px-3 py-1 text-sm font-semibold text-matrix-bg hover:bg-sky-400 disabled:opacity-40">
                {branching ? 'Branching…' : '⑂ Branch with intervention'}
              </button>
            </div>
          </div>
        )}

        <p className="mt-1 text-[11px] text-slate-500">
          Viewing state as of turn {turn}. Branching forks a new run resuming from here; this run is never modified.
        </p>
      </div>

      <div className="grid flex-1 grid-cols-1 gap-4 overflow-hidden p-4 lg:grid-cols-[300px_1fr]">
        <aside className="overflow-y-auto">
          {loading ? <p className="text-sm text-slate-500">Loading checkpoints…</p>
            : <CastBoard state={state} onSelect={() => {}} />}
        </aside>
        <main className="overflow-hidden rounded-lg border border-matrix-border bg-matrix-panel">
          <ConversationFeed feed={state.feed} agents={state.agents} activeSpeaker={null} thinking={false} />
        </main>
      </div>
    </div>
  )
}
