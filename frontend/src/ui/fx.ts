// SPDX-License-Identifier: Apache-2.0
// Whether decoration may move (docs/MOBILE-UI.md §2 rule 5, §5.4). The frame provides the FX switch, and the
// one-shot effects (boot, title decode, type-in) ask `useMotion()` before they start. The CSS loops need no
// such check: `.cc-nofx` and the reduced-motion media query in command.css already stop them.
//
// The default is OFF, so anything rendered outside the frame, every unit test included, renders still.
import { createContext, useContext, useState } from 'react'
import { phase } from './theme'

export const FxContext = createContext(false)

/**
 * The loop phase (§5.4) for one element, taken when it mounts and then kept.
 *
 * `phase()` read on every render does the opposite of what it is for once the element persists, as React's
 * do: it changes `animation-delay` under a loop that is already running, and the browser moves a running
 * animation to match its new delay, so the loop jumps ahead by the time since it mounted on every re-render
 * (measured in Chromium). Taken once, the loop starts in step with the others and is never moved.
 */
export function usePhase(): string {
  const [ph] = useState(phase)
  return ph
}

export function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
}

/** True when FX is on and the reader has not asked for reduced motion. */
export function useMotion(): boolean {
  return useContext(FxContext) && !prefersReducedMotion()
}
