// SPDX-License-Identifier: Apache-2.0
//
// The persona library: every choice is labelled with its qualification, an added pack arrives as an
// editable draft with its convictions intact, and it never collides with a name already in the cast.
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { PersonaLibrary } from './PersonaLibrary'
import { api } from '../api'

vi.mock('../api', () => ({ api: { listPersonaPacks: vi.fn() } }))
const mocked = api as unknown as { listPersonaPacks: ReturnType<typeof vi.fn> }

const PACK = {
  id: 'finance-lead', label: 'Finance lead', summary: 's', qualification: 'not yet qualified',
  persona: {
    name: 'Hana', persona: 'A finance lead.', goals: ['Measure first'],
    structured: { viewpoints: [{ position: 'Measure it.', firmness: 'firm', evidence_that_shifts: ['a measurement'] }] },
  },
}

describe('PersonaLibrary', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocked.listPersonaPacks.mockResolvedValue([PACK])
  })

  it('labels every choice with its qualification', async () => {
    render(<PersonaLibrary taken={[]} onAdd={vi.fn()} />)
    expect(await screen.findByText('Finance lead (not yet qualified)')).toBeInTheDocument()
  })

  it('adds the pack as a draft with its position, firmness and exit condition', async () => {
    const onAdd = vi.fn()
    render(<PersonaLibrary taken={[]} onAdd={onAdd} />)
    fireEvent.change(await screen.findByLabelText('Add a persona from the library'), { target: { value: 'finance-lead' } })
    const [draft] = onAdd.mock.calls[0]
    expect(draft).toMatchObject({ name: 'Hana', goals: 'Measure first', positions: '[firm] Measure it. -> a measurement' })
  })

  it('renames rather than colliding with a persona already in the cast', async () => {
    const onAdd = vi.fn()
    render(<PersonaLibrary taken={['hana', 'Hana 2']} onAdd={onAdd} />)
    fireEvent.change(await screen.findByLabelText('Add a persona from the library'), { target: { value: 'finance-lead' } })
    expect(onAdd.mock.calls[0][0].name).toBe('Hana 3')
  })

  it('shows nothing when the library cannot be read', async () => {
    mocked.listPersonaPacks.mockRejectedValue(new Error('404'))
    const { container } = render(<PersonaLibrary taken={[]} onAdd={vi.fn()} />)
    await waitFor(() => expect(mocked.listPersonaPacks).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()
  })
})
