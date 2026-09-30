// SPDX-License-Identifier: Apache-2.0
// Opens a run in the 8-bit theatre, in a new tab.
//
// Two forms, one rule. `icon` sits in the run header beside ⋯, visible on every run so the
// theatre can be found; `menu` is the entry in Run options, which also says why it is off.
// Both are disabled while the run is still going: the theatre replays a finished transcript and
// has no live mode, so offering it mid-run would open a page that can only say "not yet".
//
// `window.open` rather than a target=_blank link, and without `noopener`, is deliberate. The
// sign-in tokens live in sessionStorage, and a new tab only starts with a copy of the opener's
// sessionStorage when it has an opener. Modern browsers give a target=_blank link `noopener` by
// default, so a plain link would open the theatre signed out.

interface Props {
  runId: string
  running: boolean
  /** Nothing to replay yet: the transcript has no lines. */
  empty: boolean
  variant?: 'icon' | 'menu'
  /** Called after the tab is opened, e.g. to close the sheet the entry sits in. */
  onOpened?: () => void
}

export function theatreUrl(runId: string): string {
  return `/theatre.html?run=${encodeURIComponent(runId)}`
}

export const THEATRE_LABEL = 'Replay in the 8-bit theatre'

function reasonOff(running: boolean, empty: boolean): string | null {
  if (running) return 'Available when the run ends: the theatre replays a finished transcript'
  if (empty) return 'Nothing to replay: this run has no transcript'
  return null
}

/** A space invader, drawn on the icon set's 24-unit grid. */
function Invader({ size = 18 }: { size?: number }) {
  // 11 × 8 pixels at 2 units each, one run of pixels per rect.
  const rows = ['00100000100', '00010001000', '00111111100', '01101110110', '11111111111', '10111111101', '10100000101', '00011011000']
  const rects: JSX.Element[] = []
  rows.forEach((row, y) => {
    for (let x = 0; x < row.length; x++) {
      if (row[x] !== '1') continue
      let w = 1
      while (row[x + w] === '1') w++
      rects.push(<rect key={`${x},${y}`} x={1 + 2 * x} y={4 + 2 * y} width={2 * w} height={2} />)
      x += w - 1
    }
  })
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden style={{ flex: 'none' }}>
      {rects}
    </svg>
  )
}

export function TheatreButton({ runId, running, empty, variant = 'icon', onOpened }: Props) {
  const off = reasonOff(running, empty)
  const open = () => {
    window.open(theatreUrl(runId), '_blank')
    onOpened?.()
  }
  if (variant === 'menu') {
    return (
      <button type="button" onClick={open} disabled={!!off} style={off ? { opacity: 0.45, cursor: 'not-allowed' } : undefined}>
        <Invader />
        <span className="flex min-w-0 flex-col">
          <span>{THEATRE_LABEL} ↗</span>
          {off && <span className="text-xs text-cc-t3">{off}</span>}
        </span>
      </button>
    )
  }
  return (
    <button
      type="button"
      className="cc-icon"
      onClick={open}
      disabled={!!off}
      aria-label={`${THEATRE_LABEL} (opens a new tab)`}
      aria-description={off ?? undefined}
      style={off ? { opacity: 0.35, cursor: 'not-allowed' } : undefined}
    >
      <Invader size={20} />
    </button>
  )
}
