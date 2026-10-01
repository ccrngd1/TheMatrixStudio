// SPDX-License-Identifier: Apache-2.0
// The Library tab (docs/MOBILE-UI.md §3): the ready-made persona archetypes and the casts you have saved. Both
// used to be reachable only from inside the new-run form; here they can be browsed before starting anything,
// and each one starts a run from itself rather than leaving you to find it again in the wizard.
import { useEffect, useState } from 'react'
import { api, type CastTemplateSummary, type PersonaPack } from '../api'
import type { Route } from '../lib/route'
import { Icon } from '../ui/icons'
import { Btn, Hex, Label, Panel, Tag, identityOf } from '../ui/primitives'

// Both open the wizard on the Cast step (2), where what was loaded is shown and can be edited.
export function Library({ onNavigate }: { onNavigate: (r: Route) => void }) {
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
                <div className="mt-2">
                  <Btn onClick={() => onNavigate({ name: 'new', step: 2, castTemplate: c.name })}>
                    <Icon name="play" /> Start a run with this cast
                    {/* Every card has this button; the name tells them apart for a screen reader. */}
                    <span className="sr-only">: {c.name}</span>
                  </Btn>
                </div>
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
                <div className="mt-2">
                  <Btn onClick={() => onNavigate({ name: 'new', step: 2, packId: p.id })}>
                    <Icon name="plus" /> Add to a new run
                    <span className="sr-only">: {p.label}</span>
                  </Btn>
                </div>
              </Panel>
            ))}
          </div>
        )}
      </section>

      <div>
        {/* Says "blank" because it is: starting from a saved cast or an archetype is on its card above. */}
        <Btn variant="primary" onClick={() => onNavigate({ name: 'new' })}>
          <Icon name="plus" /> Start a blank run
        </Btn>
      </div>
    </div>
  )
}
