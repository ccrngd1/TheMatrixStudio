// SPDX-License-Identifier: Apache-2.0
import type { EnsembleClaim, EnsembleReport } from '../api'
import { ClaimTable } from './ClaimTable'

interface Props {
  report: EnsembleReport
  cells: string[]
}

// What the runs concluded, and what every run in a group agreed on — the answer to "so what did the
// room decide, and how consistently?", placed above the full claim table so it is read first.
//
// Four rules, from docs/BACKLOG.md and ENSEMBLE-CONVERSATIONS.md §4:
//
//   - Per group, never pooled. Conclusions reuse ClaimTable, so they get its one-column-per-group,
//     no-total layout rather than a second implementation of it.
//   - Counts, not adjectives. Nothing here says "most runs": the table says 4 of 5.
//   - Labelled as model analysis — extraction and clustering are model calls.
//   - Divergence is a result. If no conclusion recurs, that is said plainly rather than the most
//     common conclusion being promoted to "the conclusion".
export function ConclusionsPanel({ report, cells }: Props) {
  if (report.conclusions === undefined) {
    // A report built before conclusions existed. NOT the same as "no run concluded anything",
    // which is a finding — so the two must not render alike.
    return (
      <p className="text-xs text-slate-500">
        This report was built before conclusions were extracted. Regenerate it to see what the
        runs concluded.
      </p>
    )
  }

  const recurring = report.conclusions.filter((c) =>
    Object.values(c.per_cell).some((cell) => (cell?.held ?? 0) >= 2),
  )
  const single = report.conclusions.filter((c) => !recurring.includes(c))
  // Agreements: claims every usable run in some group held. Derived from the claim table rather
  // than stored, so the two cannot disagree.
  const agreed: { claim: EnsembleClaim; cells: string[] }[] = []
  for (const claim of report.claims) {
    const inCells = cells.filter((c) => claim.per_cell[c]?.tier === 'unanimous')
    if (inCells.length) agreed.push({ claim, cells: inCells })
  }

  return (
    <div className="space-y-4">
      <p className="text-xs italic text-amber-300/80">
        Model-generated analysis: each run's conclusions were extracted from its transcript and
        grouped across runs. It can be wrong — the conversations are linked above.
      </p>

      <div>
        <h3 className="mb-2 text-sm text-slate-300">What the runs concluded</h3>
        {report.conclusions.length === 0 ? (
          <p className="text-sm text-slate-400">
            No run reached a conclusion. Every conversation ended without the group deciding
            anything — which is itself the finding.
          </p>
        ) : (
          <>
            {recurring.length === 0 && (
              <p className="mb-2 text-sm text-amber-300">
                No conclusion recurred: no two runs in any group concluded the same thing. The runs
                diverged, and that divergence is the result — none of the conclusions below is
                "the" conclusion.
              </p>
            )}
            {recurring.length > 0 && (
              <>
                <p className="mb-1 text-xs text-slate-500">Reached in two or more runs of a group</p>
                <ClaimTable claims={recurring} cells={cells} clustered={report.clustered} />
              </>
            )}
            {/* Collapsed by default. Measured on renewal-cells, 35 of 41 conclusions came from a
                single run, and shown in full they buried the six that recurred. A one-run
                conclusion is `rare` — not a finding — but it is still listed, so nothing is hidden. */}
            {single.length > 0 && (
              <details className="mt-3">
                <summary className="cursor-pointer text-xs text-slate-500">
                  Reached in a single run only ({single.length}) — rare, not a finding
                </summary>
                <div className="mt-2">
                  <ClaimTable claims={single} cells={cells} clustered={report.clustered} />
                </div>
              </details>
            )}
          </>
        )}
      </div>

      <div>
        <h3 className="mb-2 text-sm text-slate-300">What every run in a group agreed on</h3>
        {agreed.length === 0 ? (
          <p className="text-sm text-slate-400">
            No demand or refusal was held in every run of any group.
          </p>
        ) : (
          <ul className="space-y-1 text-sm text-slate-300">
            {agreed.map(({ claim, cells: inCells }) => (
              <li key={`${claim.kind}:${claim.claim}`}>
                <span className="text-slate-500">[{claim.kind}]</span> {claim.claim}{' '}
                <span className="text-xs text-slate-500">
                  —{' '}
                  {inCells
                    .map((c) => `${c}: all ${claim.per_cell[c]?.of} runs`)
                    .join(', ')}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}
