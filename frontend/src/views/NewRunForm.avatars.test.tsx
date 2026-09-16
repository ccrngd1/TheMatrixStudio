// SPDX-License-Identifier: Apache-2.0
// Avatars are on unless the operator says otherwise.
//
// Worth its own file because the failure was invisible: the form shipped the toggle
// defaulted OFF and sent `generate_avatars` on every submit, so an explicit `false`
// reached the server and beat the engine default (`enable_avatars`, on). Nothing was
// broken in the avatar path itself — every run through the UI simply asked for no
// avatars, while CLI runs, which omit the key, kept producing them. A default is only
// a default if the request does not overwrite it, so the assertion is on the WIRE
// BODY, not on the checkbox: `checked` could be right while the payload is wrong.

import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { NewRunForm } from './NewRunForm'

vi.mock('../api', () => ({
  api: {
    getDocumentFormats: vi.fn().mockResolvedValue({
      formats: [{ suffix: '.txt', media_type: 'txt', available: true, needs: null }],
      max_upload_bytes: 10485760,
      max_document_chars: 400000,
    }),
    extractDocument: vi.fn(),
    getModels: vi.fn().mockResolvedValue({ models: [] }),
    suggestPersonas: vi.fn(),
    suggestName: vi.fn().mockResolvedValue({ name: 'trusted-robot', description: 'x' }),
    createRun: vi.fn(),
    listKnowledgeBases: vi.fn().mockResolvedValue({ knowledge_bases: [], count: 0 }),
  },
}))
import { api } from '../api'

const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>

beforeEach(() => {
  vi.clearAllMocks()
  mocked.getDocumentFormats.mockResolvedValue({
    formats: [{ suffix: '.txt', media_type: 'txt', available: true, needs: null }],
    max_upload_bytes: 10485760,
    max_document_chars: 400000,
  })
  mocked.getModels.mockResolvedValue({ models: [] })
  mocked.suggestName.mockResolvedValue({ name: 'trusted-robot', description: 'x' })
  mocked.listKnowledgeBases.mockResolvedValue({ knowledge_bases: [], count: 0 })
  mocked.createRun.mockResolvedValue({ run_id: 'r1', name: 'x', status: 'pending' })
})

function fillMinimum() {
  fireEvent.change(screen.getByPlaceholderText('What should the cast discuss?'), {
    target: { value: 'egress inspection at the border' },
  })
  fireEvent.change(screen.getByPlaceholderText('Name'), { target: { value: 'Ada' } })
  fireEvent.change(screen.getByPlaceholderText('Persona description'), {
    target: { value: 'a network architect' },
  })
}

const avatarBox = () =>
  screen.getByRole('checkbox', { name: /generate avatars/i }) as HTMLInputElement

describe('avatar generation default', () => {
  it('asks for avatars on a run nobody configured', async () => {
    render(<NewRunForm onStarted={() => {}} onCancel={() => {}} />)
    fillMinimum()
    expect(avatarBox().checked).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: /run simulation/i }))

    await waitFor(() => expect(mocked.createRun).toHaveBeenCalled())
    expect(mocked.createRun.mock.calls[0][0].config.generate_avatars).toBe(true)
  })

  it('still lets the operator decline the image-model spend', async () => {
    // The other half of a default: it has to be refusable, or it is a hardcode.
    render(<NewRunForm onStarted={() => {}} onCancel={() => {}} />)
    fillMinimum()
    fireEvent.click(avatarBox())

    fireEvent.click(screen.getByRole('button', { name: /run simulation/i }))

    await waitFor(() => expect(mocked.createRun).toHaveBeenCalled())
    expect(mocked.createRun.mock.calls[0][0].config.generate_avatars).toBe(false)
  })
})
