// SPDX-License-Identifier: Apache-2.0
import { useEffect, useState } from 'react'
import { api } from '../api'
import type { AgentDossier, AgentView, FeedMessage, TurnTrace } from '../types'
import { AvatarBadge } from './AvatarBadge'

interface Props {
  agent: AgentView
  feed: FeedMessage[]
  runId: string
  onClose: () => void
}

export function Dossier({ agent, feed, runId, onClose }: Props) {
  const messages = feed.filter((m) => m.speaker === agent.name)
  const [dossier, setDossier] = useState<AgentDossier | null>(null)
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    let alive = true
    api
      .getDossier(runId, agent.name)
      .then((d) => alive && setDossier(d))
      .catch(() => undefined)
      .finally(() => alive && setLoaded(true))
    return () => {
      alive = false
    }
  }, [runId, agent.name])

  // Cognition is "captured" only if the engine actually produced any of it.
  const hasCognition =
    !!dossier &&
    (dossier.memory_stream.length > 0 ||
      dossier.beliefs.length > 0 ||
      Object.keys(dossier.relationships).length > 0)

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/50" onClick={onClose}>
      <div
        className="h-full w-full max-w-md overflow-y-auto border-l border-matrix-border bg-matrix-panel p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between">
          <div className="flex items-center gap-3">
            <AvatarBadge name={agent.name} portrait={agent.portrait} size={64} />
            <div>
              <h2 className="text-xl font-bold text-slate-100">{agent.name}</h2>
              <p className="text-xs text-slate-500">
                {agent.avatarResolved
                  ? agent.portrait
                    ? 'portrait generated'
                    : 'placeholder (avatar unavailable)'
                  : 'avatar pending…'}
              </p>
            </div>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-200">
            ✕
          </button>
        </div>

        <Section title="Persona">
          <p className="whitespace-pre-wrap text-sm text-slate-300">{agent.persona || '—'}</p>
        </Section>

        <Section title="Goals">
          {(dossier?.goals ?? agent.goals).length ? (
            <ul className="list-inside list-disc space-y-1 text-sm text-slate-300">
              {(dossier?.goals ?? agent.goals).map((g, i) => (
                <li key={i}>{g}</li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-slate-500">No goals specified.</p>
          )}
        </Section>

        <Section title="Usage">
          <div className="grid grid-cols-3 gap-2 text-center text-sm">
            <Stat label="Messages" value={String(agent.messageCount)} />
            <Stat label="Tokens" value={(agent.tokensIn + agent.tokensOut).toLocaleString()} />
            <Stat label="Cost" value={`$${agent.costUsd.toFixed(4)}`} />
          </div>
          <p className="mt-1 text-[11px] text-slate-500">
            {agent.tokensIn.toLocaleString()} in · {agent.tokensOut.toLocaleString()} out
          </p>
        </Section>

        <Section title={`Messages (${messages.length})`}>
          {messages.length ? (
            <div className="space-y-2">
              {messages.map((m) => (
                <MessageRow key={m.seq} m={m} runId={runId} traceable={hasCognition} />
              ))}
            </div>
          ) : (
            <p className="text-sm text-slate-500">This agent hasn't spoken yet.</p>
          )}
        </Section>

        {/* Phase 2c: real captured cognition when present; an honest
            "not captured for this run" state otherwise — never fabricated. */}
        {hasCognition && dossier ? (
          <>
            <Section title={`Memory stream (${dossier.memory_stream.length})`}>
              <div className="space-y-1">
                {dossier.memory_stream.map((mem) => (
                  <div
                    key={mem.id}
                    className="rounded border border-matrix-border p-2 text-sm text-slate-300"
                  >
                    <div className="flex items-center justify-between text-[11px] text-slate-500">
                      <span>{(mem.tags || []).join(', ') || 'memory'}</span>
                      {mem.importance != null && <span>importance {mem.importance.toFixed(2)}</span>}
                    </div>
                    {mem.content}
                  </div>
                ))}
              </div>
            </Section>

            {dossier.beliefs.length > 0 && (
              <Section title={`Beliefs / reflections (${dossier.beliefs.length})`}>
                <ul className="list-inside list-disc space-y-1 text-sm text-slate-300">
                  {dossier.beliefs.map((b) => (
                    <li key={b.id}>{b.content}</li>
                  ))}
                </ul>
              </Section>
            )}

            {Object.keys(dossier.relationships).length > 0 && (
              <Section title="Relationships">
                <div className="space-y-1">
                  {Object.entries(dossier.relationships).map(([other, stance]) => (
                    <div key={other} className="flex gap-2 text-sm">
                      <span className="font-semibold text-slate-200">{other}:</span>
                      <span className="text-slate-400">{stance}</span>
                    </div>
                  ))}
                </div>
              </Section>
            )}
          </>
        ) : (
          <Section title="Cognition">
            <p className="text-xs text-slate-500">
              {loaded
                ? 'This run was created without cognition, so there is no memory stream, reflections, relationships, or per-turn “why” trace to show. Enable Cognition when creating a run to capture it.'
                : 'Loading…'}
            </p>
          </Section>
        )}
      </div>
    </div>
  )
}

function MessageRow({ m, runId, traceable }: { m: FeedMessage; runId: string; traceable: boolean }) {
  const [open, setOpen] = useState(false)
  const [trace, setTrace] = useState<TurnTrace | null>(null)
  const [loading, setLoading] = useState(false)

  const toggle = async () => {
    const next = !open
    setOpen(next)
    if (next && trace === null && !loading) {
      setLoading(true)
      try {
        setTrace(await api.getTurnTrace(runId, m.turn))
      } catch {
        setTrace({ run_id: runId, turn: m.turn, available: false })
      } finally {
        setLoading(false)
      }
    }
  }

  return (
    <div className="rounded border border-matrix-border p-2 text-sm text-slate-300">
      <div className="flex items-start justify-between gap-2">
        <div>
          <span className="mr-2 text-[11px] text-slate-500">turn {m.turn}</span>
          {m.content}
        </div>
        {traceable && (
          <button
            onClick={toggle}
            className="whitespace-nowrap rounded bg-matrix-accent/15 px-2 py-0.5 text-[11px] text-matrix-accent hover:bg-matrix-accent/25"
            title="Why did it say that?"
          >
            {open ? 'hide' : 'why?'}
          </button>
        )}
      </div>
      {open && (
        <div className="mt-2 border-t border-matrix-border pt-2 text-xs">
          {loading && <p className="text-slate-500">Loading trace…</p>}
          {!loading && trace && !trace.available && (
            <p className="text-slate-500">Trace not available for this turn.</p>
          )}
          {!loading && trace && trace.available && (
            <div className="space-y-1">
              {trace.selection_reason && (
                <Line label="Chosen because" value={trace.selection_reason} />
              )}
              {trace.rationale && <Line label="Rationale" value={trace.rationale} />}
              {trace.goal_served && <Line label="Goal served" value={trace.goal_served} />}
              {trace.memories && trace.memories.length > 0 && (
                <div>
                  <div className="text-[10px] uppercase tracking-wide text-slate-500">
                    Memories in context
                  </div>
                  <ul className="list-inside list-disc text-slate-400">
                    {trace.memories.map((mem) => (
                      <li key={mem.id}>{mem.content}</li>
                    ))}
                  </ul>
                </div>
              )}
              <p className="pt-1 text-[10px] text-slate-600">
                Model-generated introspection captured at generation time.
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function Line({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <span className="text-[10px] uppercase tracking-wide text-slate-500">{label}: </span>
      <span className="text-slate-300">{value}</span>
    </div>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mt-5">
      <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-matrix-accent">{title}</h3>
      {children}
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded border border-matrix-border py-2">
      <div className="font-semibold text-slate-100">{value}</div>
      <div className="text-[10px] uppercase tracking-wide text-slate-500">{label}</div>
    </div>
  )
}
