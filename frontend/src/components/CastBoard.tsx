// SPDX-License-Identifier: Apache-2.0
import type { SimState } from '../lib/simState'
import { identityOf, Label, type Stance } from '../ui/primitives'
import { CastCard } from './CastCard'

interface Props {
  state: SimState
  onSelect: (name: string) => void
  stance?: Record<string, Stance> | null
}

export function CastBoard({ state, onSelect, stance }: Props) {
  return (
    <section className="flex flex-col gap-2">
      <Label as="h2">Personas · {String(state.order.length).padStart(2, '0')}</Label>
      {state.order.map((name) => (
        <CastCard
          key={name}
          agent={state.agents[name]}
          slot={identityOf(name, state.order)}
          active={state.activeSpeaker === name}
          thinking={state.thinking && state.activeSpeaker === name}
          onClick={() => onSelect(name)}
          stance={stance?.[name]}
        />
      ))}
    </section>
  )
}
