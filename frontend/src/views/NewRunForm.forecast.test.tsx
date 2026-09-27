// SPDX-License-Identifier: Apache-2.0
// The launch form's cost forecast: asked for only when the form could launch, debounced, and
// priced on exactly the body that would launch.
import { afterEach, describe, expect, it, vi, beforeEach } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
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
    forecastRun: vi.fn(),
    forecastEnsemble: vi.fn(),
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


const mocked = api as unknown as {
  forecastRun: ReturnType<typeof vi.fn>
  forecastEnsemble: ReturnType<typeof vi.fn>
}
const FORECAST = {
  runs: 1, parts: [], low: 0.5, typical: 0.6, high: 0.7, complete: true, unmeasured: [], thin: [], budget: null,
}

describe('NewRunForm cost forecast', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.useFakeTimers()
    mocked.forecastRun.mockResolvedValue(FORECAST)
    mocked.forecastEnsemble.mockResolvedValue({ ...FORECAST, runs: 5 })
  })
  afterEach(() => vi.useRealTimers())

  const settle = async () => {
    await act(async () => {
      vi.advanceTimersByTime(700)
    })
    await act(async () => {})
  }

  it('asks for nothing until the form could launch', async () => {
    renderForm()
    await settle()
    expect(mocked.forecastRun).not.toHaveBeenCalled()
  })

  it('prices exactly the body that would launch, once typing pauses', async () => {
    renderForm()
    fillMinimum()
    await settle()
    expect(mocked.forecastRun).toHaveBeenCalledTimes(1)
    const body = mocked.forecastRun.mock.calls[0][0]
    expect(body.topic).toMatch(/lapsed plan authorisation/)
    expect(body.cast[0].name).toBe('Casey')
    expect(screen.getByTestId('forecast-total')).toHaveTextContent('about $0.60')
  })

  it('prices an ensemble through the ensemble route, with its cells', async () => {
    renderForm()
    fillMinimum()
    fireEvent.change(screen.getByLabelText(/Run/, { selector: 'select' }), {
      target: { value: 'ensemble' },
    })
    await settle()
    const body = mocked.forecastEnsemble.mock.lastCall![0]
    expect(body.cells[0].label).toBe('base')
    expect(screen.getByText(/for all 5 conversations/)).toBeInTheDocument()
  })

  it('does not send pasted documents with every keystroke', async () => {
    renderForm()
    fillMinimum()
    await settle()
    const body = mocked.forecastRun.mock.lastCall![0]
    expect(body.cast.every((c: Record<string, unknown>) => !('document_texts' in c))).toBe(true)
  })
})
