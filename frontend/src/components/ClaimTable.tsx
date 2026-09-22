// SPDX-License-Identifier: Apache-2.0
// The per-cell claim table: one row per demand or refusal, one column per group.
//
// This component exists to make one mistake impossible to render. `docs/ENSEMBLE-CONVERSATIONS.md`
// §4: the same "5 of 9" is a strong method-dependent finding or a coin flip depending on how
// it splits across groups, and a single pooled number cannot tell them apart. So there is no
// total column here and no overall percentage — deliberately, not as an omission. A reader who
// wants "how many runs in all" would be reading the one number that destroys the distinction
// the table is for.
//
// Two display decisions carry the same weight:
//
//   * A group that produced no usable extraction renders as "—", never as "0 of N". An empty
//     group has no opinion; 0-of-N says every run in it declined the claim. Those are
//     different facts and the API distinguishes them with `null`, so this must too.
//   * `rare` is labelled "1 run only" rather than styled as a weak version of `split`. The
//     boundary is replication, not magnitude: `rare` means nothing reproduced it, which is a
//     claim any group size supports, where "weak" invites a comparison that five runs cannot
//     support.

import type { EnsembleClaim } from '../api'

interface Props {
  claims: EnsembleClaim[]
  /** Group labels in the order the spec declared them, so `base` stays leftmost. */
  cells: string[]
}

const TIER_STYLE: Record<string, string> = {
  unanimous: 'bg-emerald-900/40 text-emerald-200 border-emerald-700/50',
  split: 'bg-amber-900/40 text-amber-200 border-amber-700/50',
  rare: 'bg-slate-800 text-slate-400 border-slate-600/50',
  absent: 'bg-transparent text-slate-600 border-transparent',
}

const TIER_LABEL: Record<string, string> = {
  unanimous: 'every run',
  split: 'some runs',
  rare: '1 run only',
  absent: 'none',
}

export function ClaimTable({ claims, cells }: Props) {
  if (claims.length === 0) {
    return (
      <p className="text-sm text-slate-500">
        No demands or refusals were extracted from these conversations.
      </p>
    )
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-matrix-border text-left text-xs uppercase tracking-wide text-slate-500">
            <th className="py-2 pr-4 font-medium">Claim</th>
            {cells.map((cell) => (
              <th key={cell} className="py-2 pr-4 font-medium">
                {cell}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {claims.map((claim, i) => (
            <tr key={`${claim.claim}-${i}`} className="border-b border-matrix-border/40 align-top">
              <td className="py-2 pr-4 text-slate-200">
                {claim.claim}
                <span className="ml-2 text-[11px] uppercase tracking-wide text-slate-500">
                  {claim.kind}
                </span>
              </td>
              {cells.map((cell) => {
                const at = claim.per_cell[cell]
                if (!at) {
                  return (
                    <td key={cell} className="py-2 pr-4 text-slate-600" title="This group produced no usable conversation, so it has no opinion on this claim.">
                      —
                    </td>
                  )
                }
                return (
                  <td key={cell} className="py-2 pr-4">
                    <span
                      className={`inline-block rounded border px-2 py-0.5 text-[11px] ${
                        TIER_STYLE[at.tier] ?? TIER_STYLE.absent
                      }`}
                      title={at.runs.length ? at.runs.join(', ') : 'No run in this group held it.'}
                    >
                      {at.held} of {at.of} · {TIER_LABEL[at.tier] ?? at.tier}
                    </span>
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
