// SPDX-License-Identifier: Apache-2.0
import { Hint } from './Hint'

export interface DraftAssumption {
  statement: string
  basis: string
}

export const MAX_ASSUMPTIONS = 8

/** The assumptions as `config.assumptions` takes them: stated ones only. */
export function assumptionsConfig(list: DraftAssumption[]) {
  return list
    .filter((a) => a.statement.trim())
    .map((a) => ({ statement: a.statement.trim(), ...(a.basis.trim() ? { basis: a.basis.trim() } : {}) }))
}

interface Props {
  assumptions: DraftAssumption[]
  onChange: (list: DraftAssumption[]) => void
}

// Working assumptions (matrix_studio/assumptions.py): what the room reasons from when nobody in it can
// know the answer. Shown to every persona and marked in the transcript; never treated as evidence.
export function AssumptionsEditor({ assumptions, onChange }: Props) {
  const update = (i: number, patch: Partial<DraftAssumption>) =>
    onChange(assumptions.map((a, n) => (n === i ? { ...a, ...patch } : a)))
  const input = 'rounded border border-matrix-border bg-matrix-bg p-2 text-sm'

  return (
    <div className="mt-6 rounded-lg border border-matrix-border bg-matrix-panel p-4">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="flex items-center gap-1 text-sm font-semibold text-slate-300">
          Working assumptions
          <Hint label="working assumptions">
            Things the room should reason from because nobody in it can know them — "assume pilot
            churn is about 7%". Without them a persona that needs the number can only say "show me",
            every turn. Each is shown to every persona, marked in the conversation, and listed in the
            brief so a reader knows which conclusions rest on it.
            <br />
            <br />
            An assumption is not evidence: it changes what a persona would do <em>if it holds</em>,
            not what it is convinced of, and a persona may dispute it. To see what one was worth,
            start over with this setup and change it.
          </Hint>
        </h2>
        <button
          onClick={() => onChange([...assumptions, { statement: '', basis: '' }])}
          disabled={assumptions.length >= MAX_ASSUMPTIONS}
          className="rounded border border-matrix-border px-2 py-1 text-xs hover:border-matrix-accent disabled:opacity-50"
        >
          + Add assumption
        </button>
      </div>
      {assumptions.length === 0 ? (
        <p className="text-xs text-slate-500">None. Personas reason only from what they know.</p>
      ) : (
        <div className="space-y-2">
          {assumptions.map((a, i) => (
            <div key={i} className="flex gap-2">
              <span className="pt-2 text-xs text-slate-500">A{i + 1}</span>
              <input
                value={a.statement}
                onChange={(e) => update(i, { statement: e.target.value })}
                placeholder="Assume… e.g. the pilot's churn is about 7%"
                maxLength={300}
                aria-label={`Assumption ${i + 1}`}
                className={`min-w-0 flex-[2] ${input}`}
              />
              <input
                value={a.basis}
                onChange={(e) => update(i, { basis: e.target.value })}
                placeholder="Basis (optional)"
                maxLength={300}
                aria-label={`Basis for assumption ${i + 1}`}
                className={`min-w-0 flex-1 ${input}`}
              />
              <button
                onClick={() => onChange(assumptions.filter((_, n) => n !== i))}
                className="px-2 text-slate-500 hover:text-rose-300"
                aria-label={`Remove assumption ${i + 1}`}
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
