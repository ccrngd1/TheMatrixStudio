// SPDX-License-Identifier: Apache-2.0
// The new-run wizard's frame (docs/MOBILE-UI.md §4.9): five steps on a diamond stepper, each tappable, and a
// Launch page that reads every choice back with an Edit beside it.
//
// Every step stays MOUNTED; the ones not being shown are hidden with CSS. The form is one body of state, so
// nothing has to be lifted or re-read when you move between steps, and jumping back to fix one field cannot
// lose another. Hidden with `display:none`, so a hidden step is out of the tab order and the accessibility
// tree too.
import type { ReactNode } from 'react'

export const WIZARD_STEPS = ['Topic', 'Cast', 'Knowledge', 'Assume', 'Launch'] as const
export type WizardStep = 1 | 2 | 3 | 4 | 5

export function Stepper({ step, onStep }: { step: WizardStep; onStep: (s: WizardStep) => void }) {
  return (
    <nav className="cc-stepper" aria-label="New run steps">
      {WIZARD_STEPS.map((label, i) => {
        const n = (i + 1) as WizardStep
        return (
          <button
            key={label}
            type="button"
            className={`cc-sn ${n < step ? 'cc-done' : n === step ? 'cc-cur' : ''}`}
            aria-current={n === step ? 'step' : undefined}
            onClick={() => onStep(n)}
          >
            <i aria-hidden="true" />
            {label}
          </button>
        )
      })}
    </nav>
  )
}

/** One step's content. Kept mounted when not shown (see the file comment). */
export function Step({ n, step, children }: { n: WizardStep; step: WizardStep; children: ReactNode }) {
  return (
    <section
      className={n === step ? 'cc-step' : 'cc-step cc-step-off'}
      aria-label={WIZARD_STEPS[n - 1]}
      data-step={n}
    >
      {children}
    </section>
  )
}

export interface ReviewRow {
  label: string
  value: ReactNode
  step: WizardStep
}

/** Step 5: every choice on one page, grouped by the step it was made on. */
export function LaunchReview({ rows, onEdit }: { rows: ReviewRow[]; onEdit: (s: WizardStep) => void }) {
  return (
    <div className="flex flex-col gap-2">
      {([1, 2, 3, 4] as WizardStep[]).map((s) => {
        const mine = rows.filter((r) => r.step === s)
        if (mine.length === 0) return null
        return (
          <div key={s} className="cc-card">
            <div className="flex items-center justify-between">
              <span className="cc-label">{WIZARD_STEPS[s - 1]}</span>
              <button type="button" className="cc-btn cc-sm" onClick={() => onEdit(s)}>
                Edit
              </button>
            </div>
            <dl className="mt-1.5 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
              {mine.map((r) => (
                <div key={r.label} className="contents">
                  <dt className="cc-muted">{r.label}</dt>
                  <dd className="min-w-0 break-words">{r.value}</dd>
                </div>
              ))}
            </dl>
          </div>
        )
      })}
    </div>
  )
}
