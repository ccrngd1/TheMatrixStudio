// SPDX-License-Identifier: Apache-2.0
import type { AgentView } from '../types'
import { AvatarBadge } from './AvatarBadge'

interface Props {
  agent: AgentView
  active: boolean
  thinking: boolean
  onClick: () => void
}

export function CastCard({ agent, active, thinking, onClick }: Props) {
  return (
    <button
      onClick={onClick}
      className={`w-full text-left rounded-lg border p-3 transition
        ${active ? 'border-matrix-live bg-matrix-panel shadow-lg shadow-matrix-live/10' : 'border-matrix-border bg-matrix-panel hover:border-matrix-accent'}`}
    >
      <div className="flex items-center gap-3">
        <AvatarBadge name={agent.name} portrait={agent.portrait} ring={active} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate font-semibold text-slate-100">{agent.name}</span>
            {active && (
              <span className="inline-flex items-center gap-1 text-xs text-matrix-live">
                <span className="h-2 w-2 animate-pulse rounded-full bg-matrix-live" />
                {thinking ? 'thinking…' : 'speaking'}
              </span>
            )}
          </div>
          <p className="truncate text-xs text-slate-400" title={agent.persona}>
            {agent.persona || '—'}
          </p>
        </div>
      </div>
      {agent.goals.length > 0 && (
        <p className="mt-2 truncate text-xs text-slate-500" title={agent.goals.join('; ')}>
          🎯 {agent.goals.join('; ')}
        </p>
      )}
      <div className="mt-2 flex justify-between text-[11px] text-slate-500">
        <span>{agent.messageCount} msgs</span>
        <span>
          {agent.tokensIn + agent.tokensOut} tok · ${agent.costUsd.toFixed(4)}
        </span>
      </div>
    </button>
  )
}
