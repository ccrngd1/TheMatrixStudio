// SPDX-License-Identifier: Apache-2.0
// Backing out of a run remounts the Runs screen. It comes back with the list it had, then refreshes.
import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { History } from './History'

const run = (id: string, name: string) => ({
  run_id: id, name, topic: 't', status: 'complete', turn_count: 3, total_cost_usd: 0.01, created_at: 1,
})

vi.mock('../api', () => ({
  api: { listRuns: vi.fn(), listEnsembles: vi.fn().mockResolvedValue([]) },
}))

describe('the Runs screen on return', () => {
  it('shows the last list at once and swaps in the refreshed one', async () => {
    const { api } = await import('../api')
    ;(api.listRuns as any).mockResolvedValueOnce([run('r1', 'first-name')])
    const first = render(<History onOpen={() => {}} />)
    await waitFor(() => expect(screen.getByText('first-name')).toBeInTheDocument())
    first.unmount()

    let resolve!: (v: unknown) => void
    ;(api.listRuns as any).mockReturnValueOnce(new Promise((r) => (resolve = r)))
    render(<History onOpen={() => {}} />)
    // No spinner and no blank list: the cached rows are there before the request answers.
    expect(screen.getByText('first-name')).toBeInTheDocument()
    expect(screen.queryByText('Loading…')).not.toBeInTheDocument()
    expect(screen.getByText('Refreshing…')).toBeInTheDocument()
    resolve([run('r1', 'first-name'), run('r2', 'second-name')])
    await waitFor(() => expect(screen.getByText('second-name')).toBeInTheDocument())
    expect(screen.queryByText('Refreshing…')).not.toBeInTheDocument()
  })

  it('keeps the rows when the refresh fails, and says so', async () => {
    const { api } = await import('../api')
    ;(api.listRuns as any).mockResolvedValueOnce([run('r1', 'kept-name')])
    render(<History onOpen={() => {}} />).unmount()
    await waitFor(() => expect((api.listRuns as any).mock.calls.length).toBeGreaterThan(0))
    await new Promise((r) => setTimeout(r, 0))
    ;(api.listRuns as any).mockRejectedValueOnce(new Error('503'))
    render(<History onOpen={() => {}} />)
    await waitFor(() => expect(screen.getByText(/refreshing failed: 503/)).toBeInTheDocument())
    expect(screen.getByText('kept-name')).toBeInTheDocument()
  })
})
