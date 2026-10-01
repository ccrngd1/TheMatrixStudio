// SPDX-License-Identifier: Apache-2.0
// The frame's atmosphere (docs/MOBILE-UI.md §5.4, §7 stage 6): the floor behind the view, and the boot log over
// it. The boot is the one effect that covers the app, so most of this is about it getting out of the way: once a
// session, never with FX off or reduced motion, gone within 1.5 s, ended by any tap or key, and never in front of
// an app that has not rendered.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { AppFrame } from '../views/Shell'
import { BOOT_FADE_MS, BOOT_SHOW_MS } from './Boot'

const boot = () => document.getElementById('cc-boot')
const frame = (fx: boolean) => (
  <AppFrame fx={fx} theme="matrix">
    <button type="button">The app</button>
  </AppFrame>
)

function reduceMotion() {
  vi.stubGlobal('matchMedia', (q: string) => ({ matches: q.includes('reduce'), addEventListener() {}, removeEventListener() {} }))
}

beforeEach(() => {
  sessionStorage.clear()
  window.location.hash = '#/runs'
  HTMLCanvasElement.prototype.getContext = (() => null) as unknown as HTMLCanvasElement['getContext']
  vi.useFakeTimers()
})
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('the boot log', () => {
  it('shows over an app that has already rendered, and says only what the client knows', () => {
    render(frame(true))
    expect(boot()).not.toBeNull()
    // The app is there beneath it from the first frame, not waiting for it.
    expect(screen.getByRole('button', { name: 'The app' })).toBeInTheDocument()
    // Decoration: nothing a screen reader is told about.
    expect(boot()).toHaveAttribute('aria-hidden', 'true')
    const text = boot()!.textContent!
    expect(text).toMatch(/theme \.+ matrix/)
    expect(text).toMatch(/effects \.+ on/)
    expect(text).toMatch(/view \.+ runs/)
    // The prototype counted live simulations; the frame cannot know that when it mounts.
    expect(text).not.toMatch(/live|simulation|\d/i)
  })

  it('is gone within 1.5 s, and takes its timer and key listener with it', () => {
    const removed = vi.spyOn(window, 'removeEventListener')
    render(frame(true))
    expect(BOOT_SHOW_MS + BOOT_FADE_MS).toBeLessThanOrEqual(1500)
    act(() => vi.advanceTimersByTime(BOOT_SHOW_MS))
    expect(boot()).toHaveClass('cc-out')
    act(() => vi.advanceTimersByTime(BOOT_FADE_MS))
    expect(boot()).toBeNull()
    expect(removed).toHaveBeenCalledWith('keydown', expect.any(Function))
    expect(vi.getTimerCount()).toBe(0)
  })

  it('shows once per browser session', () => {
    const first = render(frame(true))
    expect(boot()).not.toBeNull()
    first.unmount()
    // A reload in the same tab: sessionStorage remembers it was shown.
    render(frame(true))
    expect(boot()).toBeNull()
  })

  it('never shows with FX off, and goes at once if FX is switched off while it shows', () => {
    const off = render(frame(false))
    expect(boot()).toBeNull()
    off.unmount()
    // Not shown is not spent: with FX on, the session still gets it once.
    const on = render(frame(true))
    expect(boot()).not.toBeNull()
    on.rerender(frame(false))
    expect(boot()).toBeNull()
  })

  it('never shows when reduced motion is asked for', () => {
    reduceMotion()
    render(frame(true))
    expect(boot()).toBeNull()
  })

  it('skips with ?noboot, the prototype flag (§5.4)', () => {
    window.history.replaceState(null, '', '/?noboot#/runs')
    try {
      render(frame(true))
      expect(boot()).toBeNull()
    } finally {
      window.history.replaceState(null, '', '/#/runs')
    }
  })

  it('a key ends it, and still reaches the app', () => {
    const seen = vi.fn()
    window.addEventListener('keydown', seen)
    render(frame(true))
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(boot()).toHaveClass('cc-out')
    expect(seen).toHaveBeenCalled()
    act(() => vi.advanceTimersByTime(BOOT_FADE_MS))
    expect(boot()).toBeNull()
    window.removeEventListener('keydown', seen)
  })

  it('a tap ends it, and it stops taking taps while it fades', () => {
    render(frame(true))
    fireEvent.pointerDown(boot()!)
    // `.cc-out` is `pointer-events:none` (command.css), so the next tap reaches the app.
    expect(boot()).toHaveClass('cc-out')
    act(() => vi.advanceTimersByTime(BOOT_FADE_MS))
    expect(boot()).toBeNull()
  })
})

describe('the floor', () => {
  it('sits behind the view, hidden from screen readers, phased once for the life of the frame', () => {
    const { container, rerender } = render(frame(true))
    const floor = document.getElementById('cc-floor')!
    expect(floor).toHaveAttribute('aria-hidden', 'true')
    // Before the view in document order: the view (z-index 1) paints over it, as over the rain.
    expect(floor.nextElementSibling?.id).toBe('cc-view')
    const ph = floor.style.getPropertyValue('--ph')
    expect(ph).toMatch(/^-\d+ms$/)
    act(() => vi.advanceTimersByTime(700))
    rerender(frame(true))
    // A changed delay would move the running loop (§5.4): the phase must survive a re-render.
    expect(document.getElementById('cc-floor')!.style.getPropertyValue('--ph')).toBe(ph)
    expect(container.querySelector('.cc-app')).not.toHaveClass('cc-nofx')
  })

  it('FX off marks the frame, which is what hides the rain, the floor and the scanlines', () => {
    const { container } = render(frame(false))
    expect(container.querySelector('.cc-app')).toHaveClass('cc-nofx')
  })
})
