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
import type { StoredSummary } from '../types'

interface Props {
  runId: string
  onBack: () => void
}

// The control room: cast board + live feed + cost meter + playback + dossier.
// Works identically for a live run and a replayed completed run.
export function LiveView({ runId, onBack }: Props) {
  const [detail, setDetail] = useState<RunDetail | null>(null)
  const [cast, setCast] = useState<Persona[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [asidesOpen, setAsidesOpen] = useState(false)
  const [generated, setGenerated] = useState<StoredSummary | null>(null)
  const [imported, setImported] = useState<StoredSummary | null>(null)
  const [defaultInstructions, setDefaultInstructions] = useState<string>('')

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
  }, [runId])

  const stream = useRunStream({ runId, cast })
  const { state } = stream

  // A run is analyzable once it has completed (live or on reload).
  const completed = detail?.status === 'complete' || state.status === 'complete'

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
                {state.status === 'running' && !stream.engineDone ? '· running' : `· ${state.status}`}
              </span>
            </h1>
            <p className="text-xs text-slate-500">{detail?.description ?? detail?.topic}</p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          {completed && (
            <button
              onClick={() => setAsidesOpen(true)}
              className="rounded border border-matrix-border px-3 py-1 text-sm text-slate-300 hover:border-matrix-accent"
              title="Ask read-only questions about the finished run"
            >
              💬 Asides
            </button>
          )}
          <span className="text-xs text-slate-500">
            {stream.connected ? '🔌 connected' : '… connecting'}
          </span>
        </div>
      </header>

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
            <SummaryPanel
              runId={runId}
              generated={generated}
              imported={imported}
              defaultInstructions={defaultInstructions}
              canGenerate={completed}
              onUpdated={setGenerated}
            />
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

      {selected && state.agents[selected] && (
        <Dossier
          agent={state.agents[selected]}
          feed={state.feed}
          onClose={() => setSelected(null)}
        />
      )}

      {asidesOpen && (
        <AsidesDrawer runId={runId} cast={cast} onClose={() => setAsidesOpen(false)} />
      )}
    </div>
  )
}
