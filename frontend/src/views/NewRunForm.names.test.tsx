// SPDX-License-Identifier: Apache-2.0
/**
 * Real people's names in the new-run form (`lib/realNames.ts`) and the simulated-persona marker on its cast.
 *
 * What these pin: a real public figure's name is switched on blur, never mid-typing, with the reason on that
 * persona; the notice goes once the user picks another name, and the real one is switched again if retyped; a
 * cast filled wholesale is checked without anyone typing; the wizard's own renames carry the same notice; a check
 * that fails changes nothing and blocks nothing; and the marker never reaches the request.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { NewRunForm } from './NewRunForm'
import { api } from '../api'

vi.mock('../api', () => ({
  api: {
    listKnowledgeBases: vi.fn().mockResolvedValue({ knowledge_bases: [], count: 0 }),
    getDocumentFormats: vi.fn().mockResolvedValue({ formats: [], max_upload_bytes: 0, max_document_chars: 0 }),
    extractDocument: vi.fn(),
    createRun: vi.fn().mockResolvedValue({ run_id: 'new-run', renamed: [] }),
    getModels: vi.fn().mockResolvedValue({ models: [] }),
    suggestPersonas: vi.fn(),
    suggestName: vi.fn().mockResolvedValue({ name: 'x', description: 'y' }),
    checkNames: vi.fn(),
  },
}))
const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>

const BEZOS = { from: 'Jeff Bezos', to: 'Geoff Beesoh', reason: 'real public figure', source: 'list' }
const NOTICE = "'Jeff Bezos' is a real public figure, so this persona is 'Geoff Beesoh'. Personas never use real people's names."

const form = () => render(<NewRunForm onStarted={() => {}} onEnsembleStarted={() => {}} onCancel={() => {}} />)
const nameField = (i = 0) => screen.getAllByPlaceholderText('Name')[i] as HTMLInputElement
const asked = () => mocked.checkNames.mock.calls.map((c) => c[0] as string[])

// A stand-in for the server: only Jeff Bezos is a real public figure.
const server = (names: string[]) =>
  Promise.resolve({ renamed: names.filter((n) => n.trim().toLowerCase() === 'jeff bezos').map(() => BEZOS) })

describe('real names in the new-run form', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocked.checkNames.mockImplementation(server)
  })

  it('does not check a name while it is being typed, and switches a real one on blur, saying why', async () => {
    form()
    fireEvent.focus(nameField())
    fireEvent.change(nameField(), { target: { value: 'Jeff Bezos' } })
    // Mid-typing: nothing asked, nothing switched under the cursor.
    await new Promise((r) => setTimeout(r, 20))
    expect(asked().flat()).not.toContain('Jeff Bezos')
    expect(nameField().value).toBe('Jeff Bezos')

    fireEvent.blur(nameField())
    await waitFor(() => expect(nameField().value).toBe('Geoff Beesoh'))
    // A status, so a screen reader hears it: the field changed under the user.
    expect(screen.getByText(NOTICE)).toHaveAttribute('role', 'status')
  })

  it('drops the notice once the user picks another fictional name, and switches the real one again if retyped', async () => {
    form()
    fireEvent.focus(nameField())
    fireEvent.change(nameField(), { target: { value: 'Jeff Bezos' } })
    fireEvent.blur(nameField())
    await waitFor(() => expect(screen.getByText(NOTICE)).toBeInTheDocument())

    fireEvent.focus(nameField())
    fireEvent.change(nameField(), { target: { value: 'Rafe Calloway' } })
    expect(screen.queryByText(NOTICE)).toBeNull()
    fireEvent.blur(nameField())
    await new Promise((r) => setTimeout(r, 20))
    expect(nameField().value).toBe('Rafe Calloway')

    const calls = mocked.checkNames.mock.calls.length
    fireEvent.focus(nameField())
    fireEvent.change(nameField(), { target: { value: 'jeff  bezos' } })
    fireEvent.blur(nameField())
    await waitFor(() => expect(nameField().value).toBe('Geoff Beesoh'))
    expect(screen.getByText(NOTICE)).toBeInTheDocument()
    // Answered from the form's cache: the same person, however it was spaced or capitalised.
    expect(mocked.checkNames.mock.calls.length).toBe(calls)
  })

  it('checks a cast filled wholesale without anyone typing (the example, an import, a template)', async () => {
    form()
    fireEvent.click(screen.getByRole('button', { name: 'Load example' }))
    await waitFor(() => expect(asked().flat()).toEqual(expect.arrayContaining(['Maya', 'Alex'])))
    expect(screen.queryByText(NOTICE)).toBeNull()
  })

  it('shows the notice on a drafted persona the server already renamed', async () => {
    mocked.suggestPersonas.mockResolvedValue({
      cast: [{ name: 'Geoff Beesoh', persona: 'Runs the marketplace.', goals: [] }, { name: 'Dana', persona: 'CFO.', goals: [] }],
      count: 2,
      renamed: [BEZOS],
    })
    form()
    fireEvent.change(screen.getByPlaceholderText(/deciding whether to move/), { target: { value: 'Pricing' } })
    fireEvent.click(screen.getByRole('button', { name: /Draft cast/ }))
    await waitFor(() => expect(nameField().value).toBe('Geoff Beesoh'))
    expect(screen.getByText(NOTICE)).toBeInTheDocument()
    expect(screen.getAllByText(/is a real public figure/)).toHaveLength(1)
  })

  it('a failed check changes nothing and blocks nothing: the run still starts', async () => {
    mocked.checkNames.mockRejectedValue(new Error('404: Not Found'))
    form()
    fireEvent.change(screen.getByPlaceholderText('What should the cast discuss?'), { target: { value: 'Pricing' } })
    fireEvent.focus(nameField())
    fireEvent.change(nameField(), { target: { value: 'Ruth' } })
    fireEvent.blur(nameField())
    fireEvent.change(screen.getAllByPlaceholderText('Persona description')[0], { target: { value: 'A buyer.' } })
    await waitFor(() => expect(mocked.checkNames).toHaveBeenCalled())
    expect(nameField().value).toBe('Ruth')
    expect(screen.queryByText(/is a real public figure/)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Run simulation/ }))
    await waitFor(() => expect(mocked.createRun).toHaveBeenCalled())
  })

  it('a consultant gets the same check and its own wording', async () => {
    form()
    fireEvent.click(screen.getByRole('button', { name: '+ Add consultant' }))
    const field = screen.getByPlaceholderText('Consultant name') as HTMLInputElement
    fireEvent.focus(field)
    fireEvent.change(field, { target: { value: 'Jeff Bezos' } })
    fireEvent.blur(field)
    await waitFor(() => expect(field.value).toBe('Geoff Beesoh'))
    expect(
      screen.getByText("'Jeff Bezos' is a real public figure, so this consultant is 'Geoff Beesoh'. Consultants never use real people's names."),
    ).toBeInTheDocument()
  })
})

describe('the simulated-persona marker in the new-run form', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocked.checkNames.mockResolvedValue({ renamed: [] })
  })

  it('sits beside every persona’s name field and on the launch review, and never reaches the request', async () => {
    form()
    fireEvent.click(screen.getByRole('button', { name: 'Load example' }))
    // One beside each name field.
    const row = nameField(0).closest('div') as HTMLElement
    expect(within(row).getByRole('img', { name: 'simulated persona' })).toBeInTheDocument()

    // The launch review names the cast, each marked.
    const review = screen.getByText('Cast', { selector: 'dt' }).nextElementSibling as HTMLElement
    expect(review).toHaveTextContent('Maya · Alex')
    expect(within(review).getAllByRole('img', { name: 'simulated persona' })).toHaveLength(2)

    fireEvent.click(screen.getByRole('button', { name: /Run simulation/ }))
    await waitFor(() => expect(mocked.createRun).toHaveBeenCalled())
    const body = mocked.createRun.mock.calls[0][0]
    expect(body.cast.map((c: { name: string }) => c.name)).toEqual(['Maya', 'Alex'])
    expect(JSON.stringify(body)).not.toContain('(bot)')
    expect(JSON.stringify(body)).not.toContain('renamed')
  })
})
