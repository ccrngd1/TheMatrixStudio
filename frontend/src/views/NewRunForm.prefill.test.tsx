// SPDX-License-Identifier: Apache-2.0
/**
 * "Start over with this setup": the new-run form prefilled from an existing run.
 *
 * The failure mode these guard against is a *silent* one. The form looks complete and
 * plausible whether or not a field made it across, so a dropped conviction or a lost
 * document does not look like a bug — it looks like a conversation you set up slightly
 * differently. Every field the form can edit is therefore asserted to arrive, and the
 * submitted body is asserted to reflect edits made after loading.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { NewRunForm } from './NewRunForm'
import { api } from '../api'

vi.mock('../api', () => ({
  api: {
    getDocumentFormats: vi.fn().mockResolvedValue({
      formats: [
        { suffix: '.txt', media_type: 'txt', available: true, needs: null },
        { suffix: '.md', media_type: 'md', available: true, needs: null },
        { suffix: '.pdf', media_type: 'pdf', available: true, needs: null },
        { suffix: '.docx', media_type: 'docx', available: true, needs: null },
      ],
      max_upload_bytes: 10485760,
      max_document_chars: 400000,
    }),
    extractDocument: vi.fn(),
    createRun: vi.fn().mockResolvedValue({ run_id: 'new-run' }),
    getModels: vi.fn().mockResolvedValue({
      models: [{ id: 'model-default', label: 'Default' }, { id: 'model-from-setup', label: 'Setup' }],
      default: 'model-default',
    }),
    getRunSetup: vi.fn(),
    suggestPersonas: vi.fn(),
    suggestName: vi.fn().mockResolvedValue({ name: 'x', description: 'y' }),
  },
}))

const SETUP = {
  topic: 'Should we cut over this quarter?',
  name: 'migration-debate',
  description: 'Whether to cut over now.',
  model: 'model-from-setup',
  cast: [
    {
      name: 'Priya',
      persona: 'Staff engineer who owns the migration.',
      goals: ['Ship without a rollback', 'Keep the audit trail'],
      structured: {
        viewpoints: [
          {
            position: 'The cutover must be reversible',
            firmness: 'non-negotiable',
            evidence_that_shifts: ['A tested rollback path'],
            underlying_concern: 'I was on call for the last one',
          },
        ],
        preferences: { dismisses: ['quarterly targets'] },
      },
      document_texts: [{ title: 'plan.md', text: 'The migration plan in full.' }],
    },
    { name: 'Dan', persona: 'Product lead answering to the board.', goals: ['Hit the quarter'] },
  ],
  config: { max_messages: 7, generate_avatars: true, cognition: { enabled: true } },
}

const loadWith = (setup: unknown = SETUP, warnings: string[] = []) => {
  ;(api.getRunSetup as any).mockResolvedValue({ run_id: 'r1', setup, warnings })
  return render(<NewRunForm onStarted={() => {}} onCancel={() => {}} fromRunId="r1" />)
}

describe('NewRunForm prefilled from a run', () => {
  beforeEach(() => vi.clearAllMocks())

  it('loads the topic, the whole cast, and their goals', async () => {
    loadWith()
    await waitFor(() =>
      expect(screen.getByDisplayValue('Should we cut over this quarter?')).toBeInTheDocument(),
    )
    expect(screen.getByDisplayValue('Priya')).toBeInTheDocument()
    expect(screen.getByDisplayValue('Dan')).toBeInTheDocument()
    expect(
      screen.getByDisplayValue('Staff engineer who owns the migration.'),
    ).toBeInTheDocument()
    // Goals arrive as the form's newline-separated format, both of them.
    // One textarea per persona; Priya is first.
    const goals = screen.getAllByPlaceholderText(
      /Goals \(one per line\)/,
    )[0] as HTMLTextAreaElement
    expect(goals.value.split('\n')).toEqual([
      'Ship without a rollback',
      'Keep the audit trail',
    ])
  })

  it('loads convictions, the private concern, and the dismissal list', async () => {
    // These are the fields most likely to be dropped quietly: they live in a nested
    // `structured` block and are rendered into a line format, so a mistake anywhere in
    // that chain shows up as an empty box rather than an error.
    loadWith()
    await waitFor(() =>
      expect(
        screen.getByDisplayValue('[non-negotiable] The cutover must be reversible -> A tested rollback path'),
      ).toBeInTheDocument(),
    )
    expect(screen.getByDisplayValue('I was on call for the last one')).toBeInTheDocument()
    expect(screen.getByDisplayValue('quarterly targets')).toBeInTheDocument()
  })

  it('loads background documents as editable text', async () => {
    loadWith()
    await waitFor(() => expect(screen.getByDisplayValue('plan.md')).toBeInTheDocument())
    expect(screen.getByDisplayValue('The migration plan in full.')).toBeInTheDocument()
  })

  it("keeps the setup's model instead of the server default", async () => {
    loadWith()
    await waitFor(() => expect(screen.getByDisplayValue('Priya')).toBeInTheDocument())
    const picker = screen.getByDisplayValue('Setup') as HTMLSelectElement
    await waitFor(() => expect(picker.value).toBe('model-from-setup'))
  })

  it("keeps the setup's model even when the model list arrives last", async () => {
    /**
     * The ordering that actually bites. `getModels` and `getRunSetup` are two
     * independent requests, and in the natural mock ordering the model list lands
     * first, so a plain `setModel(m.default)` still looks correct. Deferring the list
     * until after the setup has applied is the only way to observe the clobber — and
     * that ordering is entirely plausible in the browser, where the setup response
     * may be cached and the model list may not be.
     *
     * Getting this wrong silently changes which model you are billed for.
     */
    let releaseModels: (v: unknown) => void = () => {}
    ;(api.getModels as any).mockReturnValue(
      new Promise((resolve) => {
        releaseModels = resolve
      }),
    )
    loadWith()

    // The setup has been applied; the model list has not arrived yet.
    await waitFor(() => expect(screen.getByDisplayValue('Priya')).toBeInTheDocument())

    releaseModels({
      models: [
        { id: 'model-default', label: 'Default' },
        { id: 'model-from-setup', label: 'Setup' },
      ],
      default: 'model-default',
    })

    const picker = await waitFor(() => screen.getByDisplayValue('Setup') as HTMLSelectElement)
    expect(picker.value).toBe('model-from-setup')
  })

  it('carries the turn budget and avatar setting rather than re-defaulting them', async () => {
    // Avatars cost money per persona, so silently flipping the flag either way is a
    // real cost or a real loss.
    loadWith()
    await waitFor(() => expect(screen.getByDisplayValue('Priya')).toBeInTheDocument())
    expect(screen.getByDisplayValue('7')).toBeInTheDocument()
    const avatarBox = screen.getByRole('checkbox', { name: /avatar/i }) as HTMLInputElement
    expect(avatarBox.checked).toBe(true)
  })

  it('says it is a new conversation and the original is untouched', async () => {
    loadWith()
    await waitFor(() => expect(screen.getByText(/Prefilled from/i)).toBeInTheDocument())
    const banner = screen.getByText(/Prefilled from/i)
    expect(banner.textContent).toMatch(/brand-new conversation/i)
    expect(banner.textContent).toMatch(/original is untouched/i)
    expect(banner.textContent).toMatch(/nothing from its transcript carries over/i)
  })

  it("surfaces the server's warnings about what could not be carried over", async () => {
    // A cast-wide document has nowhere to go in a create-run request. Dropping it
    // without a word is the thing to avoid: the new run would quietly lack context
    // the old one had.
    loadWith(SETUP, ['2 cast-wide document(s) could not be carried over (board-memo.txt).'])
    await waitFor(() =>
      expect(screen.getByText(/cast-wide document\(s\) could not be carried over/i)).toBeInTheDocument(),
    )
    expect(screen.getByText(/board-memo\.txt/)).toBeInTheDocument()
  })

  it('submits what is on screen after editing, as a fresh run', async () => {
    loadWith()
    await waitFor(() => expect(screen.getByDisplayValue('Priya')).toBeInTheDocument())

    // The reason to come here at all: change the premise, then run it.
    fireEvent.change(screen.getByDisplayValue('Should we cut over this quarter?'), {
      target: { value: 'Should we cut over NEXT quarter?' },
    })
    fireEvent.click(screen.getByRole('button', { name: /run simulation/i }))

    await waitFor(() => expect(api.createRun).toHaveBeenCalledTimes(1))
    const body = (api.createRun as any).mock.calls[0][0]
    expect(body.topic).toBe('Should we cut over NEXT quarter?')
    expect(body.cast.map((c: { name: string }) => c.name)).toEqual(['Priya', 'Dan'])
    // The edited premise goes out with the convictions intact.
    expect(body.cast[0].structured.viewpoints[0].firmness).toBe('non-negotiable')
    expect(body.cast[0].structured.viewpoints[0].underlying_concern).toBe(
      'I was on call for the last one',
    )
    expect(body.cast[0].document_texts[0].text).toBe('The migration plan in full.')
    // Not a branch: nothing in the create-run body ties it to the source run.
    expect(JSON.stringify(body)).not.toMatch(/parent_run_id|branch_turn|from_turn/)
  })

  it('reports a failed load instead of showing a silently blank form', async () => {
    ;(api.getRunSetup as any).mockRejectedValue(new Error('404: Run not found'))
    render(<NewRunForm onStarted={() => {}} onCancel={() => {}} fromRunId="ghost" />)
    await waitFor(() => expect(screen.getByText(/Run not found/)).toBeInTheDocument())
    // And it does not claim to have prefilled anything.
    expect(screen.queryByText(/Prefilled from/i)).toBeNull()
  })

  it('is an ordinary blank form when no source run is given', async () => {
    render(<NewRunForm onStarted={() => {}} onCancel={() => {}} />)
    await waitFor(() => expect(api.getModels).toHaveBeenCalled())
    expect(api.getRunSetup).not.toHaveBeenCalled()
    expect(screen.queryByText(/Prefilled from/i)).toBeNull()
  })
})
