// SPDX-License-Identifier: Apache-2.0
/**
 * Tests for the new-run form's option hints.
 *
 * The point of these is coverage, not prose-checking: every optional toggle on this
 * screen must carry an explanation, because an unexplained switch either gets left
 * off forever or gets turned on without the operator knowing it costs money. The
 * `every option has a hint` test is the one that will catch a future option added
 * without one.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { NewRunForm } from './NewRunForm'
import { api } from '../api'

vi.mock('../api', () => ({
  api: {
    createRun: vi.fn(),
    getModels: vi.fn().mockResolvedValue({ models: [] }),
    suggestName: vi.fn().mockResolvedValue({ name: 'trusted-robot', description: 'x' }),
  },
}))

describe('NewRunForm option hints', () => {
  beforeEach(() => vi.clearAllMocks())

  /**
   * Renders with the collapsible sections expanded.
   *
   * Cognition and Summary are collapsed by default — correctly, since they are
   * opt-in — so their sub-option hints are not mounted until the operator opens
   * them. Expanding here mirrors what a user does before reading them.
   */
  const renderForm = () => {
    const r = render(<NewRunForm onStarted={() => {}} onCancel={() => {}} />)
    fireEvent.click(screen.getByText(/Summary options/))
    fireEvent.click(screen.getByText(/Cognition \(introspectable engine\)/))
    return r
  }

  it('keeps the opt-in sections collapsed until asked', () => {
    // Worth locking: the default screen should not be a wall of switches.
    render(<NewRunForm onStarted={() => {}} onCancel={() => {}} />)
    expect(
      screen.queryByRole('button', { name: 'About reflection' }),
    ).not.toBeInTheDocument()
  })

  it('gives every optional toggle a hint the operator can read', () => {
    renderForm()
    // Named via aria-label so a screen reader announces which option is being
    // explained, rather than a row of identical "more information" buttons.
    for (const label of [
      'max messages',
      'generate avatars',
      'summary options',
      'cognition',
      'memory stream',
      'reflection',
      'dynamic goals',
      'relationships',
    ]) {
      expect(
        screen.getByRole('button', { name: `About ${label}` }),
        `no hint for "${label}"`,
      ).toBeInTheDocument()
    }
  })

  it('explains the cost consequence of cognition, not just what it does', () => {
    renderForm()
    // An operator deciding whether to enable this needs to know it costs more; a
    // description that only says what a feature does is not a decision aid.
    const tips = screen.getAllByRole('tooltip').map((t) => t.textContent ?? '')
    expect(tips.some((t) => /20-40% more tokens/.test(t))).toBe(true)
  })

  it('states when a feature is purely cosmetic', () => {
    renderForm()
    const tips = screen.getAllByRole('tooltip').map((t) => t.textContent ?? '')
    expect(tips.some((t) => /cosmetic/i.test(t))).toBe(true)
  })

  it('keeps hint text in the DOM so it is reachable without a mouse', () => {
    // Deliberate design choice: hidden with opacity rather than unmounted, so a
    // screen reader and find-in-page can reach it. A hover-only tooltip explains
    // nothing to a keyboard user and cannot be asserted at all.
    renderForm()
    expect(screen.getAllByRole('tooltip').length).toBeGreaterThanOrEqual(8)
  })

  it('hint buttons do not submit the form', () => {
    // They live inside a form; a bare <button> would default to type="submit" and
    // start a run when the operator only wanted to read the explanation.
    renderForm()
    const hint = screen.getByRole('button', { name: 'About cognition' })
    expect(hint).toHaveAttribute('type', 'button')
    expect(api.createRun).not.toHaveBeenCalled()
  })

  it('associates each hint with its own control', () => {
    // Guards against the copy drifting onto the wrong option, which is worse than
    // no hint: the operator would be confidently misinformed.
    renderForm()
    const reflection = screen
      .getByRole('button', { name: 'About reflection' })
      .closest('label')
    expect(reflection).not.toBeNull()
    expect(within(reflection as HTMLElement).getByRole('tooltip').textContent).toMatch(
      /every fourth turn/i,
    )
  })

  // ------------------------------------------------------------------
  // Cognition defaults, and the two config blocks that were unreachable
  // ------------------------------------------------------------------

  it('has cognition on with every sub-feature by default', () => {
    // A run without cognition cannot answer "why did it say that?" -- the dossier
    // and the why-trace are both empty -- so the interactive default should be on.
    // The ENGINE default stays off (CognitionConfig) so CLI runs are unchanged.
    renderForm()
    for (const name of [
      /Enable cognition/,
      /Memory stream/,
      /Reflection/,
      /Dynamic goals/,
      /Relationships/,
    ]) {
      expect(screen.getByRole('checkbox', { name })).toBeChecked()
    }
  })

  it('lets a persona be given convictions and background documents', () => {
    // Both were config-file only before this: shipped in v0.5.0 and unreachable
    // from the UI, which made them effectively invisible to anyone not editing JSON.
    renderForm()
    fireEvent.click(screen.getByText(/Convictions & background documents/))
    expect(screen.getByPlaceholderText(/No feature may add an external service/)).toBeInTheDocument()
    expect(screen.getByPlaceholderText(/retrieval answer quality/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /paste document/ })).toBeInTheDocument()
  })

  it('explains what convictions are for, not just what the box is', () => {
    renderForm()
    fireEvent.click(screen.getByText(/Convictions & background documents/))
    const tips = screen.getAllByRole('tooltip').map((t) => t.textContent ?? '')
    // The distinction that justifies the whole feature.
    expect(tips.some((t) => /satisfiable/.test(t) && /defended/.test(t))).toBe(true)
    // And the one about declining to WEIGH rather than to engage.
    expect(tips.some((t) => /decline/i.test(t) && /weigh/i.test(t))).toBe(true)
  })

  it('adds and removes pasted documents', () => {
    renderForm()
    fireEvent.click(screen.getByText(/Convictions & background documents/))
    fireEvent.click(screen.getByRole('button', { name: /paste document/ }))
    expect(screen.getByPlaceholderText(/Paste the document text here/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'remove' }))
    expect(screen.queryByPlaceholderText(/Paste the document text here/)).not.toBeInTheDocument()
  })

  it('does not enable personas or retrieval when the cast authored neither', () => {
    // Enabling a feature nobody configured would cost tokens for an empty prompt
    // block, so the config must stay as small as it was before these existed.
    renderForm()
    fireEvent.change(screen.getByPlaceholderText(/What should the cast discuss/i), {
      target: { value: 'a topic' },
    })
    fireEvent.change(screen.getByPlaceholderText('Name'), { target: { value: 'Ada' } })
    fireEvent.change(screen.getByPlaceholderText(/Persona description/), {
      target: { value: 'an ethicist' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Run simulation/ }))

    const body = (api.createRun as ReturnType<typeof vi.fn>).mock.calls[0]?.[0]
    expect(body.config.personas).toBeUndefined()
    expect(body.config.retrieval).toBeUndefined()
    expect(body.cast[0].structured).toBeUndefined()
  })

  it('sends convictions and turns the personas feature on when authored', () => {
    renderForm()
    fireEvent.change(screen.getByPlaceholderText(/What should the cast discuss/i), { target: { value: 'a topic' } })
    fireEvent.change(screen.getByPlaceholderText('Name'), { target: { value: 'Ada' } })
    fireEvent.change(screen.getByPlaceholderText(/Persona description/), {
      target: { value: 'an ethicist' },
    })
    fireEvent.click(screen.getByText(/Convictions & background documents/))
    fireEvent.change(screen.getByPlaceholderText(/No feature may add an external service/), {
      target: { value: '[firm] consent comes first -> a signed waiver' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Run simulation/ }))

    const body = (api.createRun as ReturnType<typeof vi.fn>).mock.calls[0]?.[0]
    expect(body.config.personas).toEqual({ enabled: true })
    expect(body.cast[0].structured.viewpoints[0]).toEqual({
      position: 'consent comes first',
      firmness: 'firm',
      evidence_that_shifts: ['a signed waiver'],
    })
  })

  it('sends pasted documents and turns retrieval on when authored', () => {
    renderForm()
    fireEvent.change(screen.getByPlaceholderText(/What should the cast discuss/i), { target: { value: 'a topic' } })
    fireEvent.change(screen.getByPlaceholderText('Name'), { target: { value: 'Ada' } })
    fireEvent.change(screen.getByPlaceholderText(/Persona description/), {
      target: { value: 'an ethicist' },
    })
    fireEvent.click(screen.getByText(/Convictions & background documents/))
    fireEvent.click(screen.getByRole('button', { name: /paste document/ }))
    fireEvent.change(screen.getByPlaceholderText(/Paste the document text here/), {
      target: { value: 'The policy requires written consent.' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Run simulation/ }))

    const body = (api.createRun as ReturnType<typeof vi.fn>).mock.calls[0]?.[0]
    expect(body.config.retrieval).toEqual({ enabled: true })
    expect(body.cast[0].document_texts[0].text).toMatch(/written consent/)
    // An untitled paste still gets a usable title rather than being dropped.
    expect(body.cast[0].document_texts[0].title).toBeTruthy()
  })
})