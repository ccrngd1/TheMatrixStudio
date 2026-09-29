// SPDX-License-Identifier: Apache-2.0
import { Fragment, useEffect, useState } from 'react'
import { api } from '../api'
import type { Persona, Quote, RunDetail, WorkingAssumption } from '../types'
import { describeFork, forkEstimate } from '../lib/forkCost'
import { useRunStream } from '../hooks/useRunStream'
import { isLive, isResumable, isTerminal } from '../lib/runStatus'
import { CastBoard } from '../components/CastBoard'
import { ConversationFeed } from '../components/ConversationFeed'
import { ParticipationPanel } from '../components/ParticipationPanel'
import { PlaybackControls } from '../components/PlaybackControls'
import { Dossier } from '../components/Dossier'
import { BriefDialog } from '../components/BriefButton'
import { ExportMenu } from '../components/ExportMenu'
import { ResearchPanel } from '../components/ResearchPanel'
import { SummaryPanel } from '../components/SummaryPanel'
import { AsidesDrawer } from '../components/AsidesDrawer'
import { BranchTree } from '../components/BranchTree'
import { Scrubber } from '../components/Scrubber'
import type { StoredSummary } from '../types'
import { FaceStrip, RunHud, RunStatusTag, RunTabs } from '../components/run/RunChrome'
import { Hint } from '../components/Hint'
import { TopBar } from './Shell'
import { navigate, type RunTab } from '../lib/route'
import { Btn, Label, Panel, Sheet, Ticks } from '../ui/primitives'
import { Icon } from '../ui/icons'
import { useWide } from '../ui/useWide'

interface Props {
  runId: string
  onBack: () => void
  // Navigate to another run (used when a branch is created / lineage is clicked).
  onOpenRun?: (runId: string) => void
  // Open the new-run form prefilled with this run's setup, to edit and run afresh.
  onStartFresh?: (runId: string) => void
  /** Which pane a phone shows (the URL's tab). A wide screen shows all three. */
  tab?: RunTab
  /** The scrubber route (`#/run/:id/scrub`). */
  scrub?: boolean
}

// The control room (docs/MOBILE-UI.md §4.2): header, HUD, then Conversation · Cast · Analysis — tabs on a
// phone, columns from 768 px. Works identically for a live run and a replayed completed run.
export function LiveView({ runId, onBack, onOpenRun, onStartFresh, tab = 'conversation', scrub = false }: Props) {
  const wide = useWide()
  const [menuOpen, setMenuOpen] = useState(false)
  const [briefOpen, setBriefOpen] = useState(false)
  const [detail, setDetail] = useState<RunDetail | null>(null)
  const [cast, setCast] = useState<Persona[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [asidesOpen, setAsidesOpen] = useState(false)
  const [generated, setGenerated] = useState<StoredSummary | null>(null)
  const [imported, setImported] = useState<StoredSummary | null>(null)
  const [defaultInstructions, setDefaultInstructions] = useState<string>('')
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
      if (onOpenRun) onOpenRun(res.run_id)
    } catch (e) {
      setBranchError((e as Error).message)
    } finally {
      setBranching(false)
    }
  }

  // Fork at an assumption's turn with it replaced or withdrawn (matrix_studio/assumptions.py). Throws so
  // the card that asked can show the error where the operator is looking.
  const forkAssumption = async (a: WorkingAssumption, statement: string | null) => {
    const res = await api.branchRun(runId, a.turn, {
      ...(analysisModel ? { model: analysisModel } : {}),
      description:
        statement === null ? `${a.id} withdrawn at turn ${a.turn}` : `${a.id}: ${a.statement} → ${statement}`,
      mutation:
        statement === null
          ? { kind: 'withdraw_assumption', assumption_id: a.id }
          : { kind: 'replace_assumption', assumption_id: a.id, statement },
    })
    if (onOpenRun) onOpenRun(res.run_id)
  }

  const scrubbing = scrub && completed
  const maxMessages = (detail?.config?.max_messages as number | undefined) ?? undefined
  // The turn reached so far, from what has been revealed. Consultants answer inside a turn.
  const turn = state.feed.reduce((t, m) => (m.consultant ? t : Math.max(t, m.turn)), 0)
  const status = state.status === 'idle' ? (detail?.status ?? 'idle') : state.status
  const liveNow = status === 'running' && !stream.engineDone
  const next = state.thinking ? state.activeSpeaker : null
  const openScrub = () => navigate({ name: 'scrub', runId })
  const openAsides = () => {
    setMenuOpen(false)
    setAsidesOpen(true)
  }
  const jump = (seq: number) => {
    setJumpTo({ seq, nonce: Date.now() })
    // On a phone the transcript is another tab; go to it so the jump lands where the reader is looking.
    if (!wide && tab !== 'conversation') navigate({ name: 'run', runId, tab: 'conversation' })
  }

  const header = (
    <TopBar
      back={onBack}
      code
      title={detail?.name ?? 'Run'}
      sub={
        <span className="flex min-w-0 items-center gap-2">
          <RunStatusTag status={status} stalled={stream.stalled} turn={turn} max={maxMessages} />
          <span className="cc-tt truncate">{detail?.description ?? detail?.topic}</span>
        </span>
      }
      right={
        <>
          {/* Stop or Resume, always in the same place (§4.2); everything else is under ⋯. */}
          {running && (
            <Btn variant="danger" size="sm" onClick={stop} disabled={stopping || stopRequested}
              aria-description="Stop after the turn being generated now. That turn is finished and kept; no further turns start. The run can be resumed later.">
              <Icon name="stop" size={12} />{' '}
              {stopRequested ? 'Stopping after this turn…' : stopping ? 'Stopping…' : 'Stop'}
            </Btn>
          )}
          {resumable && (
            <Btn variant="warn" size="sm" onClick={resume} disabled={resuming}
              aria-description="Continue this interrupted or failed run forward from its last checkpoint (same run)">
              <Icon name="resume" size={13} /> {resuming ? 'Resuming…' : 'Resume'}
            </Btn>
          )}
          <button type="button" className="cc-icon" aria-label="More" onClick={() => setMenuOpen(true)}>
            <Icon name="more" size={22} />
          </button>
        </>
      }
    />
  )

  const alerts = [
    stopError && `Stop failed: ${stopError}`,
    resumeError && `Resume failed: ${resumeError}`,
    branchError && `Branch failed: ${branchError}`,
    state.status === 'failed' && `Simulation failed: ${state.error}`,
  ].filter(Boolean) as string[]

  const lineageRow = (lineage?.parent || (lineage?.branches?.length ?? 0) > 0) && (
    <div className="flex flex-none flex-wrap items-center gap-1.5 px-[14px] pt-2">
      {lineage?.parent && (
        <>
          <Label>From</Label>
          <button type="button" className="cc-chip cc-accent" onClick={() => onOpenRun?.(lineage.parent!.run_id)}>
            <Icon name="branch" size={12} /> {lineage.parent.name ?? lineage.parent.run_id.slice(0, 8)} @
            {lineage.parent.branch_turn}
          </button>
        </>
      )}
      {(lineage?.branches?.length ?? 0) > 0 && (
        <>
          <Label>Branches</Label>
          {lineage!.branches.map((b) => (
            <button key={b.run_id} type="button" className="cc-chip cc-accent" onClick={() => onOpenRun?.(b.run_id)}>
              <Icon name="branch" size={12} /> {b.name ?? b.run_id.slice(0, 8)} @{b.branch_turn}
            </button>
          ))}
        </>
      )}
    </div>
  )

  const conversationPane = (
    <div className="cc-pane">
      <FaceStrip order={state.order} next={next} onOpen={setSelected} />
      <ConversationFeed
        feed={state.feed}
        agents={state.agents}
        activeSpeaker={state.activeSpeaker}
        thinking={state.thinking}
        jumpTo={jumpTo}
        runId={runId}
        sourceIndex={state.sourceIndex}
        quotes={quotes}
        assumptions={state.assumptions}
        onForkAssumption={state.status !== 'running' && state.status !== 'idle' ? forkAssumption : undefined}
        forkCost={(t) => describeFork(forkEstimate(stream.events, t))}
        onOpenDossier={setSelected}
      />
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
        turn={turn}
        maxTurn={maxMessages}
        ended={
          completed && (
            <>
              <Btn size="sm" onClick={openScrub}>
                <Icon name="clock" size={14} /> Scrub
              </Btn>
              <Btn size="sm" onClick={openAsides}>
                <Icon name="chat" size={14} /> Asides
              </Btn>
            </>
          )
        }
      />
    </div>
  )

  const castPane = (
    <div className="cc-pane">
      <div className="cc-scroll">
        <CastBoard state={state} onSelect={setSelected} />
      </div>
    </div>
  )

  const analysisPane = (
    <div className="cc-pane">
      <div className="cc-scroll">
        {!completed ? (
          // Mid-run the counts change under the reader, and the questions this tab answers ("who spoke, what
          // did it conclude?") are asked of a transcript rather than of a conversation in progress.
          <Panel center>
            <Label>
              Analysis locked <span aria-hidden="true">◇</span>
            </Label>
            <p className="cc-muted mt-1.5">Opens when the run ends.</p>
            {maxMessages ? (
              <div className="mt-2">
                <Ticks n={Math.min(turn, maxMessages)} max={maxMessages} live={liveNow} />
              </div>
            ) : null}
          </Panel>
        ) : (
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
            {/* Renders nothing unless this run researched, so it adds no section to the
                conversations that did not. §5.3: nobody watches the pass, so this is
                where an operator finds out what it did. */}
            <ResearchPanel research={detail?.research ?? null} />
            {/* Consultants and injected messages are not a share of the conversation; the panel filters
                them itself. */}
            <ParticipationPanel feed={state.feed} order={state.order} onJump={jump} />
            <BranchTree runId={runId} onOpenRun={onOpenRun} />
          </>
        )}
      </div>
    </div>
  )

  const panes = wide
    ? [castPane, conversationPane, analysisPane]
    : [tab === 'cast' ? castPane : tab === 'analysis' ? analysisPane : conversationPane]

  return (
    <>
      {header}
      {scrubbing ? (
        <Scrubber
          runId={runId}
          maxTurn={maxTurn}
          cast={cast}
          defaultBudget={maxMessages ?? maxTurn}
          models={models}
          defaultModel={analysisModel}
          onBranch={branchFrom}
          branching={branching}
          onStartFresh={onStartFresh ? () => onStartFresh(runId) : undefined}
        />
      ) : (
        <>
          <RunHud
            turn={turn}
            max={maxMessages}
            live={liveNow}
            cost={state.totalCost}
            tokensIn={state.totalTokensIn}
            tokensOut={state.totalTokensOut}
          />
          {lineageRow}
          {alerts.map((a) => (
            <p key={a} role="alert" className="cc-card mx-[14px] mt-2 flex-none text-cc-danger">
              {a}
            </p>
          ))}
          <RunTabs runId={runId} tab={tab} locked={!completed} />
          <div className="cc-panes">
            {panes.map((p, i) => (
              <Fragment key={i}>{p}</Fragment>
            ))}
          </div>
        </>
      )}

      {menuOpen && (
        <Sheet title="Run options" onClose={() => setMenuOpen(false)}>
          <div className="cc-menu">
            {completed && (
              <button type="button" onClick={() => { setMenuOpen(false); setBriefOpen(true) }}>
                <Icon name="doc" size={18} /> Decision brief
              </button>
            )}
            {completed && (
              <button type="button" onClick={() => { setMenuOpen(false); openScrub() }}>
                <Icon name="clock" size={18} /> Scrub and branch
              </button>
            )}
            {completed && (
              <button type="button" onClick={openAsides}>
                <Icon name="chat" size={18} /> Asides
              </button>
            )}
            {onStartFresh && (
              <button type="button" onClick={() => { setMenuOpen(false); onStartFresh(runId) }}>
                <Icon name="plus" size={18} /> Start fresh from this setup
              </button>
            )}
          </div>
          {/* Offered only once the run has finished: an export mid-run would be a transcript that
              stops at an arbitrary turn with no sign that more is coming. */}
          {completed && <ExportMenu kind="run" id={runId} name={detail?.name ?? runId} />}
          {models.length > 0 && (
            <div className="cc-setting">
              <span>
                Model <Hint label="the run's model">Used for analysis (summary and asides) and for branching forward from this run.</Hint>
              </span>
              <select
                value={analysisModel}
                onChange={(e) => setAnalysisModel(e.target.value)}
                aria-label="Model"
                className="cc-field max-w-[14rem]"
              >
                {models.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.label}
                  </option>
                ))}
              </select>
            </div>
          )}
          {/* Which model the personas actually spoke with. A definition once asked for Opus 5 and
              every run silently used Sonnet 5; this is where that becomes visible. */}
          {detail?.models?.voice && (
            <p className="cc-muted">
              Models used:{' '}
              {Object.entries(detail.models)
                .map(([role, m]) => `${role} ${(m ?? 'default').split('/').pop()?.split('anthropic.').pop()}`)
                .join(' · ')}
            </p>
          )}
          <p className="cc-muted">
            {stream.engineDone ? '✓ Replay complete' : stream.connected ? 'Connected' : 'Connecting…'}
          </p>
        </Sheet>
      )}

      {briefOpen && (
        <BriefDialog kind="run" id={runId} name={detail?.name ?? runId} onClose={() => setBriefOpen(false)} />
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
    </>
  )
}
