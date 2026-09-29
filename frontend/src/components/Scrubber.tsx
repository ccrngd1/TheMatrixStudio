// SPDX-License-Identifier: Apache-2.0
import { useEffect, useMemo, useState, type CSSProperties } from 'react'
import { api } from '../api'
import type { Persona, SimEvent } from '../types'
import { deriveState, initialState } from '../lib/simState'
import { Hint } from './Hint'
import { describeFork, forkEstimate } from '../lib/forkCost'
import { ConversationFeed } from './ConversationFeed'
import { Icon } from '../ui/icons'
import { HudCell, HudStrip, Label, Panel, Tag, identityColor, identityOf } from '../ui/primitives'

type Css = CSSProperties & Record<`--${string}`, string>

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
  // Open the new-run form prefilled with this run's setup. Absent = not offered.
  onStartFresh?: () => void
}

// Checkpoint scrubber (Phase 2a+2b): read-only turn slider + Phase 2b intervention panel.
export function Scrubber({ runId, maxTurn, cast, defaultBudget, models = [], defaultModel, onBranch, branching = false, onStartFresh }: Props) {
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
  // Working assumptions (matrix_studio/assumptions.py): which one, and its new value.
  const [assumptionId, setAssumptionId] = useState('')
  const [assumptionText, setAssumptionText] = useState('')
  const [assumptionBasis, setAssumptionBasis] = useState('')

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
  // The assumptions in force AT the selected turn: `state` is replayed from events up to it, so one the
  // moderator made later is not offered, and the server would refuse it anyway.
  const inForce = state.assumptions
  const chosen = inForce.find((a) => a.id === assumptionId) ?? inForce[0]
  const assumptionKind = mutKind === 'replace_assumption' || mutKind === 'withdraw_assumption'
  // A kind that needs an assumption falls back to a plain fork's selector when the turn has none.
  useEffect(() => {
    if (assumptionKind && inForce.length === 0) setMutKind('none')
  }, [assumptionKind, inForce.length])
  const pickAssumption = (id: string) => {
    setAssumptionId(id)
    setAssumptionText(inForce.find((a) => a.id === id)?.statement ?? '')
  }

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
    if (mutKind === 'replace_assumption' && chosen)
      return {
        kind: 'replace_assumption', assumption_id: chosen.id,
        statement: (assumptionText || chosen.statement).trim(),
        ...(assumptionBasis.trim() ? { basis: assumptionBasis.trim() } : {}),
      }
    if (mutKind === 'withdraw_assumption' && chosen)
      return { kind: 'withdraw_assumption', assumption_id: chosen.id }
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
  // Replacing an assumption with itself is a plain fork that claims to be a change.
  const unchangedAssumption =
    mutKind === 'replace_assumption' && (!chosen || !(assumptionText || '').trim() ||
      (assumptionText || '').trim() === chosen.statement)

  // The waveform (docs/MOBILE-UI.md §4.6) reads the WHOLE run, not the state at the cursor: it is the map
  // you move the cursor over. One bar per turn in the speaker's colour, taller where a position moved.
  const whole = useMemo(() => deriveState(initialState(cast), events), [events, cast])
  const order = whole.order
  const bars = useMemo(() => {
    const out: { t: number; speaker: string | null; shift: boolean; injected: boolean }[] = []
    for (let t = 1; t <= maxTurn; t++) {
      const here = whole.feed.filter((m) => m.turn === t && !m.consultant)
      const spoke = here.find((m) => !m.injected)
      out.push({
        t,
        speaker: spoke?.speaker ?? null,
        shift: here.some((m) => m.shift),
        injected: here.some((m) => m.injected),
      })
    }
    return out
  }, [whole, maxTurn])
  const upto = bars.filter((x) => x.t <= turn)
  const shifts = upto.filter((x) => x.shift).length
  const injections = upto.filter((x) => x.injected).length
  const pad = (n: number) => String(n).padStart(2, '0')

  return (
    <div className="cc-scrub">
      <div className="cc-scrub-controls">
        <Panel>
          <div className="flex items-center justify-between gap-2">
            <span className="flex items-center gap-2">
              <Label>Timeline</Label>
              <Tag>read-only</Tag>
            </span>
            <span className="cc-readout" style={{ fontSize: 18 }}>
              #{pad(turn)}
              <small>/{pad(maxTurn)}</small>
              <span className="sr-only">turn {turn} / {maxTurn}</span>
            </span>
          </div>
          <div className="cc-wave" aria-hidden="true">
            {bars.map((x) => {
              const slot = x.speaker ? identityOf(x.speaker, order) : 'a0'
              return (
                <i
                  key={x.t}
                  className={`${x.t <= turn ? 'cc-on' : ''} ${x.t === turn ? 'cc-cur' : ''}`}
                  style={{ '--wc': identityColor(slot), height: `${x.speaker ? (x.shift ? 100 : 45) : 12}%` } as Css}
                  onClick={() => setTurn(x.t)}
                />
              )
            })}
            {bars
              .filter((x) => x.injected)
              .map((x) => (
                <span key={`i${x.t}`} className="cc-inj" style={{ left: `${((x.t - 0.5) * 100) / Math.max(1, maxTurn)}%` }} />
              ))}
          </div>
          <input type="range" min={0} max={maxTurn} value={turn} aria-label="checkpoint turn"
            onChange={(e) => setTurn(Number(e.target.value))}
            className="cc-range" />
          <div className="mt-1 flex items-center justify-between gap-2">
            <div className="cc-legend">
              <span>bar = one turn, speaker colour</span>
              <span className="text-cc-inject">▍ injection</span>
              <span>taller = shift</span>
            </div>
            <span className="flex gap-1.5">
              <button type="button" className="cc-ctl" aria-label="Previous turn" disabled={turn <= 0}
                onClick={() => setTurn((t) => Math.max(0, t - 1))}><Icon name="back" size={16} /></button>
              <button type="button" className="cc-ctl" aria-label="Next turn" disabled={turn >= maxTurn}
                onClick={() => setTurn((t) => Math.min(maxTurn, t + 1))}><Icon name="back" size={16} style={{ transform: 'scaleX(-1)' }} /></button>
            </span>
          </div>
        </Panel>

        <HudStrip>
          {/* The transcript beside this ends at the cursor, and shows the assumptions in force there. */}
          <HudCell label="Turn" value={pad(turn)} />
          <HudCell label="Shifts" value={<span className="text-cc-shift">{pad(shifts)}</span>} sub="so far" />
          <HudCell label="Injections" value={<span className="text-cc-inject">{pad(injections)}</span>} sub="so far" />
        </HudStrip>

        <Panel>
          <div className="flex items-center justify-between gap-2">
            <Label>Fork from #{pad(turn)}</Label>
            <button onClick={handleBranch} disabled={branching || unchangedAssumption}
              aria-description={changing
                ? 'Fork a new run from this turn with your change applied — this run is never modified'
                : 'Fork a new run from this turn, unchanged — this run is never modified'}
              className="cc-btn cc-primary cc-sm">
              <Icon name="branch" size={14} /> {branching ? 'Branching…' : changing ? 'Branch with change' : 'Branch from here'}
            </button>
          </div>
        {/* Always shown. There is no separate "intervene" mode: a branch either
            carries a change or it does not, and hiding the selector behind a second
            button made them look like rival actions. */}
        <div className="mt-2 space-y-2 text-sm">
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
                className="cc-field">
                <option value="none">— none (fork unchanged) —</option>
                <option value="inject_message">💬 Inject message</option>
                <option value="continue">▶ Continue (+N turns)</option>
                <option value="edit_goal">🎯 Edit goal</option>
                <option value="add_persona">➕ Add persona</option>
                <option value="remove_persona">➖ Remove persona</option>
                {inForce.length > 0 && <option value="replace_assumption">≈ Change an assumption</option>}
                {inForce.length > 0 && <option value="withdraw_assumption">≈ Withdraw an assumption</option>}
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
                <strong>Change / withdraw an assumption</strong> — offered when working
                assumptions are in force at this turn. Every later turn reasons from the new
                value, or without it; nothing already said is rewritten. The cleanest test of
                what an assumption was worth.
                <br />
                <strong>Adaptive pressure</strong> — one narrator-voiced world event that
                raises the stakes. Experimental and off unless enabled server-side; it can
                only change the <em>world</em>, never a participant's choices.
              </Hint>
              {models.length > 0 && (
                <>
                  <label className="ml-auto text-xs text-slate-400">Model</label>
                  <select value={branchModel} onChange={(e) => setBranchModel(e.target.value)}
                    aria-label="Model" aria-description="Model the branched discussion generates with (defaults to the page's model)"
                    className="cc-field max-w-[12rem]">
                    {models.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
                  </select>
                </>
              )}
            </div>

            {mutKind === 'inject_message' && (<>
              <input placeholder="Speaker name (can be new, e.g. Moderator)"
                value={injectSpeaker} onChange={(e) => setInjectSpeaker(e.target.value)}
                className="cc-field" />
              <textarea placeholder="Message content…" value={injectContent} rows={3}
                onChange={(e) => setInjectContent(e.target.value)}
                className="cc-field" />
              <div className="flex items-center gap-2">
                <label className="text-xs text-slate-400">New discussion turns</label>
                <input type="number" min={1} value={injectTurns}
                  onChange={(e) => setInjectTurns(Number(e.target.value))}
                  aria-description="How many turns the group talks after your injected message (defaults to the original run's budget)"
                  className="cc-field w-24" />
                <span className="text-[10px] text-slate-500">default = original budget</span>
              </div>
            </>)}

            {mutKind === 'continue' && (
              <div className="flex items-center gap-2">
                <label className="text-xs text-slate-400">Add turns</label>
                <input type="number" min={1} value={addBudget}
                  onChange={(e) => setAddBudget(Number(e.target.value))}
                  className="cc-field w-24" />
              </div>
            )}

            {mutKind === 'edit_goal' && (<>
              <select value={editPersona} onChange={(e) => setEditPersona(e.target.value)}
                className="cc-field">
                <option value="">Select persona…</option>
                {castNames.map((n) => <option key={n} value={n}>{n}</option>)}
              </select>
              <textarea placeholder="New goals, one per line" value={editGoals} rows={3}
                onChange={(e) => setEditGoals(e.target.value)}
                className="cc-field" />
            </>)}

            {mutKind === 'add_persona' && (<>
              <input placeholder="Name" value={addName} onChange={(e) => setAddName(e.target.value)}
                className="cc-field" />
              <textarea placeholder="Persona description" value={addPersonaText} rows={2}
                onChange={(e) => setAddPersonaText(e.target.value)}
                className="cc-field" />
              <textarea placeholder="Goals, one per line (optional)" value={addGoals} rows={2}
                onChange={(e) => setAddGoals(e.target.value)}
                className="cc-field" />
            </>)}

            {mutKind === 'remove_persona' && (
              <select value={removePersona} onChange={(e) => setRemovePersona(e.target.value)}
                className="cc-field">
                <option value="">Select persona to remove…</option>
                {castNames.map((n) => <option key={n} value={n}>{n}</option>)}
              </select>
            )}

            {assumptionKind && chosen && (<>
              <select value={chosen.id} onChange={(e) => pickAssumption(e.target.value)}
                aria-label="Assumption to change"
                className="cc-field">
                {inForce.map((a) => <option key={a.id} value={a.id}>{a.id}: {a.statement}</option>)}
              </select>
              {mutKind === 'replace_assumption' && (<>
                <input value={assumptionText || chosen.statement} maxLength={300}
                  onChange={(e) => setAssumptionText(e.target.value)} aria-label="New value"
                  className="cc-field" />
                <input placeholder="Basis (optional)" value={assumptionBasis} maxLength={300}
                  onChange={(e) => setAssumptionBasis(e.target.value)}
                  className="cc-field" />
              </>)}
              <p className="text-[11px] text-slate-500">
                {chosen.id} was {chosen.turn === 0 && chosen.source === 'operator'
                  ? 'set before the run' : `made at turn ${chosen.turn} by the ${chosen.source}`}.
                Forking later than that keeps what was said under the old value up to turn {turn}.
              </p>
            </>)}

            {mutKind === 'adaptive_pressure' && (<>
              <p className="text-[11px] text-amber-400">
                ⚠ Experimental — must be enabled server-side (ADAPTIVE_PRESSURE_ENABLED=true).
                Injects ONE narrator world event that raises the stakes. Pressure only ever
                modulates the world; output that would negate a participant's choices is rejected.
              </p>
              <input placeholder="Optional focus, e.g. “escalate the audit dilemma”"
                value={pressureFocus} onChange={(e) => setPressureFocus(e.target.value)}
                className="cc-field" />
            </>)}

        </div>
        </Panel>

        {/* A different operation, so it is separated from the branch controls rather
            than sitting beside them: it ignores the selected turn entirely and keeps
            none of the transcript. Grouping it with the mutation kinds would imply it
            is one more variation on "fork from turn N". */}
        {onStartFresh && (
          <div className="cc-card flex flex-wrap items-center gap-2">
            {/* Wrapped rather than passed directly: onClick would hand the click
                event to a callback declared to take none, which would land in any
                parameter added later. */}
            <button onClick={() => onStartFresh()}
              aria-description="Open the new-conversation form filled in with this run's topic, cast, convictions and documents"
              className="whitespace-nowrap rounded border border-matrix-accent/60 px-3 py-1 text-sm font-semibold text-matrix-accent hover:bg-matrix-accent/10">
              ✎ Start over with this setup
            </button>
            <span className="text-xs text-slate-400">Edit everything, then run from turn 0</span>
            <Hint label="starting over with this setup">
              Loads this conversation's <strong>setup</strong> — topic, cast, goals,
              convictions and documents — into the new-conversation form, where all of it
              is editable. Submitting starts a <strong>brand-new</strong> conversation
              from turn 0.
              <br />
              <br />
              Unlike branching, the selected turn is irrelevant and{' '}
              <strong>nothing from the transcript carries over</strong>: no messages, no
              memories, no relationships. Use it when the thing you want to change is the
              premise — a persona's convictions, who is in the room, the question itself,
              or the background material — rather than what happened at some point in the
              discussion.
              <br />
              <br />
              The original run is not modified, and the new one is not recorded as a
              branch of it.
            </Hint>
          </div>
        )}

        <p className="cc-muted">
          Viewing state as of turn {turn}. Branching always forks a NEW run that replays to
          here and then generates forward — this run is never modified. A branch from turn {turn}
          costs {describeFork(forkEstimate(events, turn))}. With no change it
          asks “what else might have happened from here?”; with one, “what if this had been
          different?”
        </p>
      </div>

      <div className="cc-scrub-feed">
        {loading ? (
          <p className="cc-empty">Loading checkpoints…</p>
        ) : (
          <ConversationFeed feed={state.feed} agents={state.agents} activeSpeaker={null} thinking={false}
            assumptions={state.assumptions} />
        )}
      </div>
    </div>
  )
}
