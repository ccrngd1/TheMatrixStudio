// SPDX-License-Identifier: Apache-2.0
import { useEffect, useRef, useState } from 'react'
import type { AgentView, FeedMessage } from '../types'
import { AvatarBadge } from './AvatarBadge'

interface Props {
  feed: FeedMessage[]
  agents: Record<string, AgentView>
  activeSpeaker: string | null
  thinking: boolean
  /** Scroll this message into view and flag it briefly. Sent by the participation panel. */
  jumpTo?: { seq: number; nonce: number } | null
}

export function ConversationFeed({
  feed, agents, activeSpeaker, thinking, jumpTo,
}: Props) {
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
          const roundSize = feed.filter((x) => x.turn === m.turn).length
          const opensRound = roundSize > 1 && feed[i - 1]?.turn !== m.turn
          return (
            <div key={`${m.seq}`} className="space-y-3">
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
                  highlight === m.seq ? 'bg-matrix-accent/10 ring-1 ring-matrix-accent/60' : ''
                }`}
              >
                <AvatarBadge name={m.speaker} portrait={agents[m.speaker]?.portrait ?? null}
                  portraitUrl={agents[m.speaker]?.portraitUrl ?? null} size={36} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline gap-2">
                    <span className="font-semibold text-slate-200">{m.speaker}</span>
                    <span className="text-[11px] text-slate-500">turn {m.turn}</span>
                    {roundSize > 1 && !opensRound && (
                      <span className="text-[11px] text-slate-600">· at the same time</span>
                    )}
                  </div>
                  <p className="whitespace-pre-wrap text-sm text-slate-300">{m.content}</p>
                </div>
              </div>
            </div>
          )
        })}
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
