// SPDX-License-Identifier: Apache-2.0
//
// The Library tab: every saved cast and every archetype starts a run from itself, on the wizard's Cast step,
// and the route it asks for is one the app can parse back to the same thing.
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { Library } from './Library'
import { api } from '../api'
import { hrefOf, parseHash } from '../lib/route'

vi.mock('../api', () => ({ api: { listPersonaPacks: vi.fn(), listCastTemplates: vi.fn() } }))
const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>

describe('Library', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocked.listCastTemplates.mockResolvedValue([
      { name: 'Q3 & Q4 review', description: null, personas: ['Robin', 'Quinn'], updated_at: 1 },
      { name: 'Pricing panel', description: 'Two sides of the new tier', personas: ['Sam'], updated_at: 2 },
    ])
    mocked.listPersonaPacks.mockResolvedValue([
      { id: 'test-sceptic', label: 'Sceptic', summary: 'Doubts every claim.', qualification: 'not yet qualified', persona: {} },
    ])
  })

  it('starts a run with the chosen saved cast, on the Cast step', async () => {
    const onNavigate = vi.fn()
    render(<Library onNavigate={onNavigate} />)
    // One button per card; the accessible name says which cast it is for. (`\s*`: jsdom puts a space at the
    // visually hidden span's edge where a browser does not.)
    fireEvent.click(await screen.findByRole('button', { name: /^Start a run with this cast\s*: Pricing panel$/ }))
    expect(onNavigate).toHaveBeenCalledWith({ name: 'new', step: 2, castTemplate: 'Pricing panel' })
  })

  it('carries a cast name with URL-unsafe characters through the route intact', async () => {
    const onNavigate = vi.fn()
    render(<Library onNavigate={onNavigate} />)
    fireEvent.click(await screen.findByRole('button', { name: /^Start a run with this cast\s*: Q3 & Q4 review$/ }))
    const [route] = onNavigate.mock.calls[0]
    expect(parseHash(hrefOf(route))).toEqual({ name: 'new', step: 2, castTemplate: 'Q3 & Q4 review' })
  })

  it('adds the chosen archetype to a new run, on the Cast step', async () => {
    const onNavigate = vi.fn()
    render(<Library onNavigate={onNavigate} />)
    fireEvent.click(await screen.findByRole('button', { name: /^Add to a new run\s*: Sceptic$/ }))
    expect(onNavigate).toHaveBeenCalledWith({ name: 'new', step: 2, packId: 'test-sceptic' })
  })

  it('names every saved cast’s personas, and each archetype’s persona, with the simulated-persona marker', async () => {
    mocked.listPersonaPacks.mockResolvedValue([
      { id: 'test-sceptic', label: 'Sceptic', summary: 'Doubts every claim.', qualification: 'not yet qualified',
        persona: { name: 'Mina', persona: 'A sceptic.', goals: [] } },
    ])
    render(<Library onNavigate={vi.fn()} />)
    for (const name of ['Robin', 'Quinn', 'Sam', 'Mina']) {
      const el = await screen.findByText(name, { selector: '.cc-pname' })
      expect(within(el).getByRole('img', { name: 'simulated persona' })).toBeInTheDocument()
    }
  })

  it('still offers a blank run', async () => {
    const onNavigate = vi.fn()
    render(<Library onNavigate={onNavigate} />)
    await screen.findByText('Pricing panel')
    fireEvent.click(screen.getByRole('button', { name: /Start a blank run/ }))
    expect(onNavigate).toHaveBeenCalledWith({ name: 'new' })
  })

  it('shows both lists empty, not broken, when the routes are missing', async () => {
    mocked.listCastTemplates.mockRejectedValue(new Error('404'))
    mocked.listPersonaPacks.mockRejectedValue(new Error('404'))
    render(<Library onNavigate={vi.fn()} />)
    expect(await screen.findByText(/None saved yet/)).toBeInTheDocument()
    expect(await screen.findByText(/No archetypes on this deployment/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Start a run with this cast|Add to a new run/ })).toBeNull()
  })
})
