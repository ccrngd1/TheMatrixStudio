// SPDX-License-Identifier: Apache-2.0
//
// The closing-round toggle. It is independent of the method — a moderated run that hits its
// cap stops mid-argument just as a simultaneous one does — so the test that matters is that
// it composes with both the method and the convergence flag.
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { NewRunForm } from './NewRunForm'
import { api } from '../api'

vi.mock('../api', () => ({
  api: {
    listKnowledgeBases: vi.fn().mockResolvedValue({ knowledge_bases: [], count: 0 }),
    getDocumentFormats: vi.fn().mockResolvedValue({
      formats: [{ suffix: '.txt', media_type: 'txt', available: true, needs: null }],
      max_upload_bytes: 10485760, max_document_chars: 400000,
    }),
    extractDocument: vi.fn(),
    createRun: vi.fn().mockResolvedValue({ run_id: 'r1' }),
    getModels: vi.fn().mockResolvedValue({ models: [] }),
    suggestPersonas: vi.fn(),
    suggestName: vi.fn().mockResolvedValue({ name: 'n', description: 'd' }),
  },
}))

const box = (label: RegExp) =>
  screen.getByText(label).closest('label')!
    .querySelector('input[type="checkbox"]') as HTMLInputElement
const methodBox = () =>
  screen.getByText('Conversation method').closest('label')!
    .querySelector('select') as HTMLSelectElement

async function form() {
  render(<NewRunForm onStarted={() => {}} onEnsembleStarted={() => {}} onCancel={() => {}} />)
  await waitFor(() => expect(methodBox()).toBeInTheDocument())
  fireEvent.click(screen.getByRole('button', { name: /Load example/i }))
}

async function submitted() {
  fireEvent.click(screen.getByRole('button', { name: /Run simulation/i }))
  await waitFor(() => expect(api.createRun).toHaveBeenCalled())
  return (api.createRun as ReturnType<typeof vi.fn>).mock.calls[0][0]
}

describe('NewRunForm closing round', () => {
  beforeEach(() => vi.clearAllMocks())

  it('is off by default and sends nothing', async () => {
    await form()
    expect(box(/Closing round when the ceiling/).checked).toBe(false)
    expect((await submitted()).config.selection).toBeUndefined()
  })

  it('sends the flag on its own', async () => {
    await form()
    fireEvent.click(box(/Closing round when the ceiling/))
    expect((await submitted()).config.selection).toEqual({ closing_round: true })
  })

  it('composes with the method and the convergence flag', async () => {
    await form()
    fireEvent.click(box(/Closing round when the ceiling/))
    fireEvent.click(box(/End when the conversation is finished/))
    fireEvent.change(methodBox(), { target: { value: 'simultaneous' } })
    expect((await submitted()).config.selection).toEqual({
      method: 'simultaneous', stop_when_converged: true, closing_round: true,
    })
  })

  it('stays available in simultaneous mode', async () => {
    // Unlike the convergence toggle, which that mode makes automatic. A simultaneous run
    // can still run out of budget mid-argument — the first one did, at round 8 of 8.
    await form()
    fireEvent.change(methodBox(), { target: { value: 'simultaneous' } })
    expect(box(/Closing round when the ceiling/).disabled).toBe(false)
  })
})
