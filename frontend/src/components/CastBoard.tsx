// SPDX-License-Identifier: Apache-2.0
import type { SimState } from '../lib/simState'
import { CastCard } from './CastCard'

interface Props {
  state: SimState
  onSelect: (name: string) => void
}

export function CastBoard({ state, onSelect }: Props) {
  return (
    <div className="space-y-2">
      <h2 className="px-1 text-sm font-semibold text-slate-300">Cast</h2>
      <div className="grid grid-cols-1 gap-2">
        {state.order.map((name) => (
          <CastCard
            key={name}
            agent={state.agents[name]}
            active={state.activeSpeaker === name}
            thinking={state.thinking && state.activeSpeaker === name}
            onClick={() => onSelect(name)}
          />
        ))}
      </div>
    </div>
  )
}
