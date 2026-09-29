// SPDX-License-Identifier: Apache-2.0
import type { ReactNode } from 'react'
import type { PlaybackMode } from '../hooks/useRunStream'
import { Icon } from '../ui/icons'
import { Ticks } from '../ui/primitives'

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
  /** Turns shown so far, and the run's budget, for the ticks. */
  turn?: number
  maxTurn?: number
  /** What the bar offers once everything has been shown (Scrub, Asides). */
  ended?: ReactNode
}

// Reveal delays, slowest first, and the label each is shown as. 700 ms is the stream's default pace.
const SPEEDS: [number, string][] = [
  [1400, '½×'],
  [700, '1×'],
  [350, '2×'],
  [150, '4×'],
]

// UI-ONLY playback (spec §5a). These controls move the client reveal cursor
// over the buffered event stream. They do NOT pause, slow, or gate the engine —
// events keep buffering (see `behind`) while the viewer is paused.
//
// The command-centre mini-bar (docs/MOBILE-UI.md §4.2): pause/play, step (while paused), the turn ticks and
// a speed button that cycles. Once the replay has caught up with a finished engine it becomes the bar for
// what to do next.
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
  turn = 0,
  maxTurn = 0,
  ended,
}: Props) {
  if (engineDone && behind === 0 && mode === 'live') {
    return (
      <div className="cc-mini">
        <span className="cc-grow cc-mini-t" style={{ margin: 0 }}>
          ✓ Replay complete
        </span>
        {ended}
      </div>
    )
  }
  const paused = mode === 'paused'
  const idx = SPEEDS.findIndex(([ms]) => ms <= speedMs)
  const cur = idx < 0 ? SPEEDS.length - 1 : idx
  const next = SPEEDS[(cur + 1) % SPEEDS.length]
  const status = paused
    ? behind > 0
      ? `Paused · ${behind} buffered`
      : engineDone
        ? 'Paused · caught up'
        : 'Paused · engine running'
    : engineDone
      ? `Replaying · ${behind} to go`
      : 'Live'
  return (
    <div className="cc-mini">
      <button
        type="button"
        className="cc-ctl"
        onClick={paused ? onResume : onPause}
        aria-label={paused ? 'Play' : 'Pause'}
      >
        <Icon name={paused ? 'play' : 'pause'} size={18} />
      </button>
      <button type="button" className="cc-ctl" onClick={onStep} disabled={!paused} aria-label="Step one event">
        <Icon name="step" size={18} />
      </button>
      <div className="cc-grow">
        {maxTurn > 0 && <Ticks n={Math.min(turn, maxTurn)} max={maxTurn} live={!paused && !engineDone} />}
        <div className="cc-mini-t">
          <span>
            {status} · {SPEEDS[cur][1]}
          </span>
        </div>
      </div>
      {behind > 0 && (
        <button type="button" className="cc-ctl" onClick={onCatchUp} aria-label={`Catch up: show the ${behind} buffered`}>
          »{behind > 99 ? '99+' : behind}
        </button>
      )}
      <button
        type="button"
        className="cc-ctl"
        onClick={() => onSpeed(next[0])}
        aria-label={`Speed ${SPEEDS[cur][1]}, change to ${next[1]}`}
      >
        {SPEEDS[cur][1]}
      </button>
    </div>
  )
}
