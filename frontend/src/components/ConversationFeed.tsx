// SPDX-License-Identifier: Apache-2.0
import { useEffect, useRef, useState } from 'react'
import type { AgentView, FeedMessage } from '../types'
import { AvatarBadge } from './AvatarBadge'

interface Props {
  feed: FeedMessage[]
  agents: Record<string, AgentView>
  activeSpeaker: string | null
  thinking: boolean
}

export function ConversationFeed({ feed, agents, activeSpeaker, thinking }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null)
  const [autoScroll, setAutoScroll] = useState(true)

  useEffect(() => {
    if (autoScroll) bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [feed.length, thinking, autoScroll])

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
        {feed.map((m) => (
          <div key={`${m.seq}`} className="flex gap-3">
            <AvatarBadge name={m.speaker} portrait={agents[m.speaker]?.portrait ?? null} size={36} />
            <div className="min-w-0 flex-1">
              <div className="flex items-baseline gap-2">
                <span className="font-semibold text-slate-200">{m.speaker}</span>
                <span className="text-[11px] text-slate-500">turn {m.turn}</span>
              </div>
              <p className="whitespace-pre-wrap text-sm text-slate-300">{m.content}</p>
            </div>
          </div>
        ))}
        {thinking && activeSpeaker && (
          <div className="flex items-center gap-3 text-slate-400">
            <AvatarBadge name={activeSpeaker} portrait={agents[activeSpeaker]?.portrait ?? null} size={36} />
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
