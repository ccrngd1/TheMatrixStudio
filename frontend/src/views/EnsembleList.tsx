// SPDX-License-Identifier: Apache-2.0
// The Ensembles tab (docs/MOBILE-UI.md §3): every ensemble as a card, newest first. An ensemble has no transcript
// of its own; its artefact is the comparison across its members, so the card leads with that state.
import { useEffect, useState } from 'react'
import { api, type EnsembleSummary } from '../api'
import { Icon } from '../ui/icons'
import { Btn, PanelButton, Tag } from '../ui/primitives'
import { cached, remember } from '../lib/listCache'

export function EnsembleList({ onOpen, onNew }: { onOpen: (id: string) => void; onNew: () => void }) {
  // Seeded from the last visit and refreshed (lib/listCache.ts), shared with the Runs screen's copy.
  const [rows, setRows] = useState<EnsembleSummary[] | null>(() => cached<EnsembleSummary[]>('ensembles') ?? null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    api
      .listEnsembles()
      .then((r) => live && setRows(remember('ensembles', r)))
      .catch((e) => live && cached('ensembles') === undefined && setError(e instanceof Error ? e.message : String(e)))
    return () => {
      live = false
    }
  }, [])

  return (
    <div className="flex flex-col gap-2.5">
      <div>
        <Btn variant="primary" size="sm" onClick={onNew}>
          <Icon name="plus" /> New ensemble
        </Btn>
      </div>
      {error ? (
        <p className="cc-empty">Could not load ensembles: {error}</p>
      ) : rows === null ? (
        <p className="cc-empty">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="cc-empty">
          No ensembles yet. An ensemble runs one brief several times and counts each conclusion per group.
        </p>
      ) : (
        <div className="cc-list">
          {rows.map((e) => {
            const planned = (e.spec ?? []).reduce((n, c) => n + (c.n ?? 0), 0)
            const groups = e.spec ?? []
            return (
              <PanelButton key={e.ensemble_id} edge="ensemble" onClick={() => onOpen(e.ensemble_id)}>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="cc-code">{e.name ?? e.ensemble_id.slice(0, 8)}</span>
                  {e.report_error ? (
                    <Tag tone="danger">report failed</Tag>
                  ) : e.has_report ? (
                    <Tag tone="ok">report ready</Tag>
                  ) : (
                    <Tag tone="ens">{e.status ?? 'pending'}</Tag>
                  )}
                </div>
                <p className="cc-topic">{e.description ?? e.topic}</p>
                <p className="cc-meta">
                  {planned} run{planned === 1 ? '' : 's'}
                  {groups.length > 1 ? ` · ${groups.map((g) => `${g.label} ×${g.n}`).join(' vs ')}` : ''}
                  {e.report_cost_usd != null ? ` · $${e.report_cost_usd.toFixed(4)} report` : ''}
                </p>
              </PanelButton>
            )
          })}
        </div>
      )}
    </div>
  )
}
