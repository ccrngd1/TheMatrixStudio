// SPDX-License-Identifier: Apache-2.0
// What surrounds one message, opened by tapping it in the feed: why this speaker was picked, what came before
// and after, the assumptions in force, what was in front of them, what it cost, and (with cognition) their
// own account of why they said it.
//
// Everything here is read from what the run already recorded — the event log and the per-turn trace — and
// the drawer says so when a piece was not recorded rather than leaving a blank that reads as "nothing".
import { useEffect, useState } from 'react'
import { api } from '../../api'
import type { FeedMessage, SimEvent, TurnTrace, WorkingAssumption } from '../../types'
import { Btn, Hex, HudCell, HudStrip, Label, Panel, identityOf } from '../../ui/primitives'

const pad = (n: number) => String(n).padStart(2, '0')
const clip = (s: string, n = 180) => (s.length > n ? `${s.slice(0, n).trimEnd()}…` : s)

interface Props {
  message: FeedMessage
  feed: FeedMessage[]
  events: SimEvent[]
  assumptions: WorkingAssumption[]
  order: string[]
  runId: string
  onOpenDossier: (name: string) => void
  onJump: (seq: number) => void
  onScrubFrom?: (turn: number) => void
}

export function MessageContext({ message: m, feed, events, assumptions, order, runId, onOpenDossier, onJump, onScrubFrom }: Props) {
  const [trace, setTrace] = useState<TurnTrace | null>(null)
  const [traceState, setTraceState] = useState<'idle' | 'loading' | 'error'>('idle')
  useEffect(() => {
    setTrace(null)
    setTraceState('idle')
  }, [m.seq])

  const speaking = feed.filter((x) => !x.consultant && !x.injected)
  const i = speaking.findIndex((x) => x.seq === m.seq)
  const before = i > 0 ? speaking[i - 1] : null
  const after = i >= 0 && i < speaking.length - 1 ? speaking[i + 1] : null
  const round = speaking.filter((x) => x.turn === m.turn).length

  const payload = (e?: SimEvent) => ((e?.payload ?? {}) as Record<string, unknown>)
  const selected = events.find(
    (e) => e.event_type === 'speaker.selected' && e.turn === m.turn && payload(e).speaker === m.speaker,
  )
  const reason = payload(selected).reason as string | undefined
  const method = payload(selected).method as string | undefined
  const response = events.find((e) => e.event_type === 'agent.response' && e.seq === m.seq)
  const cost = payload(response).cost_usd as number | undefined
  const tin = payload(response).tokens_in as number | undefined
  const tout = payload(response).tokens_out as number | undefined
  const inForce = assumptions.filter((a) => a.turn <= m.turn)

  const why = async () => {
    setTraceState('loading')
    try {
      setTrace(await api.getTurnTrace(runId, m.turn))
      setTraceState('idle')
    } catch {
      setTraceState('error')
    }
  }

  const Neighbour = ({ x, label }: { x: FeedMessage; label: string }) => (
    <button type="button" className="cc-card w-full text-left" onClick={() => onJump(x.seq)}>
      <Label>
        {label} · #{pad(x.turn)} {x.speaker}
      </Label>
      <p className="cc-muted mt-1">{clip(x.content)}</p>
    </button>
  )

  return (
    <div className="flex flex-col gap-2.5">
      <div className="flex items-center gap-3">
        <Hex name={m.speaker} slot={identityOf(m.speaker, order)} />
        <div className="min-w-0 flex-1">
          <b className="cc-code block">{m.speaker}</b>
          <span className="cc-muted">
            Turn {pad(m.turn)}
            {round > 1 ? ` · one of ${round} who spoke at once` : ''}
            {m.consultant ? ' · consultant, answering when asked' : ''}
          </span>
        </div>
      </div>

      {m.consultant ? (
        <Panel>
          <Label>Asked by {m.consultant.askedBy}</Label>
          <p className="mt-1">“{m.consultant.question}”</p>
        </Panel>
      ) : (
        <Panel>
          <Label>Why them, now</Label>
          <p className="mt-1">
            {reason ? (
              <>“{reason}”</>
            ) : method && method !== 'moderated' ? (
              <span className="cc-muted">Chosen by the {method} method, which gives no reason.</span>
            ) : (
              <span className="cc-muted">
                No reason was recorded for this pick. The moderator gives one only when cognition is on.
              </span>
            )}
          </p>
        </Panel>
      )}

      {before && <Neighbour x={before} label="Came after" />}
      {after && <Neighbour x={after} label="Followed by" />}

      {m.shift && (
        <div className="cc-flag cc-shift">
          <span className="cc-ft">SHIFT</span>
          <span>
            Said their position moved; credits{' '}
            {m.shift.credits.length ? m.shift.credits.map((c) => c.name).join(', ') : 'nobody named'}.
            {m.shift.no_listed_condition && <b> None of the conditions they had named appears.</b>}
          </span>
        </div>
      )}

      {inForce.length > 0 && (
        <Panel>
          <Label>Assumptions in force</Label>
          {inForce.map((a) => (
            <div key={a.id} className="cc-flag cc-assume">
              <span className="cc-ft">ASSUMES</span>
              <span>
                <b>{a.id}</b> {a.statement}
              </span>
            </div>
          ))}
        </Panel>
      )}

      <Panel>
        <Label>In front of them</Label>
        {m.sources && m.sources.length > 0 ? (
          <ul className="mt-1 list-inside list-disc">
            {m.sources.map((p) => (
              <li key={p.chunk_id}>
                {p.title} #{p.ordinal}
              </li>
            ))}
          </ul>
        ) : (
          <p className="cc-muted mt-1">No passages were retrieved for this message.</p>
        )}
        {(m.citations?.length ?? 0) > 0 && (
          <p className="cc-muted mt-1">Cites {m.citations!.map((c) => c.label).join(', ')}.</p>
        )}
      </Panel>

      {cost !== undefined && (
        <HudStrip>
          <HudCell label="Cost" value={`$${cost.toFixed(4)}`} />
          <HudCell label="In" value={(tin ?? 0).toLocaleString()} sub="tokens" />
          <HudCell label="Out" value={(tout ?? 0).toLocaleString()} sub="tokens" />
        </HudStrip>
      )}

      {!m.consultant && (
        <Panel>
          <Label>In their words</Label>
          {trace ? (
            trace.available ? (
              <div className="mt-1 flex flex-col gap-1.5">
                {trace.rationale && <p>“{trace.rationale}”</p>}
                {trace.goal_served && <p className="cc-muted">Goal served: {trace.goal_served}</p>}
                {(trace.memories?.length ?? 0) > 0 && (
                  <>
                    <p className="cc-muted">Drawing on:</p>
                    <ul className="list-inside list-disc">
                      {trace.memories!.map((x) => (
                        <li key={x.id}>{clip(x.content, 140)}</li>
                      ))}
                    </ul>
                  </>
                )}
              </div>
            ) : (
              <p className="cc-muted mt-1">Not recorded: this run had cognition off, so there is no why-trace.</p>
            )
          ) : (
            <div className="mt-1.5">
              <Btn size="sm" onClick={why} disabled={traceState === 'loading'}>
                {traceState === 'loading' ? 'Loading…' : 'Why did they say that?'}
              </Btn>
              {traceState === 'error' && <p className="mt-1 text-cc-danger">Could not load the trace.</p>}
            </div>
          )}
        </Panel>
      )}

      <div className="flex flex-wrap gap-2">
        {!m.consultant && order.includes(m.speaker) && (
          <Btn size="sm" onClick={() => onOpenDossier(m.speaker)}>
            Open {m.speaker.split(' ')[0]}'s dossier
          </Btn>
        )}
        {onScrubFrom && (
          <Btn size="sm" onClick={() => onScrubFrom(m.turn)}>
            Branch from turn {pad(m.turn)}
          </Btn>
        )}
      </div>
    </div>
  )
}
