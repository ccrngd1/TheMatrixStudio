// SPDX-License-Identifier: Apache-2.0
/**
 * The pre-conversation research toggle — `docs/PERSONA-RESEARCH.md` §7.
 *
 * Two of these are the reason this file exists rather than a line in the main form test.
 *
 * **Research must force retrieval on.** `retrieve_for_turn` is never called with retrieval
 * disabled, so an operator who enabled research and attached nothing else would pay for a
 * full search pass and then hold a conversation that never queried a word of it. The corpus
 * would be there, complete and invisible — the exact failure this project keeps rediscovering,
 * and one with no symptom except a conversation that seems no better informed.
 *
 * **The button has to name the collection count.** §7: research is 1 + N corpora, so the cost
 * scales with the cast and the operator should see "Research 3 collections" BEFORE agreeing.
 * A number that appears only in the bill is a number nobody consented to.
 *
 * `targets` is asserted ABSENT from the body. It names collections to WRITE into; the server
 * resolves it and overwrites whatever arrives, and the form must never be the thing that
 * nominates one.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
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
    createRun: vi.fn(),
    createEnsemble: vi.fn(),
    getModels: vi.fn().mockResolvedValue({ models: [] }),
    suggestPersonas: vi.fn(),
    suggestName: vi.fn().mockResolvedValue({ name: 'trusted-robot', description: 'x' }),
  },
}))

function renderForm() {
  render(<NewRunForm onStarted={vi.fn()} onEnsembleStarted={vi.fn()} onCancel={vi.fn()} />)
}

/** A minimal valid run: a topic and one persona. */
function fillMinimum() {
  fireEvent.change(screen.getByPlaceholderText(/What should the cast discuss/i), {
    target: { value: 'whether a lapsed plan authorisation may be renewed' },
  })
  fireEvent.change(screen.getByPlaceholderText('Name'), { target: { value: 'Casey' } })
  fireEvent.change(screen.getByPlaceholderText(/Persona description/), {
    target: { value: 'a compliance lead' },
  })
}

/** Give the persona a viewpoint, which is what makes them researchable. */
function addViewpoint() {
  fireEvent.click(screen.getByText(/Convictions & background documents/))
  fireEvent.change(screen.getByPlaceholderText(/No feature may add an external service/), {
    target: { value: '[firm] the statute governs -> a board ruling' },
  })
}

const toggle = () => screen.getByLabelText(/Research the subject before starting/i)
const body = () => (api.createRun as ReturnType<typeof vi.fn>).mock.calls[0]?.[0]

describe('NewRunForm research toggle', () => {
  beforeEach(() => vi.clearAllMocks())

  it('is off by default and sends no research block', () => {
    // It searches the open web and costs minutes and real money per run, so it is a decision
    // made each time rather than a default somebody inherits.
    renderForm()
    fillMinimum()
    fireEvent.click(screen.getByRole('button', { name: /Run simulation/ }))
    expect(body().config.research).toBeUndefined()
  })

  it('hides its options until it is on', () => {
    renderForm()
    expect(screen.queryByText(/Shared research/)).not.toBeInTheDocument()
    fireEvent.click(toggle())
    expect(screen.getByText(/Shared research/)).toBeInTheDocument()
    expect(screen.getByText(/Per-persona research/)).toBeInTheDocument()
  })

  it('sends both tiers on by default when enabled', () => {
    renderForm()
    fillMinimum()
    fireEvent.click(toggle())
    fireEvent.click(screen.getByRole('button', { name: /Research/ }))

    expect(body().config.research).toEqual({
      enabled: true,
      shared: true,
      personas: true,
      results_per_query: 5,
    })
  })

  it('never sends a targets block', () => {
    // It names collections to WRITE into. The server resolves it and overwrites whatever
    // arrives; a form that nominated one would be the thing to exploit.
    renderForm()
    fillMinimum()
    fireEvent.click(toggle())
    fireEvent.click(screen.getByRole('button', { name: /Research/ }))
    expect(body().config.research.targets).toBeUndefined()
  })

  it('turns retrieval ON even when the cast attached nothing', () => {
    // THE test. Without this the run researches, ingests, embeds — and never queries any of
    // it, because `retrieve_for_turn` is not called with retrieval disabled. A complete,
    // invisible corpus, and no symptom but a conversation that seems no better informed.
    renderForm()
    fillMinimum()
    fireEvent.click(toggle())
    fireEvent.click(screen.getByRole('button', { name: /Research/ }))

    expect(body().config.retrieval).toEqual({ enabled: true, authority_floor: 1, cite_inline: true })
  })

  it('sets an authority floor, so a statute it finds cannot lose every slot', () => {
    // The floor defaults to 0 server-side, which is off. A run that went looking for
    // controlling authority and then let a blog outrank it would have found the answer and
    // hidden it — worse than not searching, because the corpus would show an authority no
    // turn ever saw.
    renderForm()
    fillMinimum()
    fireEvent.click(toggle())
    fireEvent.click(screen.getByRole('button', { name: /Research/ }))
    expect(body().config.retrieval.authority_floor).toBe(1)
  })

  it('leaves the floor off when research is off', () => {
    renderForm()
    fillMinimum()
    addViewpoint()
    fireEvent.click(screen.getByRole('button', { name: /Run simulation/ }))
    // Retrieval is on because a document was authored, but the floor is not: a run without
    // research has nothing tiered controlling, and this must cost it nothing.
    expect(body().config.retrieval?.authority_floor).toBeUndefined()
  })

  it('names the collection count on the button', () => {
    // §7. One persona with a viewpoint plus the shared corpus is two collections.
    renderForm()
    fillMinimum()
    addViewpoint()
    fireEvent.click(toggle())
    expect(
      screen.getByRole('button', { name: /Research 2 collections, then run/ }),
    ).toBeInTheDocument()
  })

  it('counts only the shared corpus when no persona has a viewpoint', () => {
    // A persona with nothing to research is skipped server-side rather than given an empty
    // collection, so the count must not promise one.
    renderForm()
    fillMinimum()
    fireEvent.click(toggle())
    expect(
      screen.getByRole('button', { name: /Research 1 collection, then run/ }),
    ).toBeInTheDocument()
  })

  it('drops the shared tier from both the count and the body when switched off', () => {
    renderForm()
    fillMinimum()
    addViewpoint()
    fireEvent.click(toggle())
    fireEvent.click(screen.getByLabelText(/Shared research/i, { selector: 'input' }))

    expect(
      screen.getByRole('button', { name: /Research 1 collection, then run/ }),
    ).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Research/ }))
    expect(body().config.research.shared).toBe(false)
  })

  it('warns when both tiers are off, rather than silently researching nothing', () => {
    renderForm()
    fillMinimum()
    fireEvent.click(toggle())
    fireEvent.click(screen.getByLabelText(/Shared research/i, { selector: 'input' }))
    fireEvent.click(screen.getByLabelText(/Per-persona research/i, { selector: 'input' }))
    expect(screen.getByText(/nothing would be researched/)).toBeInTheDocument()
  })

  it('clamps sources per query to the range the API accepts', () => {
    renderForm()
    fillMinimum()
    fireEvent.click(toggle())
    const input = screen.getByLabelText(/Sources per query/i, { selector: 'input' })
    fireEvent.change(input, { target: { value: '99' } })
    fireEvent.click(screen.getByRole('button', { name: /Research/ }))
    // 20 is the model's ceiling; sending 99 would be a 422 after the operator had filled in
    // a whole form.
    expect(body().config.research.results_per_query).toBe(20)
  })

  it('no longer warns that an ensemble refuses research, because it does not', () => {
    // §6 is built: the pass runs once before any member exists and every member binds the same
    // collections (tests/test_api_ensembles.py). A warning that starting "will be refused" was
    // telling the operator not to launch something that works.
    renderForm()
    fillMinimum()
    fireEvent.click(toggle())
    fireEvent.change(screen.getByLabelText(/Run/, { selector: 'select' }), {
      target: { value: 'ensemble' },
    })
    expect(screen.queryByText(/not yet available for ensembles/)).not.toBeInTheDocument()
  })

  it('explains itself, because an unexplained switch that costs money stays off', () => {
    renderForm()
    fireEvent.click(screen.getByRole('button', { name: /pre-conversation research/i }))
    expect(screen.getByText(/statutes, regulations, board opinions/)).toBeInTheDocument()
    // The two facts an operator needs before agreeing: what it costs, and that they are not
    // made to wait for it.
    expect(screen.getByText(/\$0\.25/)).toBeInTheDocument()
    expect(screen.getByText(/not made to wait/)).toBeInTheDocument()
  })
})

describe('NewRunForm inline citations', () => {
  beforeEach(() => vi.clearAllMocks())

  it('asks for inline citations by default whenever retrieval is on', () => {
    renderForm()
    fillMinimum()
    expect(screen.queryByText(/cite their sources inline/)).not.toBeInTheDocument()
    fireEvent.click(toggle())
    fireEvent.click(screen.getByRole('button', { name: /Research/ }))
    expect(body().config.retrieval).toMatchObject({ enabled: true, cite_inline: true })
  })

  it('sends an explicit false when unticked, because the server default is on', () => {
    renderForm()
    fillMinimum()
    fireEvent.click(toggle())
    fireEvent.click(screen.getByLabelText(/cite their sources inline/))
    fireEvent.click(screen.getByRole('button', { name: /Research/ }))
    expect(body().config.retrieval.cite_inline).toBe(false)
  })
})
