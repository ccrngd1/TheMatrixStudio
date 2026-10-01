// SPDX-License-Identifier: Apache-2.0
// The boot log (docs/MOBILE-UI.md §5.4, stage 6): the studio mark and a few log lines ticking in over the
// frame on first load, then fading.
//
// It is decoration, so it gives way to everything:
// - once per browser session, and never with FX off, with reduced motion asked for or with `?noboot`;
// - gone within 1.5 s, and any tap or key ends it at once;
// - the app mounts and fetches beneath it from the first frame, so it never delays anything;
// - hidden from screen readers, never focused, and it takes no keys: a key that ends it still reaches the app.
//   A tap that ends it is spent on it rather than landing blind on whatever is under the opaque overlay.
//
// The prototype's last line counted live simulations. The frame does not know that when it mounts, so every
// line here is something the client does know at that moment.
import { useEffect, useState, type CSSProperties } from 'react'
import { parseHash, type Route } from '../lib/route'
import { prefersReducedMotion } from './fx'
import type { Theme } from './theme'

const SEEN_KEY = 'cc.booted'
// Shown, then the 0.45 s fade in command.css, so it is gone within 1.5 s of the frame mounting.
export const BOOT_SHOW_MS = 1000
export const BOOT_FADE_MS = 450

type Css = CSSProperties & Record<`--${string}`, string | number>

const VIEW: Record<Route['name'], string> = {
  runs: 'runs', ensembles: 'ensembles', knowledge: 'knowledge', library: 'library',
  new: 'new run', run: 'run', scrub: 'scrubber', ensemble: 'ensemble',
}

function shouldBoot(fx: boolean): boolean {
  if (!fx || prefersReducedMotion()) return false
  if (new URLSearchParams(window.location.search).has('noboot')) return false
  try {
    return sessionStorage.getItem(SEEN_KEY) === null
  } catch {
    // No storage: "once per session" cannot be kept, so not at all.
    return false
  }
}

export function Boot({ fx, theme }: { fx: boolean; theme: Theme }) {
  // Decided once, on mount: a later FX switch never brings it back.
  const [stage, setStage] = useState<'on' | 'out' | 'gone'>(() => (shouldBoot(fx) ? 'on' : 'gone'))

  useEffect(() => {
    if (!fx) setStage('gone')
  }, [fx])

  useEffect(() => {
    if (stage === 'gone') return
    if (stage === 'out') {
      const t = setTimeout(() => setStage('gone'), BOOT_FADE_MS)
      return () => clearTimeout(t)
    }
    try {
      sessionStorage.setItem(SEEN_KEY, '1')
    } catch {
      // shouldBoot already read it, so storage works; a full quota is not worth more than this.
    }
    const t = setTimeout(() => setStage('out'), BOOT_SHOW_MS)
    const skip = () => setStage('out')
    window.addEventListener('keydown', skip)
    return () => {
      clearTimeout(t)
      window.removeEventListener('keydown', skip)
    }
  }, [stage])

  if (stage === 'gone') return null
  const lines: [string, string][] = [
    ['interface', 'ok'],
    ['theme', theme],
    ['effects', 'on'],
    ['view', VIEW[parseHash(window.location.hash).name]],
  ]
  return (
    <div id="cc-boot" className={stage === 'out' ? 'cc-out' : undefined} aria-hidden="true"
      onPointerDown={() => setStage('out')}>
      <div className="cc-logo">
        MATRIX<span style={{ color: 'var(--accent)' }}>//</span>STUDIO
      </div>
      <pre>
        {lines.map(([k, v], i) => (
          <span key={k} className="cc-bootline" style={{ '--i': i } as Css}>
            {'› '}{`${k} `.padEnd(24, '.')} <b>{v}</b>{'\n'}
          </span>
        ))}
      </pre>
      <div className="cc-bb">
        <i />
      </div>
    </div>
  )
}
