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
})
