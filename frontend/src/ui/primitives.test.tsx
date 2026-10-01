// SPDX-License-Identifier: Apache-2.0
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { Btn, Hex, Meter, Panel, PanelButton, Seg, Sheet, Tag, Ticks, Toggle, identityOf, initials } from './primitives'
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

  it('the sheet close button is a 44 px target that keeps the 36 px icon button’s place in the header', () => {
    // jsdom has no layout, but it does cascade a stylesheet: this reads what primitives.css declares. The
    // boxes themselves were measured in Chromium at 360 px.
    const css = document.createElement('style')
    css.textContent = readFileSync(join(__dirname, 'primitives.css'), 'utf8')
    document.head.append(css)
    render(<Sheet title="Dossier" onClose={() => {}}>body</Sheet>)
    const close = screen.getByRole('button', { name: 'Close' })
    expect(close).toHaveClass('cc-icon')
    const s = getComputedStyle(close)
    expect([s.width, s.height]).toEqual(['44px', '44px'])
    // 4 px given back on every side: the header lays it out at 36 px, as it did before.
    expect([s.marginTop, s.marginRight, s.marginBottom, s.marginLeft]).toEqual(['-4px', '-4px', '-4px', '-4px'])
    css.remove()
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

// The loop phase trick (§5.4). Changing `animation-delay` under a running loop moves it, so each element takes
// its phase when its loop starts and keeps it through re-renders; only a loop that starts anew takes a new one.
describe('loop phase in the primitives', () => {
  // A whole number of 12 s cycles, so the phase at T0 + x ms is exactly -x ms.
  const T0 = 12000 * 100000
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(T0 + 1000)
  })
  afterEach(() => vi.useRealTimers())
  const ph = (el: Element | null) => (el as HTMLElement).style.getPropertyValue('--ph')
  const later = (ms: number) => vi.setSystemTime(Date.now() + ms)

  it('a live panel, a live panel button and a primary button keep their phase through re-renders', () => {
    const ui = (label: string) => (
      <>
        <Panel live edge="running" data-testid="panel">{label}</Panel>
        <PanelButton live edge="running">{label} card</PanelButton>
        <Btn variant="primary">{label} launch</Btn>
      </>
    )
    const { rerender, unmount } = render(ui('one'))
    const els = () => [screen.getByTestId('panel'), screen.getByRole('button', { name: /card/ }),
      screen.getByRole('button', { name: /launch/ })]
    expect(els().map(ph)).toEqual(['-1000ms', '-1000ms', '-1000ms'])
    later(700)
    rerender(ui('two'))
    expect(screen.getByText('two')).toBeInTheDocument()
    expect(els().map(ph)).toEqual(['-1000ms', '-1000ms', '-1000ms'])
    // A newly mounted one starts its loop now, so it takes now's phase.
    unmount()
    render(ui('three'))
    expect(els().map(ph)).toEqual(['-1700ms', '-1700ms', '-1700ms'])
  })

  it('a panel that goes live later takes the phase of that moment, not of its mount', () => {
    const { rerender, container } = render(<Panel>x</Panel>)
    expect(ph(container.firstElementChild)).toBe('')
    later(700)
    rerender(<Panel live>x</Panel>)
    expect(ph(container.firstElementChild)).toBe('-1700ms')
    later(500)
    rerender(<Panel live className="moved">x</Panel>)
    expect(ph(container.firstElementChild)).toBe('-1700ms')
  })

  it('a tag’s pulse keeps its phase through re-renders and takes a fresh one when it turns on again', () => {
    const { rerender, container } = render(<Tag tone="live" pulse>Live 01</Tag>)
    const dot = () => container.querySelector('.cc-pulse')
    expect(ph(dot())).toBe('-1000ms')
    later(700)
    rerender(<Tag tone="live" pulse>Live 02</Tag>)
    expect(ph(dot())).toBe('-1000ms')
    rerender(<Tag tone="live">Live 02</Tag>)
    expect(dot()).toBeNull()
    later(300)
    rerender(<Tag tone="live" pulse>Live 03</Tag>)
    expect(ph(dot())).toBe('-2000ms')
  })

  it('the tick head keeps its phase within a turn and takes a fresh one on the tick it moves to', () => {
    const { rerender, container } = render(<Ticks n={3} max={12} live />)
    const head = () => container.querySelector('.cc-head')
    expect(head()).toBe(container.querySelectorAll('i')[2])
    expect(ph(head())).toBe('-1000ms')
    later(700)
    rerender(<Ticks n={3} max={12} live />)
    expect(ph(head())).toBe('-1000ms')
    later(300)
    rerender(<Ticks n={4} max={12} live />)
    expect(head()).toBe(container.querySelectorAll('i')[3])
    expect(ph(head())).toBe('-2000ms')
    // The tick it left is no longer pulsing and carries no phase.
    expect(ph(container.querySelectorAll('i')[2])).toBe('')
  })

  it('a hex token keeps its glow’s phase while active and takes a fresh one each time it lights', () => {
    const hex = (active: boolean, ring = 'var(--hold)') => <Hex name="Ana Silva" slot="a1" ring={ring} active={active} />
    const { rerender } = render(hex(true))
    const el = () => screen.getByLabelText('Ana Silva')
    expect(ph(el())).toBe('-1000ms')
    later(700)
    rerender(hex(true, 'var(--support)'))
    expect(ph(el())).toBe('-1000ms')
    rerender(hex(false))
    expect(ph(el())).toBe('')
    later(300)
    rerender(hex(true))
    expect(ph(el())).toBe('-2000ms')
  })
})
