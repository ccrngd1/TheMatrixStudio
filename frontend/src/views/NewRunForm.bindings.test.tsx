// SPDX-License-Identifier: Apache-2.0
// Binding knowledge bases to a run, at both levels.
//
// The last step that makes Phase 6 usable end to end: collections could be created,
// filled and shared, and there was no way to bring one into a conversation without
// hand-posting JSON.
//
// Four things are worth asserting, and only the first is obvious:
//
//   1. the two levels are sent as two different fields — run-level under `config`, and
//      persona-level on that persona alone. Sending both at run level would silently
//      widen a private collection to the whole cast;
//   2. `retrieval.enabled` must be set, or bindings do nothing at all — `retrieve_for_turn`
//      is never called with retrieval off, so a run that bound three collections and
//      pasted no documents would search none of them and say nothing about why;
//   3. a 422 from a revoked grant must reach the operator NAMING the collection —
//      "shared with me" and "bindable" are almost the same set, and the difference is
//      exactly this case;
//   4. the picker failing must not stop somebody starting a run.

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
    listKnowledgeBases: vi.fn(),
  },
}))
import { api } from '../api'

const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>

const MINE = {
  id: 'kb-mine',
  name: 'egress-policies',
  description: null,
  owner_sub: 'me',
  embedding_model: null,
  created_at: 1,
  shared: false,
  document_count: 3,
}
const SHARED = { ...MINE, id: 'kb-shared', name: 'legal-precedent', shared: true }

beforeEach(() => {
  vi.clearAllMocks()
  mocked.getDocumentFormats.mockResolvedValue({
    formats: [{ suffix: '.txt', media_type: 'txt', available: true, needs: null }],
    max_upload_bytes: 10485760,
    max_document_chars: 400000,
  })
  mocked.getModels.mockResolvedValue({ models: [] })
  mocked.suggestName.mockResolvedValue({ name: 'trusted-robot', description: 'x' })
  mocked.listKnowledgeBases.mockResolvedValue({
    knowledge_bases: [MINE, SHARED],
    count: 2,
  })
})

async function form() {
  render(<NewRunForm onStarted={() => {}} onEnsembleStarted={() => {}} onCancel={() => {}} />)
  // Wait for the pickers to resolve, or a click lands on "Loading collections…".
  await waitFor(() => expect(screen.getAllByText('egress-policies').length).toBeGreaterThan(0))
}

function fillMinimum() {
  fireEvent.change(screen.getByPlaceholderText('What should the cast discuss?'), {
    target: { value: 'egress inspection at the border' },
  })
  fireEvent.change(screen.getByPlaceholderText('Name'), { target: { value: 'Ada' } })
  fireEvent.change(screen.getByPlaceholderText('Persona description'), {
    target: { value: 'a network architect' },
  })
}

// --------------------------------------------------------------------------- //
// The two levels are two different fields
// --------------------------------------------------------------------------- //

describe('binding levels', () => {
  it('sends a run-level binding under config, not on the cast', async () => {
    mocked.createRun.mockResolvedValue({ run_id: 'r1', name: 'x', status: 'pending' })
    await form()
    fillMinimum()

    fireEvent.click(screen.getByLabelText('Bind egress-policies to every persona'))
    fireEvent.click(screen.getByRole('button', { name: /run simulation/i }))

    await waitFor(() => expect(mocked.createRun).toHaveBeenCalled())
    const body = mocked.createRun.mock.calls[0][0]
    expect(body.config.knowledge_bases).toEqual(['kb-mine'])
    // And NOT on the persona — that would be a narrower scope than was asked for.
    expect(body.cast[0].knowledge_bases).toBeUndefined()
  })

  it('sends a persona binding on that persona ALONE', async () => {
    // The half that matters: run-level would silently widen a private collection to the
    // whole cast, which is a disclosure rather than a scoping slip.
    mocked.createRun.mockResolvedValue({ run_id: 'r1', name: 'x', status: 'pending' })
    await form()
    fillMinimum()

    fireEvent.click(screen.getByLabelText('Bind egress-policies to Ada'))
    fireEvent.click(screen.getByRole('button', { name: /run simulation/i }))

    await waitFor(() => expect(mocked.createRun).toHaveBeenCalled())
    const body = mocked.createRun.mock.calls[0][0]
    expect(body.cast[0].knowledge_bases).toEqual(['kb-mine'])
    expect(body.config.knowledge_bases).toBeUndefined()
  })

  it('omits both fields entirely when nothing is bound', async () => {
    // Not `[]`: an empty array makes the server validate a binding list nobody chose.
    mocked.createRun.mockResolvedValue({ run_id: 'r1', name: 'x', status: 'pending' })
    await form()
    fillMinimum()
    fireEvent.click(screen.getByRole('button', { name: /run simulation/i }))

    await waitFor(() => expect(mocked.createRun).toHaveBeenCalled())
    const body = mocked.createRun.mock.calls[0][0]
    expect(body.config.knowledge_bases).toBeUndefined()
    expect(body.cast[0].knowledge_bases).toBeUndefined()
  })

  it('offers a SHARED collection, because that is what sharing is for', async () => {
    await form()
    expect(screen.getByLabelText('Bind legal-precedent to every persona')).toBeTruthy()
    // Marked, so the operator knows a revoked grant can refuse the run.
    expect(screen.getAllByText('shared').length).toBeGreaterThan(0)
  })
})

// --------------------------------------------------------------------------- //
// Retrieval has to be on, or a binding does nothing
// --------------------------------------------------------------------------- //

describe('retrieval', () => {
  it('is enabled by a binding alone, with no pasted documents', async () => {
    // `retrieve_for_turn` is never called with retrieval off, so without this a run that
    // bound three collections would search none of them and say nothing about why.
    mocked.createRun.mockResolvedValue({ run_id: 'r1', name: 'x', status: 'pending' })
    await form()
    fillMinimum()
    fireEvent.click(screen.getByLabelText('Bind egress-policies to every persona'))
    fireEvent.click(screen.getByRole('button', { name: /run simulation/i }))

    await waitFor(() => expect(mocked.createRun).toHaveBeenCalled())
    expect(mocked.createRun.mock.calls[0][0].config.retrieval).toEqual({ enabled: true })
  })

  it('stays off when neither a document nor a binding exists', async () => {
    // Enabling a feature nobody configured costs tokens for an empty prompt block.
    mocked.createRun.mockResolvedValue({ run_id: 'r1', name: 'x', status: 'pending' })
    await form()
    fillMinimum()
    fireEvent.click(screen.getByRole('button', { name: /run simulation/i }))

    await waitFor(() => expect(mocked.createRun).toHaveBeenCalled())
    expect(mocked.createRun.mock.calls[0][0].config.retrieval).toBeUndefined()
  })
})

// --------------------------------------------------------------------------- //
// A revoked grant, which is where "shared" and "bindable" diverge
// --------------------------------------------------------------------------- //

describe('a binding the server refuses', () => {
  it('shows the 422 naming the collection', async () => {
    // The owner revoked between the picker loading and the run starting. The server
    // re-validates and refuses; the operator has to see WHICH collection, or the only
    // remedy is unpicking them one at a time.
    mocked.createRun.mockRejectedValue(
      new Error(
        '422: These knowledge bases do not exist or are not shared with you: kb-shared',
      ),
    )
    await form()
    fillMinimum()
    fireEvent.click(screen.getByLabelText('Bind legal-precedent to every persona'))
    fireEvent.click(screen.getByRole('button', { name: /run simulation/i }))

    await waitFor(() => expect(screen.getByText(/kb-shared/)).toBeTruthy())
    expect(screen.getByText(/not shared with you/i)).toBeTruthy()
  })
})

// --------------------------------------------------------------------------- //
// The picker must never block a run
// --------------------------------------------------------------------------- //

describe('when collections cannot be listed', () => {
  it('says so and still allows the run to start', async () => {
    mocked.listKnowledgeBases.mockRejectedValue(new Error('503: Service Unavailable'))
    mocked.createRun.mockResolvedValue({ run_id: 'r1', name: 'x', status: 'pending' })
    render(<NewRunForm onStarted={() => {}} onEnsembleStarted={() => {}} onCancel={() => {}} />)

    await waitFor(() =>
      expect(screen.getAllByText(/could not load your collections/i).length).toBeGreaterThan(0),
    )
    fillMinimum()
    fireEvent.click(screen.getByRole('button', { name: /run simulation/i }))
    await waitFor(() => expect(mocked.createRun).toHaveBeenCalled())
  })

  it('survives an api that has no listKnowledgeBases at all', async () => {
    // A synchronous throw inside an effect, which `.catch` does not cover. It took the
    // whole form down when the picker was first written — found by three existing test
    // files whose api mock predates this method.
    mocked.listKnowledgeBases.mockImplementation(() => {
      throw new TypeError('api.listKnowledgeBases is not a function')
    })
    render(<NewRunForm onStarted={() => {}} onEnsembleStarted={() => {}} onCancel={() => {}} />)
    await waitFor(() =>
      expect(screen.getByPlaceholderText('What should the cast discuss?')).toBeTruthy(),
    )
  })

  it('shows the empty state when there are no collections yet', async () => {
    mocked.listKnowledgeBases.mockResolvedValue({ knowledge_bases: [], count: 0 })
    render(<NewRunForm onStarted={() => {}} onEnsembleStarted={() => {}} onCancel={() => {}} />)
    await waitFor(() =>
      expect(screen.getAllByText(/no knowledge bases yet/i).length).toBeGreaterThan(0),
    )
  })
})
