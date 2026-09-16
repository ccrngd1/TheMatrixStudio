// SPDX-License-Identifier: Apache-2.0
//
// The conversation-method selector. Two things to get right: the value reaches the API, and
// picking `simultaneous` disables the convergence toggle — because in that mode a round where
// everybody passes ends the run whatever the checkbox says, and offering a switch that does
// nothing is worse than not offering it.
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

const methodBox = () =>
  screen.getByText('Conversation method').closest('label')!
    .querySelector('select') as HTMLSelectElement
const convergeBox = () =>
  screen.getByText(/End when the conversation is finished/i).closest('label')!
    .querySelector('input[type="checkbox"]') as HTMLInputElement

async function form() {
  render(<NewRunForm onStarted={() => {}} onCancel={() => {}} />)
  await waitFor(() => expect(methodBox()).toBeInTheDocument())
  fireEvent.click(screen.getByRole('button', { name: /Load example/i }))
}

async function submitted() {
  fireEvent.click(screen.getByRole('button', { name: /Run simulation/i }))
  await waitFor(() => expect(api.createRun).toHaveBeenCalled())
  return (api.createRun as ReturnType<typeof vi.fn>).mock.calls[0][0]
}

describe('NewRunForm conversation method', () => {
  beforeEach(() => vi.clearAllMocks())

  it('defaults to moderated and sends no selection block', async () => {
    await form()
    expect(methodBox().value).toBe('moderated')
    const body = await submitted()
    // Absent rather than `{method: 'moderated'}`: the server's default is what should
    // apply, and pinning today's default into every run is how a default stops being one.
    expect(body.config.selection).toBeUndefined()
  })

  it('sends the method when simultaneous is chosen', async () => {
    await form()
    fireEvent.change(methodBox(), { target: { value: 'simultaneous' } })
    const body = await submitted()
    expect(body.config.selection).toEqual({ method: 'simultaneous' })
  })

  it('combines the method with the convergence flag when both are set', async () => {
    await form()
    fireEvent.click(convergeBox())
    fireEvent.change(methodBox(), { target: { value: 'simultaneous' } })
    const body = await submitted()
    expect(body.config.selection).toEqual({
      method: 'simultaneous', stop_when_converged: true,
    })
  })

  it('disables the convergence toggle in simultaneous mode, and says why', async () => {
    await form()
    expect(convergeBox().disabled).toBe(false)
    fireEvent.change(methodBox(), { target: { value: 'simultaneous' } })
    expect(convergeBox().disabled).toBe(true)
    expect(screen.getByText(/automatic — a round where everyone passes/)).toBeInTheDocument()
  })
})
