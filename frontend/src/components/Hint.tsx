// SPDX-License-Identifier: Apache-2.0
/**
 * An ⓘ that explains an option, and why you might turn it on (docs/MOBILE-UI.md §2 rule 6: tap, never hover).
 *
 * - **Tapping opens a sheet** with the explanation. The old version appeared on hover and keyboard focus, and
 *   phones have neither, so every explanation in the app was unreachable on a touch screen.
 * - **The text is always in the DOM**, visually hidden and linked with `aria-describedby`, so a screen reader
 *   announces it without opening anything, find-in-page reaches it, and a test can assert it. It keeps
 *   `role="tooltip"`, which is what nine test files query.
 * - `type="button"`: this sits inside forms, and a bare <button> would submit them.
 * - **The tap target is 44 px** (§7) while the ⓘ keeps the 28 px it always took in the line (Hint.css).
 */
import { useId, useState, type ReactNode } from 'react'
import { Sheet } from '../ui/primitives'
import { Icon } from '../ui/icons'
import './Hint.css'

export function Hint({ children, label }: { children: ReactNode; label?: string }) {
  const [open, setOpen] = useState(false)
  const id = useId()
  const name = label ? `About ${label}` : 'More information'
  return (
    <span className="inline-flex align-middle">
      <button
        type="button"
        aria-label={name}
        aria-describedby={id}
        onClick={(e) => {
          // Inside a <label>, a click would otherwise also toggle the label's checkbox.
          e.preventDefault()
          e.stopPropagation()
          setOpen(true)
        }}
        className="cc-info cc-hint inline-flex items-center justify-center"
      >
        <Icon name="info" size={15} />
      </button>
      <span id={id} role="tooltip" className="sr-only">
        {children}
      </span>
      {open && (
        <Sheet title={label ? `About ${label}` : 'About this'} onClose={() => setOpen(false)}>
          <div className="text-[14px] leading-relaxed" style={{ color: 'var(--t2)' }}>
            {children}
          </div>
        </Sheet>
      )}
    </span>
  )
}
