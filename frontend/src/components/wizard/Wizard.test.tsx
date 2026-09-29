// SPDX-License-Identifier: Apache-2.0
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { NewRunForm } from '../../views/NewRunForm'
import { LaunchReview, Stepper } from './Wizard'

vi.mock('../../api', async () => {
  const actual = await vi.importActual<typeof import('../../api')>('../../api')
  const never = () => new Promise(() => {})
  return {
    ...actual,
    api: new Proxy({}, { get: () => never }),
  }
})

const shown = () => document.querySelector('.cc-step:not(.cc-step-off)')?.getAttribute('aria-label')

describe('the new-run wizard', () => {
  it('shows one step at a time and keeps the others mounted', () => {
    render(<NewRunForm onStarted={vi.fn()} onEnsembleStarted={vi.fn()} onCancel={vi.fn()} />)
    expect(shown()).toBe('Topic')
    expect(document.querySelectorAll('.cc-step')).toHaveLength(5)
    fireEvent.click(screen.getByRole('button', { name: 'Next: Cast' }))
    expect(shown()).toBe('Cast')
    fireEvent.click(screen.getByRole('button', { name: 'Back: Topic' }))
    expect(shown()).toBe('Topic')
  })

  it('jumps straight to any step from the stepper', () => {
    render(<NewRunForm onStarted={vi.fn()} onEnsembleStarted={vi.fn()} onCancel={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Launch' }))
    expect(shown()).toBe('Launch')
    expect(screen.queryByRole('button', { name: /^Next/ })).not.toBeInTheDocument()
  })

  it('reports the step to a caller that routes it', () => {
    const onStep = vi.fn()
    render(<NewRunForm onStarted={vi.fn()} onEnsembleStarted={vi.fn()} onCancel={vi.fn()} step={3} onStep={onStep} />)
    expect(shown()).toBe('Knowledge')
    fireEvent.click(screen.getByRole('button', { name: 'Next: Assume' }))
    expect(onStep).toHaveBeenCalledWith(4)
  })

  it('marks the current step and those before it', () => {
    render(<Stepper step={3} onStep={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Knowledge' })).toHaveAttribute('aria-current', 'step')
    expect(screen.getByRole('button', { name: 'Topic' }).className).toMatch(/cc-done/)
    expect(screen.getByRole('button', { name: 'Launch' }).className).not.toMatch(/cc-done|cc-cur/)
  })

  it('the review sends Edit to the step a choice was made on', () => {
    const onEdit = vi.fn()
    render(
      <LaunchReview
        rows={[
          { step: 1, label: 'Topic', value: 'x' },
          { step: 4, label: 'Assumptions', value: 'none' },
        ]}
        onEdit={onEdit}
      />,
    )
    const edits = screen.getAllByRole('button', { name: 'Edit' })
    expect(edits).toHaveLength(2)
    fireEvent.click(edits[1])
    expect(onEdit).toHaveBeenCalledWith(4)
  })
})
