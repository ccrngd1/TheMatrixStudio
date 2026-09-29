// SPDX-License-Identifier: Apache-2.0
import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { assumptionUsage, type Usage } from '../lib/assumptionUsage'
import type { AgentView, FeedMessage, PositionShift, Quote, SourcePassage, WorkingAssumption } from '../types'
import { SourceViewer } from './SourceViewer'
import { citeSegments, unsourcedCitations } from '../lib/citeText'
import { Hint } from './Hint'
import { Hex, identityColor, identityOf } from '../ui/primitives'
import { phase } from '../ui/theme'

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
  /** Open a speaker's dossier from their token. */
  onOpenDossier?: (name: string) => void
}

const pad = (n: number) => String(n).padStart(2, '0')
type Css = CSSProperties & Record<`--${string}`, string>

export function ConversationFeed({
  feed, agents, activeSpeaker, thinking, jumpTo, runId, sourceIndex = {}, quotes = {}, assumptions = [],
  onForkAssumption, forkCost, onOpenDossier,
}: Props) {
  // Identity slots in the cast's own order, so a persona keeps one colour on every surface.
  const castOrder = useMemo(() => Object.keys(agents), [agents])
  const usage = useMemo(() => assumptionUsage(assumptions.map((a) => a.id), feed), [assumptions, feed])
  const card = (a: WorkingAssumption) => (
    <AssumptionCard key={a.id} a={a} onFork={onForkAssumption} cost={forkCost?.(a.turn)} usage={usage[a.id]} />
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
    <div className="cc-pane">
      {openSource && runId && (
        <SourceViewer
          runId={runId}
          documentId={openSource.document_id}
          ordinal={openSource.ordinal}
          onClose={() => setOpenSource(null)}
        />
      )}
      <div className="cc-scroll cc-feed">
        {feed.length === 0 && !thinking && (
          <p className="cc-empty">Waiting for the conversation to begin…</p>
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
          const slot = identityOf(m.speaker, castOrder)
          return (
            <div key={`${m.seq}`} className="flex flex-col gap-3">
              {madeHere.map(card)}
              {opensRound && (
                <div className="cc-legend flex items-center gap-2">
                  <span className="h-px flex-1 bg-matrix-border" />
                  round {m.turn} · {roundSize} spoke at once
                  <span className="h-px flex-1 bg-matrix-border" />
                </div>
              )}
              {m.injected ? (
                // Put in by the operator: a hatched banner, so it can never be read as a persona speaking.
                <div
                  id={`turn-${m.seq}`}
                  className={`cc-signal ${highlight === m.seq ? 'cc-jumped' : ''}`}
                  style={{ '--ph': phase() } as Css}
                >
                  <div className="cc-sig-h">
                    <span>
                      ▼ Incoming · <span>injected</span>
                    </span>
                    <span>#{pad(m.turn)}</span>
                  </div>
                  <div className="cc-sig-b">
                    <b>{m.speaker}</b>: {m.content}
                  </div>
                </div>
              ) : (
                <div
                  id={`turn-${m.seq}`}
                  className={`cc-msg ${m.consultant ? 'cc-consult' : ''} ${highlight === m.seq ? 'cc-jumped' : ''}`}
                  style={{ '--c': identityColor(m.consultant ? 'a0' : slot) } as Css}
                >
                  {onOpenDossier && !m.consultant && agents[m.speaker] ? (
                    <button type="button" onClick={() => onOpenDossier(m.speaker)} aria-label={`${m.speaker}: open dossier`}>
                      <Hex name={m.speaker} slot={slot} size="sm" ring={identityColor(slot)} />
                    </button>
                  ) : (
                    <Hex name={m.consultant ? undefined : m.speaker} slot={slot} size="sm" />
                  )}
                  <div className="cc-tx">
                    <div className="cc-tx-h">
                      <b>{m.speaker}</b>
                      <span className="cc-role">
                        {m.consultant ? 'consultant' : roundSize > 1 && !opensRound ? '· at the same time' : ''}
                      </span>
                      {!m.consultant && <span className="cc-t">#{pad(m.turn)}</span>}
                    </div>
                    {m.consultant && (
                      <p className="cc-muted italic">
                        {m.consultant.askedBy} asked: “{m.consultant.question}”
                      </p>
                    )}
                    <MessageBody
                      m={m}
                      index={sourceIndex}
                      quotes={quotes[String(m.seq)]}
                      onOpen={runId ? setOpenSource : undefined}
                    />
                    {m.shift && <ShiftFlag s={m.shift} speaker={m.speaker} />}
                  </div>
                </div>
              )}
            </div>
          )
        })}
        {/* Made after the last message so far — or before any message, which is every operator one. */}
        {assumptions
          .filter((a) => feed.length === 0 || a.turn >= feed[feed.length - 1].turn)
          .map(card)}
        {thinking && activeSpeaker && (
          <div className="cc-composing" role="status">
            <Hex name={activeSpeaker} slot={identityOf(activeSpeaker, castOrder)} size="sm" active />
            <span>{activeSpeaker} composing</span>
            <span className="cc-eq" aria-hidden="true" style={{ '--ph': phase() } as Css}>
              {['0s', '.25s', '.5s', '.125s'].map((d) => (
                <i key={d} style={{ '--d': d } as Css} />
              ))}
            </span>
          </div>
        )}
        <div ref={bottomRef} />
      </div>
      <label className="cc-legend flex items-center justify-end gap-2 px-[14px] pb-1">
        <input
          type="checkbox"
          checked={autoScroll}
          onChange={(e) => setAutoScroll(e.target.checked)}
        />
        auto-scroll
      </label>
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
      <p className="cc-tx-b whitespace-pre-wrap">
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
              aria-description={
                s.mark?.kind === 'unverified'
                  ? `Cited as if read, but ${m.speaker} was not given this source` +
                    (s.mark.reason ? ` (${s.mark.reason})` : '') + '. Open it to check.'
                  : s.own
                    ? `From ${m.speaker}'s own sources. Open it to check the claim.`
                    : `Surfaced by ${s.mark?.via ?? 'another participant'}. Open it to check.`
              }
            >
              {s.cite}
              {s.mark?.kind === 'unverified' && <span aria-hidden="true">⚠</span>}
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
                aria-description={`${q.content_words} content words appear in this order in the passage`}
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
                aria-description={cited.has(label) ? 'Cited in this message' : 'In view, not cited'}
              >
                {cited.has(label) && <span aria-hidden="true">✓ </span>}
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
  usage,
}: {
  a: WorkingAssumption
  onFork?: (a: WorkingAssumption, statement: string | null) => Promise<void>
  cost?: string
  usage?: Usage
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
    <div className="cc-flag cc-assume flex-wrap">
      <span className="cc-ft">ASSUMES</span>
      <span className="min-w-0 flex-1">
        <b>{a.id}</b> {a.statement}
        <span className="cc-muted">
          {' '}— {origin}
          {a.basis ? `; basis: ${a.basis}` : ''}
        </span>
        {usage && (usage.cited > 0 || usage.disputes.length > 0) && (
          <span className={`cc-num ml-1 ${usage.disputes.length ? 'text-cc-inject' : 'cc-muted'}`}>
            · cited in {usage.cited} message{usage.cited === 1 ? '' : 's'}
            {usage.disputes.length > 0 &&
              ` · appears disputed by ${[...new Set(usage.disputes.map((d) => d.speaker))].join(', ')}`}
          </span>
        )}
        <Hint label={`assumption ${a.id}`}>
          Not an established fact: the room was told to reason from it.
          {usage && usage.disputes.length > 0 && (
            <ul className="mt-2 list-inside list-disc">
              {usage.disputes.map((d, i) => (
                <li key={i}>
                  {d.speaker} (turn {d.turn}): {d.sentence}
                </li>
              ))}
            </ul>
          )}
        </Hint>
      </span>
      {onFork && !editing && (
        <button type="button" onClick={() => setEditing(true)} className="cc-btn cc-sm">
          Fork with a different assumption
        </button>
      )}
      {onFork && editing && (
        <div className="mt-2 flex w-full flex-wrap items-center gap-2">
          <input
            value={value}
            onChange={(e) => setValue(e.target.value)}
            maxLength={300}
            aria-label={`New value for ${a.id}`}
            className="cc-field min-w-0 flex-1"
          />
          <button
            type="button"
            onClick={() => fork(value.trim())}
            disabled={busy || !value.trim() || value.trim() === a.statement}
            className="cc-btn cc-primary cc-sm"
          >
            {busy ? 'Forking…' : `Fork from turn ${a.turn}`}
          </button>
          <button type="button" onClick={() => fork(null)} disabled={busy} className="cc-btn cc-danger cc-sm">
            Withdraw it instead
          </button>
          <button type="button" onClick={() => setEditing(false)} disabled={busy} className="cc-btn cc-sm">
            Cancel
          </button>
          <span className="cc-muted w-full">
            A new run replays this one to turn {a.turn} and continues with {a.id} changed; this run is not
            touched. Everything after turn {a.turn} is generated again, so the later the assumption, the
            cheaper the fork.{cost ? ` Cost: ${cost}.` : ''}
          </span>
          {error && <span className="w-full text-cc-danger">{error}</span>}
        </div>
      )}
    </div>
  )
}

// A persona said its position moved: what it credited, and what it had said would move it. Flag-only — a
// word match, so it asks the reader to check rather than telling them it was a fold.
function ShiftFlag({ s, speaker }: { s: PositionShift; speaker: string }) {
  const credits = s.credits.length
    ? s.credits.map((c) => `${c.name} (${c.kind})`).join(', ')
    : 'nobody named'
  return (
    <div className="cc-flag cc-shift">
      <span className="cc-ft">SHIFT</span>
      <span className="min-w-0 flex-1">
      <b>⚑ {speaker} says their position moved</b> — credits {credits}.
      {s.conditions.length > 0 && (
        <>
          {' '}
          {s.no_listed_condition ? (
            <b>None of the conditions {speaker} said would move them appears to be named: </b>
          ) : (
            <span>Appears to name a stated condition: </span>
          )}
          <span className="italic">
            {(s.no_listed_condition ? s.conditions.map((c) => c.condition) : s.matched_conditions).join('; ')}
          </span>
          .
        </>
      )}
      <Hint label="shift flags">
        Found by matching words, not by judgement. Read the message and decide.
      </Hint>
      </span>
    </div>
  )
}
