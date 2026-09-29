// SPDX-License-Identifier: Apache-2.0
import { useEffect, useRef, useState } from 'react'
import type { AgentView, FeedMessage, Quote, SourcePassage, WorkingAssumption } from '../types'
import { AvatarBadge } from './AvatarBadge'
import { SourceViewer } from './SourceViewer'
import { citeSegments, unsourcedCitations } from '../lib/citeText'

interface Props {
  feed: FeedMessage[]
  agents: Record<string, AgentView>
  activeSpeaker: string | null
  thinking: boolean
  /** Scroll this message into view and flag it briefly. Sent by the participation panel. */
  jumpTo?: { seq: number; nonce: number } | null
  /** Needed to open a cited source. Without it citations render as plain text. */
  runId?: string
  /** Every passage the run retrieved, by label — so a citation of another persona's source opens. */
  sourceIndex?: Record<string, SourcePassage>
  /** Verbatim quotes of in-view passages, by message seq. */
  quotes?: Record<string, Quote[]>
  /** Working assumptions, each shown where it was made (turn 0: above the first message). */
  assumptions?: WorkingAssumption[]
  /** Fork the run at the assumption's turn with it replaced (a statement) or withdrawn (null). */
  onForkAssumption?: (a: WorkingAssumption, statement: string | null) => Promise<void>
  /** What a fork at a turn would cost, in words (`lib/forkCost.ts`). */
  forkCost?: (turn: number) => string
}

export function ConversationFeed({
  feed, agents, activeSpeaker, thinking, jumpTo, runId, sourceIndex = {}, quotes = {}, assumptions = [],
  onForkAssumption, forkCost,
}: Props) {
  const card = (a: WorkingAssumption) => (
    <AssumptionCard key={a.id} a={a} onFork={onForkAssumption} cost={forkCost?.(a.turn)} />
  )
  // A claim is checked where it is read. The source viewer was reachable only from a persona's
  // dossier, so checking a sentence meant knowing who said it, opening their panel and finding the
  // turn; the message now links to the passage it drew on.
  const [openSource, setOpenSource] = useState<SourcePassage | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const [autoScroll, setAutoScroll] = useState(true)
  const [highlight, setHighlight] = useState<number | null>(null)

  useEffect(() => {
    if (autoScroll) bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [feed.length, thinking, autoScroll])

  // The jump carries a `nonce` so clicking the same turn twice scrolls twice — the seq
  // alone would not change and the effect would not re-run.
  //
  // Auto-scroll is switched OFF on a jump rather than fought with: on a live run the next
  // turn would otherwise yank the view back to the bottom, which reads as the click having
  // done nothing.
  useEffect(() => {
    if (!jumpTo) return
    setAutoScroll(false)
    setHighlight(jumpTo.seq)
    document
      .getElementById(`turn-${jumpTo.seq}`)
      ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    const timer = setTimeout(() => setHighlight(null), 2000)
    return () => clearTimeout(timer)
  }, [jumpTo])

  return (
    <div className="flex h-full flex-col">
      {openSource && runId && (
        <SourceViewer
          runId={runId}
          documentId={openSource.document_id}
          ordinal={openSource.ordinal}
          onClose={() => setOpenSource(null)}
        />
      )}
      <div className="flex items-center justify-between border-b border-matrix-border px-4 py-2">
        <h2 className="text-sm font-semibold text-slate-300">Conversation</h2>
        <label className="flex items-center gap-1 text-xs text-slate-400">
          <input
            type="checkbox"
            checked={autoScroll}
            onChange={(e) => setAutoScroll(e.target.checked)}
          />
          auto-scroll
        </label>
      </div>
      <div className="flex-1 space-y-3 overflow-y-auto p-4">
        {feed.length === 0 && !thinking && (
          <p className="text-sm text-slate-500">Waiting for the conversation to begin…</p>
        )}
        {feed.map((m, i) => {
          // The simultaneous method puts every survivor of a round on ONE turn number.
          // Rendered as a flat list they read as a sequence — as though the second speaker
          // had heard the first — which is precisely what did not happen. So a turn with
          // more than one message is drawn as a round: a divider naming it, and every
          // message after the first marked "at the same time".
          // Consultants' answers share their question's turn but are not part of any round.
          const roundSize = feed.filter((x) => x.turn === m.turn && !x.consultant && !x.injected).length
          const opensRound = roundSize > 1 && feed[i - 1]?.turn !== m.turn
          // An assumption made at turn t is shown before the first message of a later turn.
          const prevTurn = i === 0 ? -Infinity : feed[i - 1].turn
          const madeHere = assumptions.filter((a) => a.turn < m.turn && a.turn >= prevTurn)
          return (
            <div key={`${m.seq}`} className="space-y-3">
              {madeHere.map(card)}
              {opensRound && (
                <div className="flex items-center gap-2 pt-1 text-[11px] text-slate-500">
                  <span className="h-px flex-1 bg-matrix-border" />
                  round {m.turn} · {roundSize} spoke at once
                  <span className="h-px flex-1 bg-matrix-border" />
                </div>
              )}
              <div
                id={`turn-${m.seq}`}
                className={`flex gap-3 rounded transition-colors ${
                  m.consultant ? 'ml-10 border-l-2 border-amber-500/40 pl-3 ' : ''
                }${
                  highlight === m.seq ? 'bg-matrix-accent/10 ring-1 ring-matrix-accent/60' : ''
                }`}
              >
                <AvatarBadge name={m.speaker} portrait={agents[m.speaker]?.portrait ?? null}
                  portraitUrl={agents[m.speaker]?.portraitUrl ?? null} size={36} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline gap-2">
                    <span className="font-semibold text-slate-200">{m.speaker}</span>
                    {m.injected && (
                      <span
                        className="rounded bg-fuchsia-900/40 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-fuchsia-200"
                        title="Put into the conversation by the operator, not generated"
                      >
                        injected
                      </span>
                    )}
                    {m.consultant ? (
                      <span
                        className="rounded bg-amber-900/40 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-amber-200"
                        title="Not a participant: answers only from its own sources, when asked"
                      >
                        consultant
                      </span>
                    ) : (
                      <span className="text-[11px] text-slate-500">turn {m.turn}</span>
                    )}
                    {roundSize > 1 && !opensRound && (
                      <span className="text-[11px] text-slate-600">· at the same time</span>
                    )}
                  </div>
                  {m.consultant && (
                    <p className="text-[11px] italic text-slate-400">
                      {m.consultant.askedBy} asked: “{m.consultant.question}”
                    </p>
                  )}
                  <MessageBody
                    m={m}
                    index={sourceIndex}
                    quotes={quotes[String(m.seq)]}
                    onOpen={runId ? setOpenSource : undefined}
                  />
                </div>
              </div>
            </div>
          )
        })}
        {/* Made after the last message so far — or before any message, which is every operator one. */}
        {assumptions
          .filter((a) => feed.length === 0 || a.turn >= feed[feed.length - 1].turn)
          .map(card)}
        {thinking && activeSpeaker && (
          <div className="flex items-center gap-3 text-slate-400">
            <AvatarBadge name={activeSpeaker} portrait={agents[activeSpeaker]?.portrait ?? null}
              portraitUrl={agents[activeSpeaker]?.portraitUrl ?? null} size={36} />
            <span className="text-sm italic">
              {activeSpeaker} is thinking
              <span className="animate-pulse">…</span>
            </span>
          </div>
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}

function MessageBody({
  m, index, quotes, onOpen,
}: {
  m: FeedMessage
  index: Record<string, SourcePassage>
  quotes?: Quote[]
  onOpen?: (p: SourcePassage) => void
}) {
  const segments = onOpen ? citeSegments(m.content, m.sources, index, m.citations) : [{ text: m.content }]
  const cited = new Set(
    segments.flatMap((s) => ('passage' in s ? [`${s.passage.title} #${s.passage.ordinal}`] : [])),
  )
  const unsourced = unsourcedCitations(m.citations, index)
  return (
    <>
      <p className="whitespace-pre-wrap text-sm text-slate-300">
        {segments.map((s, i) =>
          'passage' in s ? (
            <button
              key={i}
              onClick={() => onOpen?.(s.passage)}
              className={
                s.mark?.kind === 'unverified'
                  ? 'text-amber-300 underline decoration-dotted hover:text-amber-200'
                  : 'text-matrix-accent underline decoration-dotted hover:text-sky-300'
              }
              title={
                s.mark?.kind === 'unverified'
                  ? `Cited as if read, but ${m.speaker} was not given this source` +
                    (s.mark.reason ? ` (${s.mark.reason})` : '') + '. Open it to check.'
                  : s.own
                    ? `From ${m.speaker}'s own sources. Open it to check the claim.`
                    : `Surfaced by ${s.mark?.via ?? 'another participant'}. Open it to check.`
              }
            >
              {s.cite}
            </button>
          ) : (
            <span key={i}>{s.text}</span>
          ),
        )}
      </p>
      {/* Verbatim evidence of which passage was used, for a message that did not say. Quotation
          only — a best guess among passages on the same subject could not tell them apart. */}
      {onOpen && quotes && quotes.length > 0 && (
        <p className="mt-1 text-[11px] text-slate-400">
          Quotes{' '}
          {quotes.map((q, i) => (
            <span key={q.chunk_id}>
              {i > 0 && '; '}
              <button
                onClick={() => onOpen(q)}
                className="text-matrix-accent hover:underline"
                title={`${q.content_words} content words appear in this order in the passage`}
              >
                {q.title} #{q.ordinal}
              </button>
              : <span className="italic text-slate-300">“{q.phrase}”</span>
            </span>
          ))}
        </p>
      )}
      {unsourced.length > 0 && (
        <p className="mt-1 text-[11px] text-amber-300/90">
          Cites {unsourced.map((c) => c.label).join(', ')} — no one in this conversation retrieved
          {unsourced.length === 1 ? ' it' : ' them'}.
        </p>
      )}
      {/* What was in front of the speaker, cited or not: a claim with no citation can still be
          checked against what the persona had been given. */}
      {onOpen && m.sources && m.sources.length > 0 && (
        <p className="mt-1 flex flex-wrap gap-x-2 text-[11px] text-slate-500">
          <span>Sources in view:</span>
          {m.sources.map((p) => {
            const label = `${p.title} #${p.ordinal}`
            return (
              <button
                key={p.chunk_id}
                onClick={() => onOpen(p)}
                className={`hover:text-matrix-accent hover:underline ${
                  cited.has(label) ? 'text-slate-300' : ''
                }`}
                title={cited.has(label) ? 'Cited in this message' : 'In view, not cited'}
              >
                {label}
              </button>
            )
          })}
        </p>
      )}
    </>
  )
}

function AssumptionCard({
  a,
  onFork,
  cost,
}: {
  a: WorkingAssumption
  onFork?: (a: WorkingAssumption, statement: string | null) => Promise<void>
  cost?: string
}) {
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState(a.statement)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const fork = async (statement: string | null) => {
    if (!onFork) return
    setBusy(true)
    setError(null)
    try {
      await onFork(a, statement)
    } catch (e) {
      setError((e as Error).message)
      setBusy(false)
    }
  }
  const origin = a.replaces
    ? `replaced at turn ${a.turn} (was: ${a.replaces})`
    : a.source === 'operator' && a.turn === 0
      ? 'set before the run'
      : `made at turn ${a.turn} by the ${a.source}`
  return (
    <div
      className="rounded border border-dashed border-sky-500/40 bg-sky-950/20 px-3 py-2 text-xs text-slate-300"
      title="Not an established fact: the room was told to reason from it"
    >
      <span className="mr-2 rounded bg-sky-900/50 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-sky-200">
        assumption {a.id}
      </span>
      {a.statement}
      <span className="ml-1 text-slate-500">
        — {origin}
        {a.basis ? `; basis: ${a.basis}` : ''}
      </span>
      {onFork && !editing && (
        <button
          onClick={() => setEditing(true)}
          className="ml-2 text-sky-300 underline-offset-2 hover:underline"
        >
          Fork with a different assumption
        </button>
      )}
      {onFork && editing && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <input
            value={value}
            onChange={(e) => setValue(e.target.value)}
            maxLength={300}
            aria-label={`New value for ${a.id}`}
            className="min-w-0 flex-1 rounded border border-matrix-border bg-matrix-bg p-1.5 text-xs"
          />
          <button
            onClick={() => fork(value.trim())}
            disabled={busy || !value.trim() || value.trim() === a.statement}
            className="rounded bg-sky-800/60 px-2 py-1 text-sky-100 disabled:opacity-40"
          >
            {busy ? 'Forking…' : `Fork from turn ${a.turn}`}
          </button>
          <button onClick={() => fork(null)} disabled={busy} className="text-slate-400 hover:text-rose-300">
            Withdraw it instead
          </button>
          <button onClick={() => setEditing(false)} disabled={busy} className="text-slate-500">
            Cancel
          </button>
          <span className="w-full text-[11px] text-slate-500">
            A new run replays this one to turn {a.turn} and continues with {a.id} changed; this run is not
            touched. Everything after turn {a.turn} is generated again, so the later the assumption, the
            cheaper the fork.{cost ? ` Cost: ${cost}.` : ''}
          </span>
          {error && <span className="w-full text-rose-300">{error}</span>}
        </div>
      )}
    </div>
  )
}
