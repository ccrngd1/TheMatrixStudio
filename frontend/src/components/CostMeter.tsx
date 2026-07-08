// SPDX-License-Identifier: Apache-2.0
interface Props {
  totalCost: number
  tokensIn: number
  tokensOut: number
  // Optional soft display threshold — shows a warning banner but never halts a
  // run (spend caps are Phase 3).
  warnThreshold?: number
}

export function CostMeter({ totalCost, tokensIn, tokensOut, warnThreshold = 1.0 }: Props) {
  const over = totalCost >= warnThreshold
  return (
    <div className="rounded-lg border border-matrix-border bg-matrix-panel p-3">
      <div className="flex items-baseline justify-between">
        <span className="text-xs uppercase tracking-wide text-slate-400">Run cost</span>
        <span className={`text-lg font-bold ${over ? 'text-amber-400' : 'text-matrix-accent'}`}>
          ${totalCost.toFixed(4)}
        </span>
      </div>
      <div className="mt-1 flex justify-between text-[11px] text-slate-500">
        <span>{tokensIn.toLocaleString()} in</span>
        <span>{tokensOut.toLocaleString()} out</span>
        <span>{(tokensIn + tokensOut).toLocaleString()} total</span>
      </div>
      {over && (
        <p className="mt-2 text-[11px] text-amber-400">
          ⚠ Past ${warnThreshold.toFixed(2)} display threshold (no cap enforced).
        </p>
      )}
    </div>
  )
}
