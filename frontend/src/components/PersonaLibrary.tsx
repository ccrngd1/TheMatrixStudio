// SPDX-License-Identifier: Apache-2.0
import { useEffect, useState } from 'react'
import { api, type PersonaPack } from '../api'
import { parseCast } from '../lib/importSetup'
import type { DraftPersona } from '../views/newRunTypes'
import { Hint } from './Hint'

interface Props {
  /** Names already in the cast, so an added pack never collides with one. */
  taken: string[]
  onAdd: (persona: DraftPersona) => void
}

// Add a ready-made archetype to the cast. Each pack is a persona someone else wrote and nobody has
// yet measured, so the choice is labelled "not yet qualified" where it is made, not in a footnote.
// Once added it is an ordinary persona in the form and can be edited like any other.
export function PersonaLibrary({ taken, onAdd }: Props) {
  const [packs, setPacks] = useState<PersonaPack[]>([])

  useEffect(() => {
    // Through a promise chain: a deployment without the route shows no library, not a broken form.
    Promise.resolve()
      .then(() => api.listPersonaPacks())
      .then(setPacks)
      .catch(() => setPacks([]))
  }, [])

  if (!packs.length) return null

  const add = (id: string) => {
    const pack = packs.find((p) => p.id === id)
    if (!pack) return
    const [draft] = parseCast([pack.persona]).cast
    if (!draft) return
    let name = draft.name
    for (let n = 2; taken.some((t) => t.trim().toLowerCase() === name.toLowerCase()); n++) {
      name = `${draft.name} ${n}`
    }
    onAdd({ ...draft, name })
  }

  return (
    <span className="flex items-center gap-1">
      <select
        aria-label="Add a persona from the library"
        value=""
        onChange={(e) => add(e.target.value)}
        className="rounded border border-matrix-border bg-matrix-bg px-2 py-1 text-xs"
      >
        <option value="">+ Add from library…</option>
        {packs.map((p) => (
          <option key={p.id} value={p.id} title={p.summary}>
            {p.label} ({p.qualification})
          </option>
        ))}
      </select>
      <Hint label="persona library">
        Ready-made archetypes — a regulator, a finance lead, a devil's advocate — each with positions,
        how firmly they are held, what would change them, and a concern they will not say out loud.
        <br />
        <br />
        <strong>Not yet qualified:</strong> none has been measured in an ensemble to check that it
        holds its positions and moves only on its stated conditions. Edit freely once added.
      </Hint>
    </span>
  )
}
