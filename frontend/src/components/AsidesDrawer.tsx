// SPDX-License-Identifier: Apache-2.0
import { useEffect, useState } from 'react'
import { api } from '../api'
import type { AsideTarget, Persona, ThreadDetail, ThreadSummary } from '../types'

interface Props {
  runId: string
  cast: Persona[]
  // Optional analysis-model override (from the in-thread model picker); sent
  // with each aside message so replies use the chosen model.
  model?: string
  onClose: () => void
}

// Read-only aside conversations over a finished run. Every reply here is
// model-generated analysis living in a private side-thread — it is NOT part of
// the canonical conversation, does not change the run, and other asides don't
// see it. The UI states this plainly (canon boundary) and shows a disabled
// "bring into conversation" affordance reserved for a later version (Phase 2).
export function AsidesDrawer({ runId, cast, model, onClose }: Props) {
  const [threads, setThreads] = useState<ThreadSummary[]>([])
  const [active, setActive] = useState<ThreadDetail | null>(null)
  const [target, setTarget] = useState<AsideTarget>('analyst')
  const [personaName, setPersonaName] = useState<string>(cast[0]?.name ?? '')
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const loadThreads = () =>
    api.listThreads(runId).then(setThreads).catch(() => setThreads([]))

  useEffect(() => {
    loadThreads()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId])

  const openThread = async (id: string) => {
    setError(null)
    const t = await api.getThread(id)
    setActive(t)
  }

  const startThread = async () => {
    setError(null)
    try {
      const t = await api.createThread(
        runId,
        target,
        target === 'persona' ? personaName : undefined,
      )
      await loadThreads()
      await openThread(t.id)
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const send = async () => {
    if (!active || !draft.trim()) return
    setSending(true)
    setError(null)
    const content = draft.trim()
    setDraft('')
    try {
      await api.postThreadMessage(active.id, content, model)
      await openThread(active.id)
      await loadThreads()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSending(false)
    }
  }

  const targetLabel = (t: ThreadSummary) =>
    t.target === 'persona' ? `${t.persona_name}` : t.target === 'room' ? 'The room' : 'Analyst'

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/50" onClick={onClose}>
      <div
        className="flex h-full w-full max-w-lg flex-col border-l border-matrix-border bg-matrix-panel"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex items-center justify-between border-b border-matrix-border p-4">
          <div>
            <h2 className="text-lg font-bold text-slate-100">Asides</h2>
            <p className="text-[11px] text-slate-500">
              Private, read-only side-threads over the finished run.
            </p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-200">
            ✕
          </button>
        </header>

        {/* Canon boundary banner — asides are never part of the conversation. */}
        <div className="border-b border-matrix-border bg-amber-950/20 px-4 py-2 text-[11px] text-amber-300">
          Aside — not part of the conversation. Replies are model-generated analysis and do not
          change the run.
        </div>

        {error && (
          <p className="mx-4 mt-2 rounded bg-red-950/50 p-2 text-xs text-red-300">{error}</p>
        )}

        {!active ? (
          <div className="flex-1 overflow-y-auto p-4">
            <div className="rounded-lg border border-matrix-border p-3">
              <h3 className="mb-2 text-sm font-semibold text-slate-300">New aside</h3>
              <div className="flex flex-wrap items-center gap-2">
                <select
                  value={target}
                  onChange={(e) => setTarget(e.target.value as AsideTarget)}
                  className="rounded border border-matrix-border bg-matrix-bg p-2 text-sm"
                >
                  <option value="analyst">Analyst (about the whole run)</option>
                  <option value="persona">A persona (in character)</option>
                  <option value="room">The room (all personas)</option>
                </select>
                {target === 'persona' && (
                  <select
                    value={personaName}
                    onChange={(e) => setPersonaName(e.target.value)}
                    className="rounded border border-matrix-border bg-matrix-bg p-2 text-sm"
                  >
                    {cast.map((c) => (
                      <option key={c.name} value={c.name}>
                        {c.name}
                      </option>
                    ))}
                  </select>
                )}
                <button
                  onClick={startThread}
                  className="rounded bg-matrix-accent px-3 py-2 text-sm font-semibold text-matrix-bg hover:bg-sky-400"
                >
                  Start
                </button>
              </div>
            </div>

            <h3 className="mb-2 mt-4 text-xs font-semibold uppercase tracking-wide text-slate-400">
              Threads ({threads.length})
            </h3>
            <div className="space-y-2">
              {threads.length === 0 && (
                <p className="text-sm text-slate-500">No asides yet.</p>
              )}
              {threads.map((t) => (
                <button
                  key={t.id}
                  onClick={() => openThread(t.id)}
                  className="flex w-full items-center justify-between rounded border border-matrix-border p-2 text-left text-sm hover:border-matrix-accent"
                >
                  <span className="text-slate-200">{targetLabel(t)}</span>
                  <span className="text-[11px] text-slate-500">
                    {t.message_count} msg · ${t.total_cost_usd.toFixed(4)}
                  </span>
                </button>
              ))}
            </div>
          </div>
        ) : (
          <>
            <div className="flex items-center justify-between border-b border-matrix-border px-4 py-2">
              <button
                onClick={() => {
                  setActive(null)
                  loadThreads()
                }}
                className="text-sm text-slate-400 hover:text-slate-200"
              >
                ← All threads
              </button>
              <span className="text-xs text-slate-500">
                {active.target === 'persona'
                  ? `${active.persona_name} (in character)`
                  : active.target === 'room'
                    ? 'The room'
                    : 'Analyst'}{' '}
                · ${active.total_cost_usd.toFixed(4)}
              </span>
            </div>

            <div className="flex-1 space-y-3 overflow-y-auto p-4">
              {active.messages.length === 0 && (
                <p className="text-sm text-slate-500">Ask the first question below.</p>
              )}
              {active.messages.map((m) => (
                <div
                  key={m.id}
                  className={m.role === 'user' ? 'text-right' : 'text-left'}
                >
                  <div
                    className={`inline-block max-w-[90%] rounded-lg p-2 text-sm ${
                      m.role === 'user'
                        ? 'bg-matrix-accent/20 text-slate-100'
                        : 'border border-matrix-border bg-matrix-bg text-slate-300'
                    }`}
                  >
                    {m.role === 'target' && (
                      <div className="mb-1 text-[10px] uppercase tracking-wide text-slate-500">
                        {m.speaker} · aside
                      </div>
                    )}
                    <p className="whitespace-pre-wrap">{m.content}</p>
                  </div>
                </div>
              ))}
            </div>

            <div className="border-t border-matrix-border p-3">
              {/* Phase 2 hook — non-functional in this version. */}
              <div className="mb-2 flex items-center justify-between">
                <button
                  disabled
                  title="Available in a later version"
                  className="cursor-not-allowed rounded border border-matrix-border px-2 py-1 text-[11px] text-slate-600"
                >
                  ⤴ Bring into conversation
                </button>
                <span className="text-[10px] text-slate-600">available in a later version</span>
              </div>
              <div className="flex gap-2">
                <input
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.shiftKey) {
                      e.preventDefault()
                      send()
                    }
                  }}
                  placeholder="Ask a follow-up…"
                  className="flex-1 rounded border border-matrix-border bg-matrix-bg p-2 text-sm"
                />
                <button
                  onClick={send}
                  disabled={sending || !draft.trim()}
                  className="rounded bg-matrix-accent px-3 py-2 text-sm font-semibold text-matrix-bg hover:bg-sky-400 disabled:opacity-40"
                >
                  {sending ? '…' : 'Send'}
                </button>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
