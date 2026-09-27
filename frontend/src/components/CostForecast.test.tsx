// SPDX-License-Identifier: Apache-2.0
//
// The forecast's pricing lives server-side (matrix_studio/forecast.py). What this pins is that the
// panel never lets an unpriced part read as free, and says so when the budget would not cover it.
import { describe, expect, it } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { CostForecast } from './CostForecast'
import type { Forecast } from '../api'

const f = (over: Partial<Forecast> = {}): Forecast => ({
  runs: 1,
  parts: [
    { part: 'conversation', low: 0.8, typical: 0.9, high: 1.0, measured: true, basis: '40 responses × …' },
    { part: 'avatars', low: 0.48, typical: 0.48, high: 0.48, measured: true, basis: '6 image(s) × $0.08' },
  ],
  low: 1.28,
  typical: 1.38,
  high: 1.48,
  complete: true,
  unmeasured: [],
  budget: null,
  ...over,
})

describe('CostForecast', () => {
  it('leads with the typical cost, and shows the range past runs covered', () => {
    render(<CostForecast forecast={f()} loading={false} error={null} />)
    expect(screen.getByTestId('forecast-total')).toHaveTextContent('about $1.38')
    expect(screen.getByTestId('forecast-range')).toHaveTextContent('$1.28–$1.48')
  })

  it('says "at least" and names what is unpriced, rather than a total that looks complete', () => {
    render(<CostForecast forecast={f({ complete: false, unmeasured: ['conversation'], low: 0.48, typical: 0.48, high: 0.48 })}
      loading={false} error={null} />)
    expect(screen.getByTestId('forecast-total')).toHaveTextContent('at least $0.48')
    expect(screen.getByText(/Not yet priced: conversation/)).toBeInTheDocument()
  })

  it('warns when the high end exceeds what is left of the monthly budget', () => {
    render(<CostForecast forecast={f({ budget: { cap: 10, spent: 9, remaining: 1 } })}
      loading={false} error={null} />)
    expect(screen.getByText(/this may not finish within it/)).toBeInTheDocument()
  })

  it('shows each part and where its number came from', () => {
    render(<CostForecast forecast={f()} loading={false} error={null} />)
    fireEvent.click(screen.getByText('Breakdown'))
    expect(screen.getByText('avatars')).toBeInTheDocument()
    expect(screen.getByText('6 image(s) × $0.08')).toBeInTheDocument()
  })

  it('says it is unavailable instead of disappearing', () => {
    render(<CostForecast forecast={null} loading={false} error="404: Not Found" />)
    expect(screen.getByText(/Cost forecast unavailable/)).toBeInTheDocument()
  })
})
