// SPDX-License-Identifier: Apache-2.0
// Search runs over the list already on screen, not a request per keystroke.
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { History, runMatches } from './History'

const run = (id: string, over: object = {}) => ({
  run_id: id, name: id, topic: 'a topic', status: 'complete', turn_count: 3, total_cost_usd: 0.01, created_at: 1, ...over,
})

vi.mock('../api', () => ({ api: { listRuns: vi.fn(), listEnsembles: vi.fn().mockResolvedValue([]) } }))

describe('run search', () => {
  it('matches name, description, topic and cast, case-blind', () => {
    const r = run('x', { name: 'Harbour-Ferry', description: 'fares', topic: 'The toll bridge', cast_names: ['Mina Ostrova'] })
    for (const q of ['harbour', 'FARES', 'toll', 'ostrova']) expect(runMatches(r as any, q)).toBe(true)
    expect(runMatches(r as any, 'nowhere')).toBe(false)
  })

  it('filters on the client without asking the server again', async () => {
    const { api } = await import('../api')
    ;(api.listRuns as any).mockResolvedValue([run('alpha-run'), run('beta-run', { topic: 'ferries' })])
    render(<History onOpen={() => {}} />)
    await waitFor(() => expect(screen.getByText('alpha-run')).toBeInTheDocument())
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'ferr' } })
    expect(screen.queryByText('alpha-run')).not.toBeInTheDocument()
    expect(screen.getByText('beta-run')).toBeInTheDocument()
    await new Promise((r) => setTimeout(r, 400))
    expect(api.listRuns).toHaveBeenCalledTimes(1)
    expect((api.listRuns as any).mock.calls[0][0]).toBeUndefined()
  })

  it('still asks the server once the list is at its cap, where older runs are not loaded', async () => {
    const { api } = await import('../api')
    ;(api.listRuns as any).mockReset()
    ;(api.listRuns as any).mockResolvedValueOnce(Array.from({ length: 200 }, (_, i) => run(`r${i}`)))
    ;(api.listRuns as any).mockResolvedValueOnce([run('ancient-run')])
    render(<History onOpen={() => {}} />)
    await waitFor(() => expect(screen.getByText('r0')).toBeInTheDocument())
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'ancient' } })
    await waitFor(() => expect(screen.getByText('ancient-run')).toBeInTheDocument())
    expect(api.listRuns).toHaveBeenLastCalledWith('ancient')
  })
})
