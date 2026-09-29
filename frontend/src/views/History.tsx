// SPDX-License-Identifier: Apache-2.0
import { useEffect, useRef, useState } from 'react'
import { api, type EnsembleSummary } from '../api'
import type { RunSummary } from '../types'
import { isStalled } from '../lib/runStatus'
import { Icon } from '../ui/icons'
import {
  Btn, Chip, Hex, HudCell, HudStrip, Panel, PanelButton, STANCE_COLOR, Tag, Ticks, identityOf, type RunState,
} from '../ui/primitives'
import { StanceCounts } from '../components/run/Stance'

// How long a load may take before the view says why it is still waiting.
//
// The deployed API is a container-image Lambda: warm requests answer in ~56 ms, but a
// cold start costs ~5.7 s of init, and when init overruns Lambda's hard 10 s limit it is
// aborted and RETRIED — one measured cold start took 24.8 s. It returned 200; the run
// list was simply not on screen for 25 seconds, which reads as a broken app. Naming the
// cause is the difference between waiting and reloading.
const SLOW_AFTER_MS = 3000

interface Props {
  onOpen: (runId: string) => void
  /** Optional: in the shell the New-run button is the floating action on this screen. */
  onNew?: () => void
  /** Optional so existing tests that render History alone keep working; the button is
   *  simply absent without it rather than rendering a control that does nothing. */
  onKnowledgeBases?: () => void
  /** Optional for the same reason. Without it the ensembles section is not rendered at
   *  all — a list of ensembles nobody can open would be worse than no list. */
  onOpenEnsemble?: (ensembleId: string) => void
}

export function History({ onOpen, onNew, onKnowledgeBases, onOpenEnsemble }: Props) {
  const [runs, setRuns] = useState<RunSummary[]>([])
  // Ensembles are listed ABOVE the runs rather than mixed into them. They are a different
  // kind of thing — no transcript, no turn count — and interleaving them by date would put a
  // row with no turns and no cost in a list whose columns are turns and cost. Their member
  // runs are nested under them, not repeated in the individual list.
  const [ensembles, setEnsembles] = useState<EnsembleSummary[]>([])
  const [q, setQ] = useState('')
  const [branchesOnly, setBranchesOnly] = useState(false)
  const [loading, setLoading] = useState(true)
  const [slow, setSlow] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = (query?: string) => {
    setLoading(true)
    setSlow(false)
    const slowTimer = setTimeout(() => setSlow(true), SLOW_AFTER_MS)
    api
      .listRuns(query)
      .then((rows) => {
        setRuns(rows)
        setError(null)
      })
      .catch((err) => {
        // A failure used to be swallowed into an empty list, which renders as "No runs
        // yet" — indistinguishable from an account with no runs. On a cold start that
        // times out, that tells the user their history is gone when it is not.
        setRuns([])
        setError(err instanceof Error ? err.message : String(err))
      })
      .finally(() => {
        clearTimeout(slowTimer)
        setLoading(false)
        setSlow(false)
      })
  }

  // One fetch on mount, then debounced fetches as the query changes.
  //
  // This used to be two effects, and both fired on mount: an immediate `load()` and the
  // debounced one 250 ms later with an identical query. Two identical requests is
  // ordinarily just waste, but against a cold Lambda each one starts its OWN sandbox and
  // pays its own ~5.7 s init — the deployed logs show three concurrent cold starts for
  // one page load. The ref keeps the first load immediate while removing the duplicate.
  const firstLoad = useRef(true)
  useEffect(() => {
    if (firstLoad.current) {
      firstLoad.current = false
      load()
      return
    }
    const id = setTimeout(() => load(q || undefined), 250)
    return () => clearTimeout(id)
  }, [q])

  // Ensembles load once and are not searched. Separate from `load` on purpose: the run list
  // must not depend on this request succeeding, because an older deployment has no
  // `/api/ensembles` route and folding the two together would make a 404 there empty the run
  // list. A failure here leaves the section absent, which is the honest degradation.
  //
  // The dependency is a BOOLEAN, not the callback. `onOpenEnsemble` is an inline arrow in
  // `App.tsx`, so it is a new reference on every render — with it in this array the effect tore
  // itself down on each one, setting `live = false` on the in-flight request and starting
  // another. The runs list arriving is itself a re-render, so the first ensembles response was
  // discarded, and against a cold Lambda the section could never appear at all. Measured: three
  // fetches for two re-renders, and a list that did not render.
  const canOpenEnsembles = Boolean(onOpenEnsemble)
  useEffect(() => {
    if (!canOpenEnsembles) return
    let live = true
    api
      .listEnsembles()
      .then((rows) => {
        if (live) setEnsembles(rows)
      })
      .catch(() => {
        if (live) setEnsembles([])
      })
    return () => {
      live = false
    }
  }, [canOpenEnsembles])

  const [openSections, setOpenSections] = useState(readSections)
  const toggleSection = (id: SectionId) =>
    setOpenSections((prev) => {
      const next = { ...prev, [id]: !prev[id] }
      try {
        localStorage.setItem(SECTIONS_KEY, JSON.stringify(next))
      } catch {
        // Storage can be unavailable (private mode, quota). Folding still works for this visit.
      }
      return next
    })
  // Each ensemble starts folded: its members are replicates, and the point of nesting them is
  // that they stay out of the way until asked for.
  const [openEnsembles, setOpenEnsembles] = useState<Set<string>>(new Set())
  const toggleEnsemble = (id: string) =>
    setOpenEnsembles((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  // A member is nested only under an ensemble that is actually LISTED. If the ensembles request
  // failed, or the ensemble is gone, its runs stay in the individual list — hiding them under a
  // parent that is not on screen would make them unreachable.
  const listed = new Set(ensembles.map((e) => e.ensemble_id))
  const membersOf = new Map<string, RunSummary[]>()
  const individual: RunSummary[] = []
  for (const r of runs) {
    if (onOpenEnsemble && r.ensemble_id && listed.has(r.ensemble_id)) {
      const list = membersOf.get(r.ensemble_id) ?? []
      list.push(r)
      membersOf.set(r.ensemble_id, list)
    } else {
      individual.push(r)
    }
  }
  // Grouped by cell, then oldest first — the order the ensemble page lists them in.
  for (const list of membersOf.values()) {
    list.sort(
      (a, b) =>
        (a.ensemble_cell ?? '').localeCompare(b.ensemble_cell ?? '') ||
        (a.created_at ?? 0) - (b.created_at ?? 0),
    )
  }

  // §4.1: live runs first, then everything that has stopped; branches are a filter, not a section.
  const shown = branchesOnly ? individual.filter((r) => r.parent_run_id) : individual
  const liveRuns = shown.filter((r) => isLiveStatus(r))
  const doneRuns = shown.filter((r) => !isLiveStatus(r))
  const weekAgo = Date.now() / 1000 - 7 * 86400
  const spend7d = runs.filter((r) => (r.created_at ?? 0) >= weekAgo).reduce((n, r) => n + (r.total_cost_usd ?? 0), 0)
  const liveCount = runs.filter((r) => isLiveStatus(r)).length
  const branchCount = individual.filter((r) => r.parent_run_id).length

  return (
    <div className="flex flex-col gap-2.5">
      <HudStrip className="cc-stats">
        <HudCell label="Live now" value={pad(liveCount)} sub={liveCount ? 'streaming' : 'none running'} />
        <HudCell label="Finished" value={pad(runs.filter((r) => r.status === 'complete').length)} sub="complete" />
        <HudCell label="Spend · 7 days" value={`$${spend7d.toFixed(2)}`} sub={`${runs.length} runs listed`} />
      </HudStrip>

      <div className="cc-searchbox">
        <Icon name="search" />
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search by name, description, or topic…"
          aria-label="Search runs"
        />
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {onNew && (
          <Btn variant="primary" size="sm" onClick={onNew}>
            <Icon name="plus" /> New run
          </Btn>
        )}
        {onKnowledgeBases && (
          <Btn size="sm" onClick={onKnowledgeBases}>
            <Icon name="knowledge" /> Knowledge bases
          </Btn>
        )}
        {branchCount > 0 && (
          <Chip on={branchesOnly} onClick={() => setBranchesOnly((b) => !b)} aria-pressed={branchesOnly}>
            <Icon name="branch" size={13} /> Branches only ({branchCount})
          </Chip>
        )}
      </div>

      {onOpenEnsemble && ensembles.length > 0 && (
        <Section
          id="ensembles"
          title="Ensembles"
          count={ensembles.length}
          open={openSections.ensembles}
          onToggle={() => toggleSection('ensembles')}
        >
          <div className="cc-list">
            {ensembles.map((e) => {
              const planned = (e.spec ?? []).reduce((n, c) => n + (c.n ?? 0), 0)
              const members = membersOf.get(e.ensemble_id) ?? []
              const expanded = openEnsembles.has(e.ensemble_id)
              return (
                <Panel key={e.ensemble_id} edge="ensemble">
                  <div className="flex items-start gap-2">
                    <button
                      type="button"
                      onClick={() => onOpenEnsemble(e.ensemble_id)}
                      className="min-w-0 flex-1 text-left"
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="cc-code">{e.name ?? e.ensemble_id.slice(0, 8)}</span>
                        <Tag tone="ens">
                          {planned} run{planned === 1 ? '' : 's'}
                          {(e.spec ?? []).length > 1 ? `, ${e.spec.length} groups` : ''}
                        </Tag>
                        {/* Three states, not two. "No report yet" and "a report was refused" are
                            different, and only one of them is worth opening to retry. */}
                        {e.report_error ? (
                          <Tag tone="danger">report failed</Tag>
                        ) : e.has_report ? (
                          <Tag tone="ok">report ready</Tag>
                        ) : (
                          <Tag>{e.status ?? 'pending'}</Tag>
                        )}
                      </div>
                      <p className="cc-topic">{e.description ?? e.topic}</p>
                      {e.report_cost_usd != null && (
                        <p className="cc-meta">${e.report_cost_usd.toFixed(4)} report</p>
                      )}
                    </button>
                    {/* Its own control, not part of the card: the card opens the ensemble, and
                        folding a list open should not navigate away from it. */}
                    <button
                      type="button"
                      onClick={() => toggleEnsemble(e.ensemble_id)}
                      aria-expanded={expanded}
                      aria-label={`${expanded ? 'Hide' : 'Show'} the conversations in ${e.name ?? e.ensemble_id}`}
                      disabled={members.length === 0}
                      className="cc-icon disabled:opacity-30"
                    >
                      <span className="cc-caret" style={{ transform: expanded ? 'rotate(90deg)' : undefined }}>
                        ▸
                      </span>
                    </button>
                  </div>
                  {expanded && members.length > 0 && (
                    <div className="mt-2 flex flex-col gap-2">
                      {members.map((r) => (
                        <RunCard key={r.run_id} run={r} onOpen={onOpen} cell={r.ensemble_cell} />
                      ))}
                    </div>
                  )}
                </Panel>
              )
            })}
          </div>
        </Section>
      )}

      {loading ? (
        <div className="cc-empty">
          <p>Loading…</p>
          {slow && (
            <p className="mt-2">
              Still waiting on the API. The first request after an idle period starts a
              new Lambda sandbox and can take up to 30 seconds; it is not stuck.
            </p>
          )}
        </div>
      ) : error ? (
        <Panel>
          <p className="text-sm" style={{ color: 'var(--danger)' }}>Could not load your runs: {error}</p>
          <Btn size="sm" className="mt-3" onClick={() => load(q || undefined)}>
            Try again
          </Btn>
        </Panel>
      ) : runs.length === 0 ? (
        <p className="cc-empty">No runs yet. Start one with “New run”.</p>
      ) : (
        <>
          {liveRuns.length > 0 && (
            <Section id="live" title="Live now" count={liveRuns.length} open={openSections.live} onToggle={() => toggleSection('live')}>
              <div className="cc-list">
                {liveRuns.map((r) => (
                  <RunCard key={r.run_id} run={r} onOpen={onOpen} />
                ))}
              </div>
            </Section>
          )}
          <Section
            id="finished"
            title="Finished & stopped"
            count={doneRuns.length}
            open={openSections.finished}
            onToggle={() => toggleSection('finished')}
          >
            {doneRuns.length === 0 ? (
              <p className="cc-empty">
                {q
                  ? 'No individual conversation matches.'
                  : branchesOnly
                    ? 'No finished branches.'
                    : liveRuns.length
                      ? 'Nothing has finished yet.'
                      : 'Every conversation here belongs to an ensemble above.'}
              </p>
            ) : (
              <div className="cc-list">
                {doneRuns.map((r) => (
                  <RunCard key={r.run_id} run={r} onOpen={onOpen} />
                ))}
              </div>
            )}
          </Section>
        </>
      )}
    </div>
  )
}

// Which sections are folded is remembered across visits: a user who closes the individual list
// to work with ensembles should not reopen it on every return to this page.
const SECTIONS_KEY = 'matrix-studio.history.sections'
type SectionId = 'ensembles' | 'live' | 'finished'

function readSections(): Record<SectionId, boolean> {
  const all = { ensembles: true, live: true, finished: true }
  try {
    const saved = JSON.parse(localStorage.getItem(SECTIONS_KEY) ?? '{}')
    return { ...all, ...(saved && typeof saved === 'object' ? saved : {}) }
  } catch {
    return all
  }
}

function Section({
  id,
  title,
  count,
  open,
  onToggle,
  children,
}: {
  id: SectionId
  title: string
  count: number
  open: boolean
  onToggle: () => void
  children: React.ReactNode
}) {
  return (
    <section className="cc-sec">
      <h2>
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={open}
          aria-controls={`history-${id}`}
          className="cc-label flex min-h-[32px] items-center gap-1.5"
        >
          <span>{title}</span>
          <span className="cc-num">({count})</span>
          <span className="cc-caret" aria-hidden style={{ transform: open ? 'rotate(90deg)' : undefined }}>
            ▸
          </span>
        </button>
      </h2>
      {open && <div id={`history-${id}`}>{children}</div>}
    </section>
  )
}

function RunCard({
  run: r,
  onOpen,
  cell,
}: {
  run: RunSummary
  onOpen: (runId: string) => void
  /** The ensemble group the run was created under; shown only when nested. */
  cell?: string | null
}) {
  const stalled = isStalled(r.status, r.last_event_at, r.created_at, STALL_SECONDS)
  const live = r.status === 'running' && !stalled
  const cast = r.cast_names ?? []
  return (
    <PanelButton edge={edgeOf(r.status, stalled)} live={live} onClick={() => onOpen(r.run_id)}>
      <div className="flex flex-wrap items-center gap-2">
        {cell && <span className="cc-label">{cell}</span>}
        <span className="cc-code">{r.name ?? r.run_id.slice(0, 8)}</span>
        {/* `lastEventAt` and `createdAt` were never passed, so the staleness branch could not fire:
            a run orphaned by a restart rendered as a healthy "running" for ever. */}
        <StatusTag run={r} stalled={stalled} />
        {r.parent_run_id && (
          <Tag tone="ens">
            <Icon name="branch" size={12} /> branch @ {r.branch_turn}
          </Tag>
        )}
      </div>
      <p className="cc-topic">{r.description ?? r.topic}</p>
      {!cell && r.description && <p className="cc-muted truncate">{r.topic}</p>}
      {live && r.max_messages ? <Ticks n={r.turn_count} max={r.max_messages} live /> : null}
      <div className="cc-meta flex items-center justify-between gap-2">
        <span className="flex items-center gap-[3px]">
          {/* Ringed by end stance once the run is summarised (§6.1); the counts beside say it in glyphs. */}
          {cast.slice(0, 8).map((name) => (
            <Hex key={name} name={name} slot={identityOf(name, cast)} size="xs"
              ring={r.stance?.[name] ? STANCE_COLOR[r.stance[name]] : undefined} />
          ))}
          {r.stance && (
            <span className="ml-1.5">
              <StanceCounts stance={r.stance} among={cast} />
            </span>
          )}
        </span>
        <span className="cc-num">
          {r.turn_count} turns · ${(r.total_cost_usd ?? 0).toFixed(4)}
        </span>
      </div>
    </PanelButton>
  )
}

// A run marked "running" whose most recent event is older than this is treated
// as stalled/orphaned (server up, but no live stream and no recent activity).
// Generous so a slow multi-agent turn is never mislabelled.
const STALL_SECONDS = 120

function StatusTag({ run: r, stalled }: { run: RunSummary; stalled: boolean }) {
  // `pending` counts as stalled too, and that is the more important half: a run stuck at `pending` is one
  // whose execution never started (`start_execution` returning None, a denied StartExecution), which is a
  // real and silent failure mode that would otherwise read as "about to begin" for ever.
  if (stalled) {
    return (
      <Tag tone="warn">
        ⚠ Stalled
        <span className="sr-only">
          {r.status === 'pending'
            ? ' — created but never started generating; its execution may have failed to start'
            : ' — marked running but no recent events; likely orphaned mid-run'}
        </span>
      </Tag>
    )
  }
  switch (r.status) {
    case 'running':
      return (
        <Tag tone="live" pulse>
          Live {pad(r.turn_count)}
          {r.max_messages ? `/${pad(r.max_messages)}` : ''}
        </Tag>
      )
    case 'complete':
      return <Tag tone="ok">✓ Complete</Tag>
    case 'stopped':
      return <Tag tone="warn">■ Stopped</Tag>
    case 'capped':
      return <Tag tone="danger">$ Capped</Tag>
    case 'failed':
      return <Tag tone="danger">✕ Failed</Tag>
    case 'interrupted':
      return <Tag tone="warn">Interrupted</Tag>
    default:
      return <Tag>{r.status ? r.status[0].toUpperCase() + r.status.slice(1) : 'Unknown'}</Tag>
  }
}

function edgeOf(status: string, stalled: boolean): RunState | undefined {
  if (stalled) return 'stopped'
  if (status === 'running' || status === 'stopping') return 'running'
  if (status === 'complete') return 'complete'
  if (status === 'stopped' || status === 'interrupted' || status === 'pending') return 'stopped'
  if (status === 'capped' || status === 'failed') return 'capped'
  return undefined
}

const isLiveStatus = (r: RunSummary) =>
  (r.status === 'running' || r.status === 'pending' || r.status === 'stopping') &&
  !isStalled(r.status, r.last_event_at, r.created_at, STALL_SECONDS)

const pad = (n: number) => String(n).padStart(2, '0')
