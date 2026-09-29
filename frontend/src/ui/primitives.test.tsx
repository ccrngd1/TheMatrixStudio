// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { Btn, Hex, Meter, Panel, Seg, Sheet, Tag, Ticks, Toggle, identityOf, initials } from './primitives'
import { THEMES, applyTheme } from './theme'

describe('command-centre primitives', () => {
  it('panels carry their status edge and live sweep as the stylesheet expects', () => {
    const { container } = render(<Panel edge="running" live>x</Panel>)
    const el = container.firstElementChild as HTMLElement
    expect(el.className).toContain('cc-card')
    expect(el.className).toContain('cc-livecard')
    expect(el.dataset.s).toBe('running')
    // The phase variable, so the sweep resumes in phase on re-render (§5.4).
    expect(el.style.getPropertyValue('--ph')).toMatch(/^-\d+ms$/)
  })

  it('never colour alone: a tag carries its words, ticks and meters carry an accessible count', () => {
    render(<><Tag tone="live" pulse>Live 06/12</Tag><Ticks n={6} max={12} live /><Meter value={0.8} warn /></>)
    expect(screen.getByText('Live 06/12')).toBeInTheDocument()
    expect(screen.getByRole('img', { name: '6 of 12 turns' })).toBeInTheDocument()
    expect(screen.getByRole('meter')).toHaveAttribute('aria-valuenow', '80')
  })

  it('switches to a meter past 30 turns, where ticks stop being legible', () => {
    render(<Ticks n={20} max={40} />)
    expect(screen.getByRole('meter')).toHaveAttribute('aria-valuenow', '50')
  })

  it('hex tokens show initials in a stable identity slot', () => {
    expect(initials('Maria de la Cruz')).toBe('MD')
    expect(identityOf('B', ['A', 'B'])).toBe('a2')
    expect(identityOf('Z', ['A'])).toBe('a0')
    render(<Hex name="Ana Silva" slot="a1" ring="var(--hold)" />)
    expect(screen.getByLabelText('Ana Silva')).toHaveTextContent('AS')
  })

  it('toggles and segmented controls are real switches and radios', () => {
    const onT = vi.fn(); const onS = vi.fn()
    render(<>
      <Toggle on={false} onChange={onT} label="Effects" />
      <Seg label="Theme" value="holo" onChange={onS} options={[{ value: 'holo', label: 'Holo' }, { value: 'neon', label: 'Neon' }]} />
    </>)
    fireEvent.click(screen.getByRole('switch', { name: 'Effects' }))
    expect(onT).toHaveBeenCalledWith(true)
    fireEvent.click(screen.getByRole('radio', { name: 'Neon' }))
    expect(onS).toHaveBeenCalledWith('neon')
  })

  it('sheets close on Escape and on the scrim', () => {
    const onClose = vi.fn()
    render(<Sheet title="Dossier" onClose={onClose}>body</Sheet>)
    expect(screen.getByRole('dialog', { name: 'Dossier' })).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    // Portalled out of the component's container, so it is found in the document.
    fireEvent.click(document.querySelector('.cc-scrim')!)
    expect(onClose).toHaveBeenCalledTimes(2)
  })

  it('buttons are type=button unless told otherwise, so none submits a form by accident', () => {
    render(<Btn variant="primary">Launch</Btn>)
    expect(screen.getByRole('button', { name: 'Launch' })).toHaveAttribute('type', 'button')
  })

  it('a theme is set on <html>, where the stylesheet and the Tailwind bridge both read it', () => {
    for (const t of THEMES) {
      applyTheme(t)
      expect(document.documentElement.dataset.theme).toBe(t)
    }
  })
})
