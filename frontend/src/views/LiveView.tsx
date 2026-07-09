// SPDX-License-Identifier: Apache-2.0
import { useEffect, useState } from 'react'
import { api } from '../api'
import type { Persona, RunDetail } from '../types'
import { useRunStream } from '../hooks/useRunStream'
import { CastBoard } from '../components/CastBoard'
import { ConversationFeed } from '../components/ConversationFeed'
import { CostMeter } from '../components/CostMeter'
import { PlaybackControls } from '../components/PlaybackControls'
import { Dossier } from '../components/Dossier'
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
}

// The control room: cast board + live feed + cost meter + playback + dossier.
// Works identically for a live run and a replayed completed run.
export function LiveView({ runId, onBack, onOpenRun }: Props) {
  const [detail, setDetail] = useState<RunDetail | null>(null)
  const [cast, setCast] = useState<Persona[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [asidesOpen, setAsidesOpen] = useState(false)
  const [generated, setGenerated] = useState<StoredSummary | null>(null)
  const [imported, setImported] = useState<StoredSummary | null>(null)
  const [defaultInstructions, setDefaultInstructions] = useState<string>('')
  const [scrubbing, setScrubbing] = useState(false)
  const [branching, setBranching] = useState(false)
  const [branchError, setBranchError] = useState<string | null>(null)
  const [resuming, setResuming] = useState(false)
  const [resumeError, setResumeError] = useState<string | null>(null)
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

  // A run is analyzable once it has completed (live or on reload).
  const completed = detail?.status === 'complete' || state.status === 'complete'
  // Error-recovery: an interrupted/failed run can be resumed forward in place.
  const resumable = detail?.status === 'interrupted' || detail?.status === 'failed'
  const lineage = detail?.lineage
  const maxTurn = detail?.result?.total_turns ?? detail?.turn_count ?? 0

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
          </div>
        </div>
        <div className="flex items-center gap-3">
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
            {stream.connected ? '🔌 connected' : '… connecting'}
          </span>
        </div>
      </header>

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
              <SummaryPanel
                runId={runId}
                generated={generated}
                imported={imported}
                defaultInstructions={defaultInstructions}
                canGenerate={completed}
                model={analysisModel || undefined}
                onUpdated={setGenerated}
              />
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
