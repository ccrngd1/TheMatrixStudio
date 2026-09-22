// SPDX-License-Identifier: Apache-2.0
//
// The run-type selector: one conversation, or the same brief N times.
//
// Two things carry real weight here.
//
// The default must be `single`. An operator who does not opt in must not be charged for five
// conversations, and a run type that defaulted to the expensive option would be the kind of
// bug nobody reports because they assume they clicked something.
//
// The spec must be EXPLICIT even when it matches the server default. The server would apply
// replicates-only for an omitted `cells`, but the stored spec is what the report header
// renders — a spec that says `n: 5` is the difference between a reader knowing five runs were
// asked for and inferring it from however many exist.
//
// And the hybrid comparison cell must differ in the speaker method ALONE. Turn count,
// fairness, the cast, the brief all held identical: the server refuses anything else with the
// reason, but a UI that tried would surface as a 422 the operator cannot act on.
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
    createEnsemble: vi.fn().mockResolvedValue({ ensemble_id: 'e1' }),
    getModels: vi.fn().mockResolvedValue({ models: [] }),
    suggestPersonas: vi.fn(),
    suggestName: vi.fn().mockResolvedValue({ name: 'n', description: 'd' }),
  },
}))

const mocked = api as unknown as {
  createRun: ReturnType<typeof vi.fn>
  createEnsemble: ReturnType<typeof vi.fn>
}

const runTypeBox = () =>
  screen.getByText('Run', { selector: 'label' }).querySelector('select') as HTMLSelectElement

const timesBox = () =>
  screen.getByText(/How many times/).closest('label')!
    .querySelector('input[type="number"]') as HTMLInputElement

const avatarBox = () =>
  screen.getByRole('checkbox', { name: /generate avatars/i }) as HTMLInputElement

const hybridBox = () =>
  screen.getByText(/Also compare against hybrid/).closest('label')!
    .querySelector('input[type="checkbox"]') as HTMLInputElement

async function form(onEnsembleStarted = () => {}) {
  render(
    <NewRunForm onStarted={() => {}} onEnsembleStarted={onEnsembleStarted} onCancel={() => {}} />,
  )
  await waitFor(() => expect(runTypeBox()).toBeInTheDocument())
  fireEvent.click(screen.getByRole('button', { name: /Load example/i }))
}

function chooseEnsemble() {
  fireEvent.change(runTypeBox(), { target: { value: 'ensemble' } })
}

async function submit() {
  fireEvent.click(screen.getByRole('button', { name: /Run \d+ simulations|Run simulation/i }))
}

describe('NewRunForm run type', () => {
  beforeEach(() => vi.clearAllMocks())

  it('defaults to one conversation and never mentions an ensemble', async () => {
    await form()
    expect(runTypeBox().value).toBe('single')
    // The controls are absent, not merely disabled: nothing about the default path should
    // hint that a five-times-the-cost option was nearly taken.
    expect(screen.queryByText(/How many times/)).not.toBeInTheDocument()
  })

  it('starts a single run when nobody opts in', async () => {
    await form()
    await submit()
    await waitFor(() => expect(mocked.createRun).toHaveBeenCalled())
    expect(mocked.createEnsemble).not.toHaveBeenCalled()
  })

  it('offers five replicates by default', async () => {
    await form()
    chooseEnsemble()
    expect(timesBox().value).toBe('5')
    expect(hybridBox().checked).toBe(false)
  })

  it('sends an explicit replicates-only spec', async () => {
    await form()
    chooseEnsemble()
    await submit()
    await waitFor(() => expect(mocked.createEnsemble).toHaveBeenCalled())

    const body = mocked.createEnsemble.mock.calls[0][0]
    expect(body.cells).toEqual([{ label: 'base', n: 5 }])
    expect(mocked.createRun).not.toHaveBeenCalled()
  })

  it('carries the same brief and config the single path would have sent', async () => {
    // The premise of the whole feature: a member conversation is an ORDINARY conversation.
    // If the ensemble path shaped its body differently, the runs would not be comparable
    // with anything else the operator has run.
    await form()
    chooseEnsemble()
    await submit()
    await waitFor(() => expect(mocked.createEnsemble).toHaveBeenCalled())

    const body = mocked.createEnsemble.mock.calls[0][0]
    expect(body.topic).toBeTruthy()
    expect(body.cast.length).toBeGreaterThan(0)
    expect(body.config.max_messages).toBeGreaterThan(0)
  })

  it('honours a changed replicate count', async () => {
    await form()
    chooseEnsemble()
    fireEvent.change(timesBox(), { target: { value: '3' } })
    await submit()
    await waitFor(() => expect(mocked.createEnsemble).toHaveBeenCalled())
    expect(mocked.createEnsemble.mock.calls[0][0].cells).toEqual([{ label: 'base', n: 3 }])
  })

  it('adds a hybrid cell that differs in the speaker method alone', async () => {
    await form()
    chooseEnsemble()
    fireEvent.click(hybridBox())
    await submit()
    await waitFor(() => expect(mocked.createEnsemble).toHaveBeenCalled())

    const { cells } = mocked.createEnsemble.mock.calls[0][0]
    expect(cells).toEqual([
      { label: 'base', n: 5 },
      {
        label: 'hybrid',
        n: 2,
        overrides: {
          'selection.method': 'hybrid',
          'selection.hybrid_opening_rounds': 2,
        },
      },
    ])
  })

  it('never puts a confounding key in a cell override', async () => {
    // The refusal this mirrors: turn count and fairness are rejected server-side because a
    // shorter run does not disagree, it just never arrives. Asserted here so a later edit
    // that "helpfully" varies length gets caught before it reaches a 422.
    await form()
    chooseEnsemble()
    fireEvent.click(hybridBox())
    await submit()
    await waitFor(() => expect(mocked.createEnsemble).toHaveBeenCalled())

    const { cells } = mocked.createEnsemble.mock.calls[0][0]
    const keys = cells.flatMap((c: { overrides?: Record<string, unknown> }) =>
      Object.keys(c.overrides ?? {}),
    )
    expect(keys).not.toContain('max_messages')
    expect(keys).not.toContain('selection.fairness')
    expect(keys).not.toContain('selection.stop_when_converged')
    expect(keys.every((k: string) => k.startsWith('selection.'))).toBe(true)
  })

  it('names the multiplier on the button', async () => {
    // Five conversations is five times the spend, and "Run simulation" would not say so at
    // the one moment it matters.
    await form()
    chooseEnsemble()
    expect(screen.getByRole('button', { name: /Run 5 simulations/i })).toBeInTheDocument()

    fireEvent.click(hybridBox())
    expect(screen.getByRole('button', { name: /Run 7 simulations/i })).toBeInTheDocument()
  })

  it('reports the member count before anything is spent', async () => {
    await form()
    chooseEnsemble()
    expect(screen.getByText(/5 conversations, each up to/)).toBeInTheDocument()
    expect(screen.getByText(/Nothing differs between them/)).toBeInTheDocument()

    fireEvent.click(hybridBox())
    expect(screen.getByText(/7 conversations, each up to/)).toBeInTheDocument()
    expect(
      screen.getByText(/Only the speaker method differs between the two groups/),
    ).toBeInTheDocument()
  })

  it('hands the ensemble id back, not a run id', async () => {
    // An ensemble has no transcript. Sending the operator to a run view would show them one
    // of five conversations as though it were the result.
    const onEnsembleStarted = vi.fn()
    await form(onEnsembleStarted)
    chooseEnsemble()
    await submit()
    await waitFor(() => expect(onEnsembleStarted).toHaveBeenCalledWith('e1'))
  })

  it('shows the server refusal rather than swallowing it', async () => {
    // The refusals carry the REASON — "turn count is censoring", "a cell needs at least
    // two". That reason is the useful part of the response and has to reach the operator.
    mocked.createEnsemble.mockRejectedValueOnce(
      new Error("'max_messages' may not vary across an ensemble. Turn count is censoring."),
    )
    await form()
    chooseEnsemble()
    await submit()
    await waitFor(() => expect(screen.getByText(/censoring/)).toBeInTheDocument())
  })

  it('turns avatars off for an ensemble', async () => {
    // They are generated per run, so the same cast's faces would be drawn once per
    // conversation — and image spend is not in a run's reported cost, only voice calls are, so
    // it would be multiplied AND invisible.
    await form()
    expect(avatarBox().checked).toBe(true)
    chooseEnsemble()
    expect(avatarBox().checked).toBe(false)

    await submit()
    await waitFor(() => expect(mocked.createEnsemble).toHaveBeenCalled())
    expect(mocked.createEnsemble.mock.calls[0][0].config.generate_avatars).toBe(false)
  })

  it('says why avatars are off rather than leaving it a mystery', async () => {
    await form()
    chooseEnsemble()
    expect(screen.getByText(/redrawn once per conversation/)).toBeInTheDocument()
  })

  it('is a default, not a hardcode', async () => {
    // The rule the avatar toggle already follows: a default has to be refusable. Turning it
    // back on applies to every member equally, so the comparison is unaffected.
    await form()
    chooseEnsemble()
    fireEvent.click(avatarBox())
    expect(avatarBox().checked).toBe(true)

    await submit()
    await waitFor(() => expect(mocked.createEnsemble).toHaveBeenCalled())
    expect(mocked.createEnsemble.mock.calls[0][0].config.generate_avatars).toBe(true)
  })

  it('restores the operator choice when switching back to a single run', async () => {
    // Switching away and back must not leave avatars off silently — the operator never asked
    // for that, the ensemble default did.
    await form()
    expect(avatarBox().checked).toBe(true)
    chooseEnsemble()
    expect(avatarBox().checked).toBe(false)
    fireEvent.change(runTypeBox(), { target: { value: 'single' } })
    expect(avatarBox().checked).toBe(true)
  })

  it('does not resurrect avatars the operator had already declined', async () => {
    await form()
    fireEvent.click(avatarBox())
    expect(avatarBox().checked).toBe(false)
    chooseEnsemble()
    fireEvent.change(runTypeBox(), { target: { value: 'single' } })
    expect(avatarBox().checked).toBe(false)
  })
})
