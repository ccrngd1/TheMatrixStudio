// SPDX-License-Identifier: Apache-2.0
/**
 * The new-run wizard opened from the Library tab: with a saved cast loaded, or an archetype added.
 *
 * What these pin: the item arrives in the cast intact and the wizard opens on the Cast step where it can be
 * seen; a banner says where it came from; and a cast or archetype that has gone since the Library was drawn
 * is reported, not shown as a blank cast that looks like the load worked.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { NewRunForm } from './NewRunForm'
import { api } from '../api'

vi.mock('../api', () => ({
  api: {
    listKnowledgeBases: vi.fn().mockResolvedValue({ knowledge_bases: [], count: 0 }),
    getDocumentFormats: vi.fn().mockResolvedValue({
      formats: [{ suffix: '.txt', media_type: 'txt', available: true, needs: null }],
      max_upload_bytes: 10485760,
      max_document_chars: 400000,
    }),
    extractDocument: vi.fn(),
    createRun: vi.fn().mockResolvedValue({ run_id: 'new-run' }),
    getModels: vi.fn().mockResolvedValue({ models: [] }),
    getRunSetup: vi.fn(),
    suggestPersonas: vi.fn(),
    suggestName: vi.fn().mockResolvedValue({ name: 'x', description: 'y' }),
    listCastTemplates: vi.fn(),
    getCastTemplate: vi.fn(),
    listPersonaPacks: vi.fn(),
  },
}))
const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>

const TEMPLATE = {
  name: 'Pricing panel',
  description: null,
  cast: [
    {
      name: 'Robin',
      persona: 'Runs the support desk.',
      goals: ['Fewer tickets'],
      structured: { viewpoints: [{ position: 'Keep it simple', firmness: 'firm', evidence_that_shifts: ['ticket data'] }] },
      knowledge_bases: ['kb-support'],
    },
    { name: 'Quinn', persona: 'Owns the roadmap.', goals: ['Ship the new tier'] },
  ],
  created_at: 1,
  updated_at: 1,
}

const PACK = {
  id: 'test-sceptic',
  label: 'Sceptic',
  summary: 'Doubts every claim.',
  qualification: 'not yet qualified',
  persona: { name: 'Quinn', persona: 'Asks for the evidence first.', goals: ['Find the weak claim'] },
}

const form = (props: { castTemplate?: string; packId?: string }) =>
  render(<NewRunForm onStarted={() => {}} onEnsembleStarted={() => {}} onCancel={() => {}} {...props} />)

const currentStep = () => screen.getByRole('button', { current: 'step' })

describe('NewRunForm opened from the Library', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocked.listCastTemplates.mockResolvedValue([])
    mocked.listPersonaPacks.mockResolvedValue([PACK])
    mocked.getCastTemplate.mockResolvedValue(TEMPLATE)
  })

  it('loads a saved cast, convictions and bindings included, and opens on the Cast step', async () => {
    form({ castTemplate: 'Pricing panel' })
    await waitFor(() => expect(screen.getByDisplayValue('Robin')).toBeInTheDocument())
    expect(mocked.getCastTemplate).toHaveBeenCalledWith('Pricing panel')
    expect(screen.getByDisplayValue('Quinn')).toBeInTheDocument()
    expect(screen.getByDisplayValue('[firm] Keep it simple -> ticket data')).toBeInTheDocument()
    // The template replaces the starting row rather than sitting under an empty one.
    expect(screen.getAllByPlaceholderText('Name')).toHaveLength(2)
    expect(currentStep()).toHaveTextContent('Cast')
    const banner = screen.getByText(/Loaded the saved cast/)
    expect(banner).toHaveTextContent('Pricing panel')
    expect(banner).toHaveTextContent('2 personas')
  })

  it('submits the loaded cast with its knowledge-base bindings', async () => {
    form({ castTemplate: 'Pricing panel' })
    await waitFor(() => expect(screen.getByDisplayValue('Robin')).toBeInTheDocument())
    fireEvent.change(screen.getByPlaceholderText('What should the cast discuss?'), {
      target: { value: 'Should the new tier launch this month?' },
    })
    fireEvent.click(screen.getByRole('button', { name: /run simulation/i }))
    await waitFor(() => expect(mocked.createRun).toHaveBeenCalledTimes(1))
    const body = mocked.createRun.mock.calls[0][0]
    expect(body.cast.map((c: { name: string }) => c.name)).toEqual(['Robin', 'Quinn'])
    expect(body.cast[0].knowledge_bases).toEqual(['kb-support'])
  })

  it('adds an archetype in place of the empty starting row, and says it is not yet qualified', async () => {
    form({ packId: 'test-sceptic' })
    await waitFor(() => expect(screen.getByDisplayValue('Asks for the evidence first.')).toBeInTheDocument())
    expect(screen.getAllByPlaceholderText('Name')).toHaveLength(1)
    expect(screen.getByDisplayValue('Quinn')).toBeInTheDocument()
    expect(currentStep()).toHaveTextContent('Cast')
    expect(screen.getByText(/Added the archetype/)).toHaveTextContent('Sceptic (not yet qualified)')
    expect(mocked.getCastTemplate).not.toHaveBeenCalled()
  })

  it('renames an archetype that would collide with someone in the loaded cast', async () => {
    form({ castTemplate: 'Pricing panel', packId: 'test-sceptic' })
    await waitFor(() => expect(screen.getByDisplayValue('Quinn 2')).toBeInTheDocument())
    expect(screen.getAllByPlaceholderText('Name').map((i) => (i as HTMLInputElement).value)).toEqual([
      'Robin',
      'Quinn',
      'Quinn 2',
    ])
  })

  it('reports a saved cast that no longer exists instead of showing a blank cast', async () => {
    mocked.getCastTemplate.mockRejectedValue(new Error("404: No template named 'Pricing panel'"))
    form({ castTemplate: 'Pricing panel' })
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/saved cast “Pricing panel” is no longer in the library/)
    expect(screen.queryByText(/Loaded the saved cast/)).toBeNull()
    expect(screen.queryByText(/Loading from the library/)).toBeNull()
    // The form is still usable: one empty row to start from.
    expect(screen.getAllByPlaceholderText('Name')).toHaveLength(1)
  })

  it('reports any other failure to load a saved cast with its reason', async () => {
    mocked.getCastTemplate.mockRejectedValue(new Error('500: Internal Server Error'))
    form({ castTemplate: 'Pricing panel' })
    expect(await screen.findByRole('alert')).toHaveTextContent(/Could not load the saved cast “Pricing panel”: 500/)
  })

  it('reports an archetype that is no longer in the library', async () => {
    form({ packId: 'retired-pack' })
    expect(await screen.findByRole('alert')).toHaveTextContent(/archetype “retired-pack” is no longer in the library/)
    expect(screen.queryByText(/Added the archetype/)).toBeNull()
  })

  it('is an ordinary blank form on step 1 when opened with neither', async () => {
    form({})
    await waitFor(() => expect(mocked.getModels).toHaveBeenCalled())
    expect(mocked.getCastTemplate).not.toHaveBeenCalled()
    expect(currentStep()).toHaveTextContent('Topic')
    expect(screen.queryByText(/from the library/)).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
