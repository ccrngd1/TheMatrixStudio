// SPDX-License-Identifier: Apache-2.0
// One ensemble: its groups, its member conversations, and the report over them.
//
// An ensemble is not a run, and this view is deliberately not a run view. It has no
// transcript, no stream and no scrubber — the artefact is the comparison, and each member
// conversation is reachable from here as an ordinary run.
//
// ## Polling watches the report, not readiness
//
// `report_ready` means the aggregator MAY run. The last member to finish flips its own status
// and then generates the report, in that order, so there is a window where every member is
// settled and `has_report` is still false. Polling on readiness would show "done, no report"
// as a resting state. So this polls until `has_report || report_error`, and says "building the
// report" in between — which is true and is the thing the operator is waiting for.
//
// ## Why there is no "generate" button while members are running
//
// The server refuses with 409 and the reason, and offering a button that always fails is
// worse than not offering one. The button appears only once the work could actually succeed,
// or once it has failed and a retry is meaningful.

import { useCallback, useEffect, useRef, useState } from 'react'
import { api, type EnsembleDetail } from '../api'
import { ClaimTable } from '../components/ClaimTable'
import { ConclusionsPanel } from '../components/ConclusionsPanel'
import { ExportMenu } from '../components/ExportMenu'
import { ResearchPanel } from '../components/ResearchPanel'

interface Props {
  ensembleId: string
  onBack: () => void
  onOpenRun: (runId: string) => void
}

/** How often to re-read an ensemble that is still working. */
const POLL_MS = 4000

/**
 * How long a report claim is trusted, matching the server's lease in
 * `storage/dynamo.REPORT_LEASE_SECONDS`.
 *
 * Past it the claimant is presumed dead and the server will let another caller take over, so the
 * view has to stop saying "building" and offer the button again — otherwise a build killed by a
 * timeout leaves the page claiming progress for ever.
 */
const STALE_CLAIM_MS = 20 * 60 * 1000

export function EnsembleView({ ensembleId, onBack, onOpenRun }: Props) {
  const [detail, setDetail] = useState<EnsembleDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [generating, setGenerating] = useState(false)
  // The `report_generated_at` that was on screen when a rebuild was asked for. While it has
  // not changed, the report being displayed is the OLD one — so a forced regenerate must not
  // look finished just because a report exists.
  //
  // A ref, not state: `load` is memoised on `ensembleId` so a state value read inside it would
  // be the one captured at definition time. Re-rendering is driven by `setDetail` regardless.
  const rebuildingRef = useRef<number | null>(null)
  const [rebuilding, setRebuilding] = useState(false)
  // Held in a ref so the polling effect does not restart every time the body changes, which
  // would reset the interval on each tick and poll far faster than POLL_MS.
  const settled = useRef(false)

  const load = useCallback(async () => {
    try {
      const body = await api.getEnsemble(ensembleId)
      setDetail(body)
      setError(null)
      // A rebuild is outstanding until the timestamp MOVES. `has_report` is true throughout,
      // because the previous report is still stored and still served.
      const rebuilt =
        rebuildingRef.current === null || body.report_generated_at !== rebuildingRef.current
      settled.current = rebuilt && (body.has_report || Boolean(body.report_error))
      if (rebuilt) {
        rebuildingRef.current = null
        setRebuilding(false)
      }
      return body
    } catch (e) {
      // Not swallowed into an empty view: "this ensemble has nothing" and "the request
      // failed" look identical otherwise, and the first is a lie.
      setError(e instanceof Error ? e.message : String(e))
      return null
    }
  }, [ensembleId])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    if (settled.current) return
    const timer = setInterval(() => {
      if (settled.current) return
      void load()
    }, POLL_MS)
    return () => clearInterval(timer)
  }, [load, detail?.has_report, detail?.report_error])

  const generate = async (force: boolean) => {
    setGenerating(true)
    const wasGeneratedAt = detail?.report_generated_at ?? null
    try {
      const res = await api.generateEnsembleReport(ensembleId, force)
      if (res.accepted) {
        // 202: the work is running elsewhere. Resume polling — `settled` may be true from an
        // EARLIER report, and without clearing it a forced rebuild would sit on the old one
        // for ever looking complete.
        rebuildingRef.current = wasGeneratedAt
        setRebuilding(true)
        settled.current = false
      }
      await load()
    } catch (e) {
      setError(
        `${e instanceof Error ? e.message : String(e)} — if the report was already accepted it ` +
          'is still being built and will appear here.',
      )
    } finally {
      setGenerating(false)
    }
  }

  if (error && !detail) {
    return (
      <Frame onBack={onBack} title="Ensemble">
        <p className="text-sm text-rose-300">{error}</p>
      </Frame>
    )
  }
  if (!detail) {
    return (
      <Frame onBack={onBack} title="Ensemble">
        <p className="text-sm text-slate-400">Loading…</p>
      </Frame>
    )
  }

  const declared = detail.cells.reduce((n, c) => n + c.declared, 0)
  const settledCount = detail.cells.reduce((n, c) => n + c.settled, 0)

  // A build owns this if it claimed recently. `STALE_CLAIM_MS` matches the server's 20-minute
  // lease: past it the claimant is presumed dead and the claim is reclaimable, so the view must
  // offer the button again rather than saying "building" for ever.
  const claimedAt = detail.report_claimed_at ?? null
  const claimedMsAgo = claimedAt ? Date.now() - claimedAt * 1000 : null
  const buildInFlight = claimedMsAgo !== null && claimedMsAgo < STALE_CLAIM_MS
  const claimedAgo =
    claimedMsAgo === null
      ? 'just now'
      : claimedMsAgo < 60_000
        ? 'less than a minute ago'
        : `${Math.round(claimedMsAgo / 60_000)} minute${
            Math.round(claimedMsAgo / 60_000) === 1 ? '' : 's'
          } ago`
  const cellOrder = detail.spec.map((c) => c.label)
  // Any group present in the report but missing from the spec still has to render — a
  // hand-made ensemble or an older row could have one, and dropping it would silently hide
  // conversations that were paid for.
  const reportCells = detail.report
    ? [
        ...cellOrder.filter((l) => detail.report!.cells.some((c) => c.cell === l)),
        ...detail.report.cells.map((c) => c.cell).filter((l) => !cellOrder.includes(l)),
      ]
    : cellOrder

  return (
    <Frame onBack={onBack} title={detail.name || 'Ensemble'}>
      <p className="mb-4 text-sm text-slate-400">{detail.topic}</p>
      {/* Always available: an ensemble's export says which conversations are still running and
          whether a report exists, so an early export is incomplete and SAYS so. */}
      <div className="mb-4">
        <ExportMenu kind="ensemble" id={ensembleId} name={detail.name || 'ensemble'} />
      </div>

      {/* What was asked for. Rendered from the STORED spec, so a group whose runs all
          failed still appears — that is a result, not an absence. */}
      <section className="mb-6">
        <h2 className="mb-2 text-xs uppercase tracking-wide text-slate-500">Groups</h2>
        <ul className="space-y-1 text-sm">
          {detail.spec.map((cell) => {
            const progress = detail.cells.find((c) => c.cell === cell.label)
            const overrides = Object.entries(cell.overrides ?? {})
            return (
              <li key={cell.label} className="text-slate-300">
                <span className="font-medium">{cell.label}</span> — {cell.n} run
                {cell.n === 1 ? '' : 's'}
                {progress ? ` · ${progress.complete} complete of ${progress.declared}` : ''}
                {overrides.length === 0 ? (
                  <span className="ml-2 text-[11px] text-slate-500">nothing varied</span>
                ) : (
                  <span className="ml-2 text-[11px] text-slate-500">
                    {overrides.map(([k, v]) => `${k}=${String(v)}`).join(', ')}
                  </span>
                )}
              </li>
            )
          })}
        </ul>
      </section>

      {/* PERSONA-RESEARCH.md §6: ONE pass for the whole fan-out, so the panel belongs on the
          parent. Renders nothing for an ensemble that did not research. */}
      {detail.research && (
        <section className="mb-6">
          <ResearchPanel research={detail.research} />
          <p className="mt-2 text-xs text-slate-500">
            Researched once, before any conversation started — every one of them read this same
            corpus. Searching per conversation would have given them different inputs, and a
            difference between them would then say nothing about the brief.
          </p>
        </section>
      )}

      <section className="mb-6">
        <h2 className="mb-2 text-xs uppercase tracking-wide text-slate-500">
          Conversations ({settledCount} of {declared} finished)
        </h2>
        {/* The window between "parent written" and "members created": research runs first, so
            for a few minutes the parent lists run ids that do not exist yet. Saying so beats an
            empty list, which is what a dead fan-out also looks like. */}
        {detail.status === 'researching' && detail.members.length === 0 && (
          <p className="mb-2 text-xs text-amber-400">
            Researching the subject first. The conversations start when the search finishes —
            usually a few minutes — and nothing is lost if you navigate away.
          </p>
        )}
        <ul className="space-y-1 text-sm">
          {detail.members.map((m) => (
            <li key={m.run_id} className="flex items-center gap-2">
              <span className="w-16 text-[11px] uppercase tracking-wide text-slate-500">
                {m.cell}
              </span>
              {m.run ? (
                <button
                  onClick={() => onOpenRun(m.run_id)}
                  className="text-left text-matrix-accent hover:underline"
                >
                  {m.run.name || m.run_id.slice(0, 8)}
                </button>
              ) : (
                // Reported, not hidden. A group that is short changes what its counts mean,
                // so the reader has to see that a run is missing.
                <span className="text-rose-300/80">
                  never started
                  <span className="ml-1 text-[11px] text-slate-500">
                    (this group is short, so its counts are out of fewer runs)
                  </span>
                </span>
              )}
              {m.run && (
                <span className="text-[11px] text-slate-500">
                  {m.run.status} · {m.run.turn_count} turns · $
                  {(m.run.total_cost_usd ?? 0).toFixed(3)}
                </span>
              )}
            </li>
          ))}
        </ul>
      </section>

      <section>
        <h2 className="mb-2 text-xs uppercase tracking-wide text-slate-500">Report</h2>

        {!detail.report_ready && !detail.has_report && (
          <p className="text-sm text-slate-400">
            Waiting for {declared - settledCount} conversation
            {declared - settledCount === 1 ? '' : 's'} to finish. The report is built once they
            all have — reporting sooner would count a conclusion as absent from a conversation
            that had not reached it yet.
          </p>
        )}

        {detail.report_ready && !detail.has_report && !detail.report_error && (
          <div className="space-y-2">
            {/* Two states that `has_report: false` cannot tell apart, and they call for opposite
                controls. A claim means a build owns this right now — a report takes about eight
                minutes, which is long enough for an operator to conclude it failed — so offering
                a FORCED rebuild there invites paying for a second one. No claim means nothing is
                working on it, and then a button is the only way anything happens, because the
                automatic path has exactly one trigger: the last member to finish. */}
            {buildInFlight ? (
              <>
                <p className="text-sm text-slate-400">
                  Building the report — started {claimedAgo}. This takes about eight minutes: one
                  pass per conversation, then the grouping and the synthesis.
                </p>
                <p className="text-[11px] text-slate-500">
                  It will appear here on its own. Nothing needs to stay open.
                </p>
              </>
            ) : (
              <>
                <p className="text-sm text-slate-400">
                  Every conversation has finished and no report is being built.
                </p>
                <button
                  onClick={() => generate(true)}
                  disabled={generating}
                  className="rounded border border-matrix-border px-2 py-1 text-xs text-slate-300 disabled:opacity-50"
                >
                  {generating ? 'Working…' : 'Build the report'}
                </button>
              </>
            )}
          </div>
        )}

        {detail.report_error && (
          <div className="mb-3 space-y-2">
            <p className="text-sm text-rose-300">{detail.report_error}</p>
            <button
              onClick={() => generate(true)}
              disabled={generating}
              className="rounded border border-matrix-border px-2 py-1 text-xs text-slate-300 disabled:opacity-50"
            >
              {generating ? 'Working…' : 'Try again'}
            </button>
          </div>
        )}

        {error && detail && <p className="mb-3 text-sm text-rose-300">{error}</p>}

        {rebuilding && (
          <p className="mb-3 text-sm text-amber-200">
            Rebuilding the report. What is shown below is the previous one until the new report
            lands.
          </p>
        )}

        {detail.report && (
          <div className="space-y-6">
            {/* Read first: what the runs concluded and agreed on, before the full claim table. */}
            <ConclusionsPanel report={detail.report} cells={reportCells} />
            <div>
              <h3 className="mb-2 text-sm text-slate-300">
                What held, by group
              </h3>
              <ClaimTable
                claims={detail.report.claims}
                cells={reportCells}
                clustered={detail.report.clustered}
              />
            </div>

            {detail.report.synthesis && (
              <div>
                <h3 className="mb-2 text-sm text-slate-300">Across the conversations</h3>
                {/* Pre-wrapped rather than rendered as markdown: adding a markdown
                    dependency for one field is not worth it, and the synthesis is already
                    written as readable prose with headings. */}
                <pre className="whitespace-pre-wrap rounded border border-matrix-border bg-matrix-bg p-3 text-xs leading-relaxed text-slate-300">
                  {detail.report.synthesis}
                </pre>
              </div>
            )}

            {/* The report's own caveats, shown with it and not tucked behind a link. A
                reader who sees "1 of 5" needs to know the matcher under-merges BEFORE they
                conclude a persona changed their mind. */}
            {detail.report.caveats.length > 0 && (
              <ul className="space-y-1 border-l-2 border-amber-700/50 pl-3 text-[11px] text-amber-200/80">
                {detail.report.caveats.map((c) => (
                  <li key={c}>{c}</li>
                ))}
              </ul>
            )}

            <div className="flex items-center gap-3">
              <p className="text-[11px] text-slate-500">
                Report cost ${detail.report.cost_usd.toFixed(4)}.
              </p>
              {/* Worth offering because clustering is not reproducible: the grouping model is
                  asked for temperature 0 and Sonnet 5 silently discards it, so a rebuild can
                  legitimately group differently. It costs what the line above says. */}
              <button
                onClick={() => generate(true)}
                disabled={generating || rebuilding}
                className="rounded border border-matrix-border px-2 py-1 text-[11px] text-slate-400 hover:text-slate-200 disabled:opacity-50"
              >
                {generating || rebuilding ? 'Rebuilding…' : 'Rebuild report'}
              </button>
            </div>
          </div>
        )}
      </section>
    </Frame>
  )
}

function Frame({
  children,
  onBack,
  title,
}: {
  children: React.ReactNode
  onBack: () => void
  title: string
}) {
  return (
    <div className="mx-auto max-w-4xl p-6">
      <button onClick={onBack} className="mb-4 text-sm text-slate-400 hover:text-slate-200">
        ← Back
      </button>
      <h1 className="mb-1 text-xl text-slate-100">{title}</h1>
      {children}
    </div>
  )
}
