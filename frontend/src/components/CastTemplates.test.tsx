// SPDX-License-Identifier: Apache-2.0
//
// Saved casts. What this pins: a clash asks before replacing, loading replaces the cast only after
// confirming, a dropped pasted document is reported, and a deployment without the route leaves the
// form working.
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { CastTemplates } from './CastTemplates'
import { api } from '../api'

vi.mock('../api', () => ({
  api: {
    listCastTemplates: vi.fn(),
    getCastTemplate: vi.fn(),
    saveCastTemplate: vi.fn(),
    deleteCastTemplate: vi.fn(),
  },
}))
const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>

const CAST = [{ name: 'Casey', persona: 'a compliance lead', goals: ['keep it lawful'], knowledge_bases: ['kb1'] }]

describe('CastTemplates', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocked.listCastTemplates.mockResolvedValue([
      { name: 'Board review', description: null, personas: ['Casey'], updated_at: 1 },
    ])
  })

  it('loads a template into the form, keeping bindings', async () => {
    mocked.getCastTemplate.mockResolvedValue({ name: 'Board review', description: null, cast: CAST })
    const onLoad = vi.fn()
    render(<CastTemplates getCast={() => []} onLoad={onLoad} hasCast={false} />)
    const select = await screen.findByLabelText('Load a cast template')
    fireEvent.change(select, { target: { value: 'Board review' } })
    await waitFor(() => expect(onLoad).toHaveBeenCalled())
    const [cast] = onLoad.mock.calls[0]
    expect(cast[0]).toMatchObject({ name: 'Casey', knowledgeBases: ['kb1'], goals: 'keep it lawful' })
  })

  it('asks before replacing a cast already in the form', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const onLoad = vi.fn()
    render(<CastTemplates getCast={() => []} onLoad={onLoad} hasCast />)
    fireEvent.change(await screen.findByLabelText('Load a cast template'), { target: { value: 'Board review' } })
    expect(confirm).toHaveBeenCalled()
    expect(mocked.getCastTemplate).not.toHaveBeenCalled()
    confirm.mockRestore()
  })

  it('offers to replace on a name clash instead of overwriting', async () => {
    mocked.saveCastTemplate.mockRejectedValueOnce(new Error('409: exists'))
    mocked.saveCastTemplate.mockResolvedValueOnce({ name: 'Board review', dropped_documents: 1 })
    render(<CastTemplates getCast={() => CAST} onLoad={vi.fn()} hasCast />)
    fireEvent.click(screen.getByText('Save as template'))
    fireEvent.change(screen.getByPlaceholderText('Template name'), { target: { value: 'Board review' } })
    fireEvent.click(screen.getByText('Save'))
    fireEvent.click(await screen.findByText('Replace it'))
    await waitFor(() => expect(mocked.saveCastTemplate).toHaveBeenLastCalledWith(
      expect.objectContaining({ name: 'Board review', overwrite: true })))
    expect(await screen.findByText(/1 pasted document was not kept/)).toBeInTheDocument()
  })

  it('says what is missing rather than saving an empty cast', async () => {
    render(<CastTemplates getCast={() => []} onLoad={vi.fn()} hasCast={false} />)
    fireEvent.click(screen.getByText('Save as template'))
    fireEvent.change(screen.getByPlaceholderText('Template name'), { target: { value: 'x' } })
    fireEvent.click(screen.getByText('Save'))
    expect(await screen.findByText(/Add at least one persona/)).toBeInTheDocument()
    expect(mocked.saveCastTemplate).not.toHaveBeenCalled()
  })

  it('still offers saving when the list cannot be read', async () => {
    mocked.listCastTemplates.mockRejectedValue(new Error('404'))
    render(<CastTemplates getCast={() => CAST} onLoad={vi.fn()} hasCast />)
    await waitFor(() => expect(mocked.listCastTemplates).toHaveBeenCalled())
    expect(screen.queryByLabelText('Load a cast template')).not.toBeInTheDocument()
    expect(screen.getByText('Save as template')).toBeInTheDocument()
  })
})
