// SPDX-License-Identifier: Apache-2.0
/**
 * A small "?" affordance explaining an option, and why you might turn it on.
 *
 * Design notes:
 *
 * - The text lives in the DOM at all times and is hidden visually, rather than
 *   being mounted on hover. That makes it reachable by a screen reader and by
 *   find-in-page, and it makes the content assertable in a test — a tooltip that
 *   only exists during a real mouse hover is a tooltip nothing can verify.
 * - Revealed on hover AND on keyboard focus (`focus-within`), because an option
 *   whose explanation is mouse-only is not explained to everyone.
 * - `type="button"` is deliberate: this sits inside a form, and a bare <button>
 *   would submit it.
 */
import type { ReactNode } from 'react'

export function Hint({ children, label }: { children: ReactNode; label?: string }) {
  return (
    <span className="group relative inline-flex align-middle">
      <button
        type="button"
        aria-label={label ? `About ${label}` : 'More information'}
        className="flex h-4 w-4 items-center justify-center rounded-full border border-matrix-border text-[10px] leading-none text-slate-500 transition-colors hover:border-matrix-accent hover:text-matrix-accent focus:border-matrix-accent focus:text-matrix-accent focus:outline-none"
      >
        ?
      </button>
      <span
        role="tooltip"
        className="pointer-events-none absolute bottom-full left-1/2 z-20 mb-1.5 w-64 -translate-x-1/2 rounded border border-matrix-border bg-matrix-bg p-2 text-left text-[11px] font-normal leading-snug text-slate-300 opacity-0 shadow-lg transition-opacity group-focus-within:opacity-100 group-hover:opacity-100"
      >
        {children}
      </span>
    </span>
  )
}
