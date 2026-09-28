// SPDX-License-Identifier: Apache-2.0
import { useEffect, useState } from 'react'
import { api } from '../api'
import type { Persona, Quote, RunDetail } from '../types'
import { useRunStream } from '../hooks/useRunStream'
import { isLive, isResumable, isTerminal } from '../lib/runStatus'
import { CastBoard } from '../components/CastBoard'
import { ConversationFeed } from '../components/ConversationFeed'
import { CostMeter } from '../components/CostMeter'
import { ParticipationPanel } from '../components/ParticipationPanel'
import { PlaybackControls } from '../components/PlaybackControls'
import { Dossier } from '../components/Dossier'
import { BriefButton } from '../components/BriefButton'
import { ExportMenu } from '../components/ExportMenu'
import { ResearchPanel } from '../components/ResearchPanel'
import { SummaryPanel } from '../components/SummaryPanel'
import { AsidesDrawer } from '../components/AsidesDrawer'
import { BranchTree } from '../components/BranchTree'
import { Scrubber } from '../components/Scrubber'
import type { StoredSummary } from '../types'

interface Props {
  runId: string
  onBack: () => void
  // Navigate to another run (used when a branch is created / lineage is clicked).
  onOpenRun?: (runId: string) => void
  // Open the new-run form prefilled with this run's setup, to edit and run afresh.
  onStartFresh?: (runId: string) => void
}

// The control room: cast board + live feed + cost meter + playback + dossier.
// Works identically for a live run and a replayed completed run.
export function LiveView({ runId, onBack, onOpenRun, onStartFresh }: Props) {
  const [detail, setDetail] = useState<RunDetail | null>(null)
  const [cast, setCast] = useState<Persona[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [asidesOpen, setAsidesOpen] = useState(false)
  const [generated, setGenerated] = useState<StoredSummary | null>(null)
  const [imported, setImported] = useState<StoredSummary | null>(null)
  const [defaultInstructions, setDefaultInstructions] = useState<string>('')
  const [scrubbing, setScrubbing] = useState(false)
  // The turn the participation panel last asked for. `nonce` makes a repeat click on the
  // same turn a new request, since the seq on its own would not change.
  const [jumpTo, setJumpTo] = useState<{ seq: number; nonce: number } | null>(null)
  const [branching, setBranching] = useState(false)
  const [branchError, setBranchError] = useState<string | null>(null)
  const [resuming, setResuming] = useState(false)
  const [resumeError, setResumeError] = useState<string | null>(null)
  // `stopRequested` stays true after the request succeeds: the effect lands a turn
  // later, so the button must show the request was accepted rather than look
  // unresponsive and invite a second click.
  const [stopping, setStopping] = useState(false)
  const [stopRequested, setStopRequested] = useState(false)
  const [stopError, setStopError] = useState<string | null>(null)
  // In-thread model picker: the models allowlist + the currently selected model
  // for analysis (summary/asides) and forward branching from this thread.
  const [models, setModels] = useState<{ id: string; label: string }[]>([])
  const [analysisModel, setAnalysisModel] = useState<string>('')
  // Bumped after a resume to force the run detail refetch + stream reconnect.
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    api.getRun(runId).then((d) => {
      setDetail(d)
      setCast(d.cast || [])
      setGenerated(d.summary?.generated ?? null)
      setImported(d.summary?.imported ?? null)
    })
    // Fetch the editable default analyst-role framing so the regenerate editor
    // can prefill / reset-to-default even before any generation.
    api.getSummary(runId).then((s) => setDefaultInstructions(s.default_instructions))
  }, [runId, reloadKey])

  // Load the selectable-models allowlist once; default the in-thread picker to
  // the run's own model when set, else the server default.
  useEffect(() => {
    api.getModels().then((m) => {
      setModels(m.models)
      setAnalysisModel((cur) => cur || (detail?.config?.model as string) || m.default)
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail?.config?.model])

  const stream = useRunStream({ runId, cast, reloadKey })
  const { state } = stream

  // A run is analyzable once generation has ENDED with real turns to look at —
  // not only on a clean completion. A stopped or capped run has a transcript, a
  // final checkpoint and a cast, so withholding the scrubber and asides from it
  // would hide exactly the run you most want to inspect: the one you cut short.
  const ENDED_WITH_TRANSCRIPT = ['complete', 'stopped', 'capped']
  const completed =
    ENDED_WITH_TRANSCRIPT.includes(detail?.status ?? '') ||
    ENDED_WITH_TRANSCRIPT.includes(state.status ?? '')
  // Error-recovery / deliberate stop: continue forward in place. Mirrors
  // branching.RESUMABLE_STATUSES server-side, which rejects anything else.
  const resumable = isResumable(detail?.status)
  // Only a run this server is actively generating can be stopped; the API answers
  // 409 otherwise, so the button is hidden rather than offered and refused.
  // `pending` counts as stoppable — see LIVE_STATUSES. Gating on `running` alone hid
  // the Stop button for the window a run spends preparing, on a request the API accepts.
  const running = isLive(detail?.status) && !isTerminal(state.status)
  const lineage = detail?.lineage
  const maxTurn = detail?.result?.total_turns ?? detail?.turn_count ?? 0

  // Which passage each message quotes. Computed server-side from the finished transcript, so it is
  // fetched once the run has ended; a failure leaves the feed as it was rather than blocking it.
  const [quotes, setQuotes] = useState<Record<string, Quote[]>>({})
  useEffect(() => {
    if (!completed) return
    let live = true
    Promise.resolve()
      .then(() => api.getRunQuotes(runId))
      .then((r) => live && setQuotes(r.quotes))
      .catch(() => live && setQuotes({}))
    return () => {
      live = false
    }
  }, [completed, runId])

  // Ask the engine to stop after the turn in flight. Not a cancel: that turn is
  // finished and persisted, which is why the label says "after this turn".
  const stop = async () => {
    setStopping(true)
    setStopError(null)
    try {
      await api.stopRun(runId)
      setStopRequested(true)
    } catch (e) {
      setStopError((e as Error).message)
    } finally {
      setStopping(false)
    }
  }

  // Resume an interrupted/failed run in place, then reconnect the stream so the
  // newly-generated turns stream in live (the prior socket closed on the
  // terminal event). The run keeps its id/codename.
  const resume = async () => {
    setResuming(true)
    setResumeError(null)
    try {
      await api.resumeRun(runId)
      setReloadKey((k) => k + 1)
    } catch (e) {
      setResumeError((e as Error).message)
    } finally {
      setResuming(false)
    }
  }

  // Fork this run at the given turn into a NEW run, then navigate to its live
  // view. The parent (this run) is never modified.
  const branchFrom = async (fromTurn: number, mutation?: Record<string, unknown>, modelOverride?: string) => {
    setBranching(true)
    setBranchError(null)
    try {
      const opts: Record<string, unknown> = {}
      const chosenModel = modelOverride || analysisModel
      if (chosenModel) opts.model = chosenModel
      if (mutation) opts.mutation = mutation
      const res = await api.branchRun(runId, fromTurn, Object.keys(opts).length ? opts : undefined)
      setScrubbing(false)
      if (onOpenRun) onOpenRun(res.run_id)
    } catch (e) {
      setBranchError((e as Error).message)
    } finally {
      setBranching(false)
    }
  }

  return (
    <div className="flex h-screen flex-col">
      <header className="flex items-center justify-between border-b border-matrix-border px-4 py-3">
        <div className="flex items-center gap-3">
          <button onClick={onBack} className="text-slate-400 hover:text-slate-200">
            ← Back
          </button>
          <div>
            <h1 className="text-lg font-bold text-slate-100">
              {detail?.name ?? 'Run'}{' '}
              <span className="text-sm font-normal text-slate-500">
                {state.status === 'running' && stream.stalled
                  ? '· stalled'
                  : state.status === 'running' && !stream.engineDone
                    ? '· running'
                    : `· ${state.status}`}
              </span>
            </h1>
            <p className="text-xs text-slate-500">{detail?.description ?? detail?.topic}</p>
            {/* Which model the personas actually spoke with. A definition once asked for Opus 5 and
                every run silently used Sonnet 5; this is where that becomes visible. */}
            {detail?.models?.voice && (
              <p className="text-[11px] text-slate-600" title={Object.entries(detail.models)
                .map(([role, m]) => `${role}: ${m ?? 'default'}`).join('\n')}>
                voices: {detail.models.voice.split('/').pop()?.split('anthropic.').pop()}
              </p>
            )}
          </div>
        </div>
        <div className="flex items-center gap-3">
          {/* Offered only once the run has finished: an export mid-run would be a transcript that
              stops at an arbitrary turn with no sign that more is coming. */}
          {completed && <BriefButton kind="run" id={runId} name={detail?.name ?? runId} />}
          {completed && <ExportMenu kind="run" id={runId} name={detail?.name ?? runId} />}
          {running && (
            <button
              onClick={stop}
              disabled={stopping || stopRequested}
              className="rounded border border-red-500/60 px-3 py-1 text-sm text-red-300 hover:border-red-400 disabled:opacity-50"
              title="Stop after the turn being generated now. That turn is finished and kept; no further turns start. The run can be resumed later."
            >
              {stopRequested ? '■ Stopping after this turn…' : stopping ? '■ Stopping…' : '■ Stop'}
            </button>
          )}
          {resumable && (
            <button
              onClick={resume}
              disabled={resuming}
              className="rounded border border-amber-500/60 px-3 py-1 text-sm text-amber-300 hover:border-amber-400 disabled:opacity-50"
              title="Continue this interrupted/failed run forward from its last checkpoint (same run)"
            >
              {resuming ? '↻ Resuming…' : '↻ Resume'}
            </button>
          )}
          {completed && (
            <button
              onClick={() => setScrubbing((s) => !s)}
              className={`rounded border px-3 py-1 text-sm hover:border-matrix-accent ${
                scrubbing
                  ? 'border-matrix-accent text-matrix-accent'
                  : 'border-matrix-border text-slate-300'
              }`}
              title="Scrub to any turn and view state as of that point; branch from there"
            >
              ⏱ Scrubber
            </button>
          )}
          {completed && (
            <button
              onClick={() => setAsidesOpen(true)}
              className="rounded border border-matrix-border px-3 py-1 text-sm text-slate-300 hover:border-matrix-accent"
              title="Ask read-only questions about the finished run"
            >
              💬 Asides
            </button>
          )}
          {models.length > 0 && (
            <label className="flex items-center gap-1 text-xs text-slate-400" title="Model used for analysis (summary/asides) and forward branching from this thread">
              Model
              <select
                value={analysisModel}
                onChange={(e) => setAnalysisModel(e.target.value)}
                className="max-w-[14rem] rounded border border-matrix-border bg-matrix-panel px-2 py-1 text-xs text-slate-200"
              >
                {models.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.label}
                  </option>
                ))}
              </select>
            </label>
          )}
          <span className="text-xs text-slate-500">
            {stream.engineDone
              ? '✓ replay complete'
              : stream.connected
                ? '🔌 connected'
                : '… connecting'}
          </span>
        </div>
      </header>

      {stopError && (
        <div className="border-b border-red-900/50 bg-red-950/40 px-4 py-2 text-xs text-red-300">
          Stop failed: {stopError}
        </div>
      )}

      {resumeError && (
        <div className="border-b border-red-900/50 bg-red-950/40 px-4 py-2 text-xs text-red-300">
          Resume failed: {resumeError}
        </div>
      )}

      {/* Phase 2a branch lineage — this run's parent and/or its child branches. */}
      {(lineage?.parent || (lineage?.branches?.length ?? 0) > 0) && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-matrix-border bg-matrix-panel/60 px-4 py-2 text-xs text-slate-400">
          {lineage?.parent && (
            <span>
              ⑂ Branched from{' '}
              <button
                onClick={() => onOpenRun && onOpenRun(lineage.parent!.run_id)}
                className="font-semibold text-matrix-accent hover:underline"
              >
                {lineage.parent.name ?? lineage.parent.run_id.slice(0, 8)}
              </button>{' '}
              @ turn {lineage.parent.branch_turn}
            </span>
          )}
          {(lineage?.branches?.length ?? 0) > 0 && (
            <span className="flex flex-wrap items-center gap-1">
              Branches:
              {lineage!.branches.map((b) => (
                <button
                  key={b.run_id}
                  onClick={() => onOpenRun && onOpenRun(b.run_id)}
                  className="rounded border border-matrix-border px-2 py-0.5 text-matrix-accent hover:border-matrix-accent"
                >
                  {b.name ?? b.run_id.slice(0, 8)} @ {b.branch_turn}
                </button>
              ))}
            </span>
          )}
        </div>
      )}

      {branchError && (
        <div className="border-b border-red-900 bg-red-950/40 px-4 py-2 text-sm text-red-300">
          Branch failed: {branchError}
        </div>
      )}

      {scrubbing && completed ? (
        <Scrubber
          runId={runId}
          maxTurn={maxTurn}
          cast={cast}
          defaultBudget={(detail?.config?.max_messages as number) ?? maxTurn}
          models={models}
          defaultModel={analysisModel}
          onBranch={branchFrom}
          branching={branching}
          onStartFresh={onStartFresh ? () => onStartFresh(runId) : undefined}
        />
      ) : (
        <>
      <div className="border-b border-matrix-border px-4 py-2">
        <PlaybackControls
          mode={stream.mode}
          behind={stream.behind}
          engineDone={stream.engineDone}
          speedMs={stream.speedMs}
          onPause={stream.pause}
          onResume={stream.resume}
          onStep={stream.stepForward}
          onCatchUp={stream.catchUp}
          onSpeed={stream.setSpeedMs}
        />
      </div>

      <div className="grid flex-1 grid-cols-1 gap-4 overflow-hidden p-4 lg:grid-cols-[320px_1fr]">
        <aside className="space-y-4 overflow-y-auto">
          <CostMeter
            totalCost={state.totalCost}
            tokensIn={state.totalTokensIn}
            tokensOut={state.totalTokensOut}
          />
          <CastBoard state={state} onSelect={setSelected} />
          {completed && (
            <>
              {/* Only on a finished run: mid-run the counts change under the reader, and
                  the question this answers ("who actually spoke, and when?") is one asked
                  of a transcript rather than of a conversation in progress. */}
              <ParticipationPanel
                // Consultants answer questions; they are not a share of the conversation.
                feed={state.feed.filter((m) => !m.consultant)}
                order={state.order}
                onJump={(seq) => setJumpTo({ seq, nonce: Date.now() })}
              />
              <SummaryPanel
                runId={runId}
                generated={generated}
                imported={imported}
                defaultInstructions={defaultInstructions}
                canGenerate={completed}
                model={analysisModel || undefined}
                onUpdated={setGenerated}
              />
              {/* Renders nothing unless this run researched, so it adds no section to the
                  conversations that did not. §5.3: nobody watches the pass, so this is
                  where an operator finds out what it did. */}
              <ResearchPanel research={detail?.research ?? null} />
              <BranchTree runId={runId} onOpenRun={onOpenRun} />
            </>
          )}
        </aside>

        <main className="overflow-hidden rounded-lg border border-matrix-border bg-matrix-panel">
          {state.status === 'failed' && (
            <div className="border-b border-red-900 bg-red-950/40 px-4 py-2 text-sm text-red-300">
              Simulation failed: {state.error}
            </div>
          )}
          <ConversationFeed
            feed={state.feed}
            agents={state.agents}
            activeSpeaker={state.activeSpeaker}
            thinking={state.thinking}
            jumpTo={jumpTo}
            runId={runId}
            sourceIndex={state.sourceIndex}
            quotes={quotes}
          />
        </main>
      </div>
        </>
      )}

      {selected && state.agents[selected] && (
        <Dossier
          agent={state.agents[selected]}
          feed={state.feed}
          runId={runId}
          onClose={() => setSelected(null)}
        />
      )}

      {asidesOpen && (
        <AsidesDrawer
          runId={runId}
          cast={cast}
          consultants={((detail?.config?.experts ?? []) as { name?: string }[])
            .map((e) => e.name ?? '')
            .filter(Boolean)}
          turnCount={maxTurn}
          model={analysisModel || undefined}
          models={models}
          onBranch={(branchRunId) => {
            setAsidesOpen(false)
            if (onOpenRun) onOpenRun(branchRunId)
          }}
          onClose={() => setAsidesOpen(false)}
        />
      )}
    </div>
  )
}
