// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it, vi } from 'vitest'

vi.mock('../api', () => ({ api: { getSummary: vi.fn() } }))

describe('waitForSummary', () => {
  it('polls until a summary other than the one it started with exists', async () => {
    const { api } = await import('../api')
    const { waitForSummary } = await import('./SummaryPanel')
    const old = { id: 1, payload: { overview: 'old' } }
    const fresh = { id: 2, payload: { overview: 'new' } }
    ;(api.getSummary as any)
      .mockResolvedValueOnce({ generated: old })
      .mockRejectedValueOnce(new Error('blip'))
      .mockResolvedValueOnce({ generated: fresh })
    expect(await waitForSummary('r1', 1, { everyMs: 1, forMs: 1000 })).toEqual(fresh)
  })

  it('gives up with null when nothing new arrives', async () => {
    const { api } = await import('../api')
    const { waitForSummary } = await import('./SummaryPanel')
    ;(api.getSummary as any).mockResolvedValue({ generated: null })
    expect(await waitForSummary('r1', null, { everyMs: 1, forMs: 20 })).toBeNull()
  })
})
