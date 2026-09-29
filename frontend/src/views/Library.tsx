// SPDX-License-Identifier: Apache-2.0
// The Library tab (docs/MOBILE-UI.md §3): the ready-made persona archetypes and the casts you have saved. Both
// used to be reachable only from inside the new-run form; here they can be browsed before starting anything.
import { useEffect, useState } from 'react'
import { api, type CastTemplateSummary, type PersonaPack } from '../api'
import { Icon } from '../ui/icons'
import { Btn, Hex, Label, Panel, Tag, identityOf } from '../ui/primitives'

export function Library({ onNewRun }: { onNewRun: () => void }) {
  const [packs, setPacks] = useState<PersonaPack[] | null>(null)
  const [casts, setCasts] = useState<CastTemplateSummary[] | null>(null)

  useEffect(() => {
    let live = true
    // Through promise chains, so a deployment without one of the routes shows that list empty rather than
    // failing the whole tab.
    Promise.resolve().then(() => api.listPersonaPacks()).then((p) => live && setPacks(p)).catch(() => live && setPacks([]))
    Promise.resolve().then(() => api.listCastTemplates()).then((c) => live && setCasts(c)).catch(() => live && setCasts([]))
    return () => {
      live = false
    }
  }, [])

  return (
    <div className="flex flex-col gap-3">
      <section className="cc-sec">
        <Label as="h2">Saved casts</Label>
        {casts === null ? (
          <p className="cc-empty">Loading…</p>
        ) : casts.length === 0 ? (
          <p className="cc-muted">
            None saved yet. Save a cast from the new-run wizard's Cast step to reuse it here.
          </p>
        ) : (
          <div className="cc-list">
            {casts.map((c) => (
              <Panel key={c.name}>
                <div className="flex items-center justify-between gap-2">
                  <b>{c.name}</b>
                  <span className="flex gap-[3px]">
                    {c.personas.slice(0, 8).map((n) => (
                      <Hex key={n} name={n} slot={identityOf(n, c.personas)} size="xs" />
                    ))}
                  </span>
                </div>
                {c.description && <p className="cc-topic">{c.description}</p>}
                <p className="cc-meta">{c.personas.join(' · ')}</p>
              </Panel>
            ))}
          </div>
        )}
      </section>

      <section className="cc-sec">
        <Label as="h2">Persona archetypes</Label>
        {packs === null ? (
          <p className="cc-empty">Loading…</p>
        ) : packs.length === 0 ? (
          <p className="cc-muted">No archetypes on this deployment.</p>
        ) : (
          <div className="cc-list">
            {packs.map((p, i) => (
              <Panel key={p.id}>
                <div className="flex items-center gap-3">
                  <Hex name={p.label} slot={identityOf(p.label, packs.map((x) => x.label)) || `a${(i % 6) + 1}`} />
                  <div className="min-w-0 flex-1">
                    <b className="block">{p.label}</b>
                    {/* Shipped unqualified by design: an archetype nobody has checked in an ensemble says so. */}
                    <Tag tone="warn">{p.qualification}</Tag>
                  </div>
                </div>
                <p className="cc-topic">{p.summary}</p>
              </Panel>
            ))}
          </div>
        )}
      </section>

      <div>
        <Btn variant="primary" size="sm" onClick={onNewRun}>
          <Icon name="plus" /> Start a run with these
        </Btn>
      </div>
    </div>
  )
}
