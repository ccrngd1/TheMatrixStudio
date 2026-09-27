// SPDX-License-Identifier: Apache-2.0
import { useState } from 'react'
import type { Forecast } from '../api'

interface Props {
  forecast: Forecast | null
  loading: boolean
  error: string | null
}

const money = (n: number) => (n < 10 ? `$${n.toFixed(2)}` : `$${n.toFixed(0)}`)

// What launching this would cost, shown before the button that spends it.
//
// Priced server-side from this account's own finished runs (`matrix_studio/forecast.py`), never
// from a price list — the two estimates that were 3–4× low both scaled a per-token price and
// missed whole kinds of spend. A part with no history is named as "not yet priced" and the total
// becomes "at least": an absent part must not read as a free one.
export function CostForecast({ forecast, loading, error }: Props) {
  const [open, setOpen] = useState(false)

  if (error) {
    return <p className="mt-6 text-xs text-slate-500">Cost forecast unavailable ({error}).</p>
  }
  if (!forecast) {
    return loading ? <p className="mt-6 text-xs text-slate-500">Estimating cost…</p> : null
  }

  const { low, typical, high, complete, unmeasured, budget } = forecast
  const range = low === high ? money(high) : `${money(low)}–${money(high)}`
  const over = budget != null && high > budget.remaining

  return (
    <div className="mt-6 rounded-lg border border-matrix-border bg-matrix-panel p-3 text-sm">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className="text-slate-400">Estimated cost</span>
        {/* Typical first: the range is what every comparable run covered, and early stopping
            alone can make it 4× wide. */}
        <span className="font-semibold text-slate-100" data-testid="forecast-total">
          {complete ? `about ${money(typical)}` : `at least ${money(low)}`}
        </span>
        {complete && low !== high && (
          <span className="text-xs text-slate-500" data-testid="forecast-range">
            past runs like this: {range}
          </span>
        )}
        {forecast.runs > 1 && (
          <span className="text-xs text-slate-500">for all {forecast.runs} conversations</span>
        )}
        {loading && <span className="text-xs text-slate-500">updating…</span>}
        <button
          onClick={() => setOpen((o) => !o)}
          className="ml-auto text-xs text-matrix-accent hover:underline"
          aria-expanded={open}
        >
          {open ? 'Hide breakdown' : 'Breakdown'}
        </button>
      </div>
      {!complete && (
        <p className="mt-1 text-xs text-amber-300">
          Not yet priced: {unmeasured.join(', ')} — no past run to price it from.
        </p>
      )}
      {budget && (
        <p className={`mt-1 text-xs ${over ? 'text-rose-300' : 'text-slate-500'}`}>
          {money(budget.remaining)} left of your {money(budget.cap)} monthly budget
          {over ? ' — this may not finish within it.' : '.'}
        </p>
      )}
      {open && (
        <ul className="mt-2 space-y-1 border-t border-matrix-border pt-2 text-xs">
          {forecast.parts.map((p) => (
            <li key={p.part} className="flex gap-2">
              <span className="w-44 shrink-0 text-slate-300">{p.part}</span>
              <span className="w-24 shrink-0 text-slate-200">
                {p.measured
                  ? p.low === p.high
                    ? money(p.high!)
                    : `${money(p.low!)}–${money(p.high!)}`
                  : 'not yet priced'}
              </span>
              <span className="text-slate-500">{p.basis}</span>
            </li>
          ))}
          <li className="pt-1 text-slate-500">
            From what your own past runs actually cost. No model is called to make this estimate.
          </li>
        </ul>
      )}
    </div>
  )
}
