// SPDX-License-Identifier: Apache-2.0
//
// The ceiling toggle. Two things have to hold, and the second is the one an operator
// notices: the flag reaches the API, and the turn count stops being a plan.
//
// A 10-turn "budget" with this on would cut a converging conversation off at 10 — the
// arbitrary length the feature exists to remove — so switching it on raises the number and
// switching it off puts back whatever was there before.
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { NewRunForm } from './NewRunForm'
import { CEILING_TURNS } from './newRunTypes'
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
    createRun: vi.fn().mockResolvedValue({ run_id: 'r1' }),
    getModels: vi.fn().mockResolvedValue({ models: [] }),
    suggestPersonas: vi.fn(),
    suggestName: vi.fn().mockResolvedValue({ name: 'n', description: 'd' }),
  },
}))

const toggle = () =>
  screen
    .getByText(/End when the conversation is finished/i)
    .closest('label')!
    .querySelector('input[type="checkbox"]') as HTMLInputElement

/** The turn-count box, found through its own label rather than by role: the form has
 *  several number inputs and only this one changes its name with the toggle. */
const turns = () =>
  screen
    .getByText(/^(Max messages|Turn ceiling)$/)
    .closest('label')!
    .querySelector('input[type="number"]') as HTMLInputElement

function renderForm() {
  return render(<NewRunForm onStarted={() => {}} onCancel={() => {}} />)
}

/** "Load example" fills a valid topic and cast; the form starts with one blank persona and
 *  will not submit, and hand-filling it here would test the form's validation instead. Note
 *  it also sets 12 turns, which is what makes the ceiling assertions below meaningful. */
function loadExample() {
  fireEvent.click(screen.getByRole('button', { name: /Load example/i }))
}

describe('NewRunForm: end when the conversation is finished', () => {
  beforeEach(() => vi.clearAllMocks())

  it('is off by default, matching the server', async () => {
    renderForm()
    await waitFor(() => expect(toggle()).toBeInTheDocument())
    expect(toggle().checked).toBe(false)
    expect(screen.getByText('Max messages')).toBeInTheDocument()
  })

  it('raises the turn count to a ceiling when switched on', async () => {
    renderForm()
    await waitFor(() => expect(toggle()).toBeInTheDocument())
    expect(Number(turns().value)).toBeLessThan(CEILING_TURNS)
    fireEvent.click(toggle())
    expect(Number(turns().value)).toBe(CEILING_TURNS)
    // …and says so, because "max messages" would now be a lie.
    expect(screen.getByText('Turn ceiling')).toBeInTheDocument()
  })

  it('restores the operator’s own number when switched back off', async () => {
    renderForm()
    await waitFor(() => expect(toggle()).toBeInTheDocument())
    fireEvent.change(turns(), { target: { value: '24' } })
    fireEvent.click(toggle())
    expect(Number(turns().value)).toBe(CEILING_TURNS)
    fireEvent.click(toggle())
    expect(Number(turns().value)).toBe(24)
  })

  it('does not lower a ceiling the operator set higher themselves', async () => {
    renderForm()
    await waitFor(() => expect(toggle()).toBeInTheDocument())
    fireEvent.change(turns(), { target: { value: '150' } })
    fireEvent.click(toggle())
    expect(Number(turns().value)).toBe(150)
  })

  it('sends the flag and the ceiling to the API', async () => {
    renderForm()
    await waitFor(() => expect(toggle()).toBeInTheDocument())
    loadExample()
    fireEvent.click(toggle())
    fireEvent.click(screen.getByRole('button', { name: /Run simulation/i }))
    await waitFor(() => expect(api.createRun).toHaveBeenCalled())
    const body = (api.createRun as ReturnType<typeof vi.fn>).mock.calls[0][0]
    expect(body.config.selection).toEqual({ stop_when_converged: true })
    expect(body.config.max_messages).toBe(CEILING_TURNS)
  })

  it('sends no selection block when the toggle is off', async () => {
    renderForm()
    await waitFor(() => expect(toggle()).toBeInTheDocument())
    loadExample()
    fireEvent.click(screen.getByRole('button', { name: /Run simulation/i }))
    await waitFor(() => expect(api.createRun).toHaveBeenCalled())
    const body = (api.createRun as ReturnType<typeof vi.fn>).mock.calls[0][0]
    // Absent, not `{stop_when_converged: false}`: the server's default is what should
    // apply, and sending false would pin today's default into every run for ever.
    expect(body.config.selection).toBeUndefined()
  })
})
