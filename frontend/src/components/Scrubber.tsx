// SPDX-License-Identifier: Apache-2.0
import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import type { Persona, SimEvent } from '../types'
import { deriveState, initialState } from '../lib/simState'
import { Hint } from './Hint'
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
  // 'none' means a plain fork: same state, no change applied. It is the default
  // because forking as-is is the safe, common action, and because the previous UI
  // presented "Branch" and "Intervene" as two sibling ACTIONS when they are one
  // operation — a fork, optionally with one change. That framing was the confusion.
  const [mutKind, setMutKind] = useState<string>('none')
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
  // Phase 4c (experimental): optional operator direction for adaptive pressure.
  const [pressureFocus, setPressureFocus] = useState('')

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
    if (mutKind === 'none') return undefined
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
    if (mutKind === 'adaptive_pressure') {
      const m: Record<string, unknown> = { kind: 'adaptive_pressure' }
      if (pressureFocus.trim()) m.focus = pressureFocus.trim()
      return m
    }
    return undefined
  }

  // One action, whether or not a change is attached. `buildMutation()` returns
  // undefined for 'none', which the caller already treats as a plain fork.
  const handleBranch = () => onBranch(turn, buildMutation(), branchModel || undefined)
  const changing = mutKind !== 'none'

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
          <button onClick={handleBranch} disabled={branching}
            title={changing
              ? 'Fork a new run from this turn with your change applied — this run is never modified'
              : 'Fork a new run from this turn, unchanged — this run is never modified'}
            className="whitespace-nowrap rounded bg-matrix-accent px-3 py-1 text-sm font-semibold text-matrix-bg hover:bg-sky-400 disabled:opacity-40">
            {branching ? 'Branching…' : changing ? '⑂ Branch with change' : '⑂ Branch from here'}
          </button>
        </div>

        {/* Always shown. There is no separate "intervene" mode: a branch either
            carries a change or it does not, and hiding the selector behind a second
            button made them look like rival actions. */}
        <div className="mt-3 rounded border border-matrix-border bg-matrix-bg p-3 text-sm space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              {/* htmlFor/id rather than a wrapping label: the select sits outside the
                  label so the Hint can follow the text, and without the association it
                  had NO accessible name at all — a screen reader announced an unlabelled
                  combobox. Caught by a test looking it up by name. */}
              <label htmlFor="scrubber-change-kind" className="flex items-center gap-2 text-xs text-slate-400">
                Change at this turn
                <Hint label="change at this turn">
                  Branching always forks a <strong>new</strong> run: it replays this one up
                  to the selected turn, then generates forward. The original is never
                  touched.
                  <br />
                  <br />
                  With <em>none</em>, the fork starts from identical state — so any
                  difference comes purely from the model, answering “what else might have
                  happened?”. Pick a change and it is applied <strong>once, at the fork</strong>,
                  making it the one variable: “what if this had been different?”
                </Hint>
              </label>
              <select id="scrubber-change-kind" value={mutKind}
                onChange={(e) => setMutKind(e.target.value)}
                className="rounded border border-matrix-border bg-matrix-panel px-2 py-1 text-xs text-slate-200">
                <option value="none">— none (fork unchanged) —</option>
                <option value="inject_message">💬 Inject message</option>
                <option value="continue">▶ Continue (+N turns)</option>
                <option value="edit_goal">🎯 Edit goal</option>
                <option value="add_persona">➕ Add persona</option>
                <option value="remove_persona">➖ Remove persona</option>
                <option value="adaptive_pressure">🌩 Adaptive pressure (experimental)</option>
              </select>
              <Hint label="the change options">
                <strong>Inject message</strong> — put words in someone's mouth as a real
                turn; the speaker can be new (e.g. a moderator or a customer).
                <br />
                <strong>Continue</strong> — no change at all, just more turns. Use when a
                discussion was cut off mid-argument.
                <br />
                <strong>Edit goal</strong> — replace a persona's goals. Goals are
                satisfiable, so this redirects what they will settle for.
                <br />
                <strong>Add / remove persona</strong> — change who is in the room. The
                cleanest test of whether one voice was carrying the outcome.
                <br />
                <strong>Adaptive pressure</strong> — one narrator-voiced world event that
                raises the stakes. Experimental and off unless enabled server-side; it can
                only change the <em>world</em>, never a participant's choices.
              </Hint>
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

            {mutKind === 'adaptive_pressure' && (<>
              <p className="text-[11px] text-amber-400">
                ⚠ Experimental — must be enabled server-side (ADAPTIVE_PRESSURE_ENABLED=true).
                Injects ONE narrator world event that raises the stakes. Pressure only ever
                modulates the world; output that would negate a participant's choices is rejected.
              </p>
              <input placeholder="Optional focus, e.g. “escalate the audit dilemma”"
                value={pressureFocus} onChange={(e) => setPressureFocus(e.target.value)}
                className="w-full rounded border border-matrix-border bg-matrix-bg px-2 py-1 text-xs text-slate-200" />
            </>)}

        </div>

        <p className="mt-1 text-[11px] text-slate-500">
          Viewing state as of turn {turn}. Branching always forks a NEW run that replays to
          here and then generates forward — this run is never modified. With no change it
          asks “what else might have happened from here?”; with one, “what if this had been
          different?”
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
