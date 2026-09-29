// SPDX-License-Identifier: Apache-2.0
// One persona row on the Cast tab (docs/MOBILE-UI.md §4.3): token, name, what they are for, and their share of
// the run so far. The whole row opens the dossier. The persona text is clamped rather than truncated to a
// tooltip: a tooltip is hover-only, and the dossier one tap away has all of it.
import type { AgentView } from '../types'
import { Hex, PanelButton } from '../ui/primitives'

interface Props {
  agent: AgentView
  active: boolean
  thinking: boolean
  onClick: () => void
  /** The identity slot (`identityOf`), so the row matches the feed. */
  slot?: string
}

export function CastCard({ agent, active, thinking, onClick, slot = 'a0' }: Props) {
  const n = agent.messageCount
  return (
    <PanelButton className="cc-prow" onClick={onClick} aria-label={`${agent.name}: open dossier`}>
      <Hex name={agent.name} slot={slot} active={active} />
      <div className="cc-grow min-w-0">
        <b>{agent.name}</b>
        <div className="cc-muted line-clamp-2">{agent.persona || '—'}</div>
        {agent.goals.length > 0 && <div className="cc-muted line-clamp-1">Goals: {agent.goals.join('; ')}</div>}
        <div className="cc-num mt-[3px] text-[10.5px] text-cc-t3">
          {active && <span className="text-matrix-live">{thinking ? 'COMPOSING · ' : 'SPEAKING · '}</span>}
          {n} TURN{n === 1 ? '' : 'S'} · {(agent.tokensIn + agent.tokensOut).toLocaleString()} TOK · $
          {agent.costUsd.toFixed(4)}
        </div>
      </div>
    </PanelButton>
  )
}
