// SPDX-License-Identifier: Apache-2.0
import type { AgentView, FeedMessage } from '../types'
import { AvatarBadge } from './AvatarBadge'

interface Props {
  agent: AgentView
  feed: FeedMessage[]
  onClose: () => void
}

// Fields the current engine does NOT emit. Per the honesty gate (spec §6) these
// are shown as explicitly deferred, NEVER fabricated.
const DEFERRED_FIELDS = [
  'Memory stream',
  'Reflections & beliefs',
  'Relationships graph',
  'Goal hierarchy',
  '"Why did it say that?" trace',
]

export function Dossier({ agent, feed, onClose }: Props) {
  const messages = feed.filter((m) => m.speaker === agent.name)
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
          {agent.goals.length ? (
            <ul className="list-inside list-disc space-y-1 text-sm text-slate-300">
              {agent.goals.map((g, i) => (
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
                <div key={m.seq} className="rounded border border-matrix-border p-2 text-sm text-slate-300">
                  <span className="mr-2 text-[11px] text-slate-500">turn {m.turn}</span>
                  {m.content}
                </div>
              ))}
            </div>
          ) : (
            <p className="text-sm text-slate-500">This agent hasn't spoken yet.</p>
          )}
        </Section>

        <Section title="Deeper introspection">
          <p className="mb-2 text-xs text-slate-500">
            The current engine does not yet emit this structured per-agent state.
            These views are <span className="text-slate-400">available in a later version</span>.
          </p>
          <ul className="space-y-1 text-sm text-slate-500">
            {DEFERRED_FIELDS.map((f) => (
              <li key={f} className="flex items-center justify-between">
                <span>{f}</span>
                <span className="rounded bg-matrix-border px-2 py-0.5 text-[10px] uppercase tracking-wide">
                  later version
                </span>
              </li>
            ))}
          </ul>
        </Section>
      </div>
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
