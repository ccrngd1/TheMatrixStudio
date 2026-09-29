// SPDX-License-Identifier: Apache-2.0
import type { SimState } from '../lib/simState'
import { identityOf, Label } from '../ui/primitives'
import { CastCard } from './CastCard'

interface Props {
  state: SimState
  onSelect: (name: string) => void
}

export function CastBoard({ state, onSelect }: Props) {
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
        />
      ))}
    </section>
  )
}
