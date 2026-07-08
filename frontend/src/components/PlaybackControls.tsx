// SPDX-License-Identifier: Apache-2.0
import type { PlaybackMode } from '../hooks/useRunStream'

interface Props {
  mode: PlaybackMode
  behind: number // buffered-but-unrevealed events (engine is ahead of view)
  engineDone: boolean
  speedMs: number
  onPause: () => void
  onResume: () => void
  onStep: () => void
  onCatchUp: () => void
  onSpeed: (ms: number) => void
}

// UI-ONLY playback (spec §5a). These controls move the client reveal cursor
// over the buffered event stream. They do NOT pause, slow, or gate the engine —
// events keep buffering (see `behind`) while the viewer is paused.
export function PlaybackControls({
  mode,
  behind,
  engineDone,
  speedMs,
  onPause,
  onResume,
  onStep,
  onCatchUp,
  onSpeed,
}: Props) {
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-lg border border-matrix-border bg-matrix-panel p-2 text-sm">
      {mode === 'live' ? (
        <button
          onClick={onPause}
          className="rounded bg-matrix-border px-3 py-1 hover:bg-slate-700"
        >
          ⏸ Pause
        </button>
      ) : (
        <button
          onClick={onResume}
          className="rounded bg-matrix-accent/20 px-3 py-1 text-matrix-accent hover:bg-matrix-accent/30"
        >
          ▶ Resume
        </button>
      )}
      <button
        onClick={onStep}
        className="rounded bg-matrix-border px-3 py-1 hover:bg-slate-700"
        title="Reveal one more buffered event"
      >
        ⏭ Step
      </button>
      <button
        onClick={onCatchUp}
        disabled={behind === 0}
        className="rounded bg-matrix-border px-3 py-1 hover:bg-slate-700 disabled:opacity-40"
        title="Jump the view to the latest buffered event"
      >
        ⏩ Catch up{behind > 0 ? ` (+${behind})` : ''}
      </button>

      <label className="ml-2 flex items-center gap-2 text-xs text-slate-400">
        speed
        <input
          type="range"
          min={100}
          max={2000}
          step={100}
          value={speedMs}
          onChange={(e) => onSpeed(Number(e.target.value))}
        />
      </label>

      <span className="ml-auto text-xs text-slate-500">
        {mode === 'paused' && behind > 0 && (
          <span className="text-amber-400">paused · engine running ({behind} buffered)</span>
        )}
        {mode === 'paused' && behind === 0 && !engineDone && 'paused · caught up'}
        {mode === 'live' && !engineDone && <span className="text-matrix-live">● live</span>}
        {engineDone && behind === 0 && 'run complete'}
      </span>
    </div>
  )
}
