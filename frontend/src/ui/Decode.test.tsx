// SPDX-License-Identifier: Apache-2.0
// Titles decode from glyphs when the screen changes (docs/MOBILE-UI.md §5.4). Only the eye may see the glyphs:
// the title has to be readable, by a screen reader and by every other test, from the first frame.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import { TopBar } from '../views/Shell'
import { FxContext } from './fx'
import { DECODE_MS } from './Decode'

const bar = (title: string, fx = true) => (
  <FxContext.Provider value={fx}>
    <TopBar title={title} code />
  </FxContext.Provider>
)
const glyphLayer = () => document.querySelector('.cc-title b [aria-hidden="true"]')

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'requestAnimationFrame', 'cancelAnimationFrame', 'performance'] })
})
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('title decode', () => {
  it('scrambles in an aria-hidden layer while the real title is in the DOM from the start', () => {
    render(bar('quiet-harbor'))
    expect(screen.getByText('quiet-harbor')).toHaveClass('sr-only')
    const layer = glyphLayer()!
    expect(layer.textContent).toHaveLength('quiet-harbor'.length)
    expect(layer.textContent).not.toBe('quiet-harbor')
    act(() => vi.advanceTimersByTime(DECODE_MS + 50))
    // Settled: the title is plain text again, no layer, nothing left scheduled.
    expect(glyphLayer()).toBeNull()
    expect(document.querySelector('.cc-title b')!.innerHTML).toBe('quiet-harbor')
    expect(vi.getTimerCount()).toBe(0)
  })

  it('decodes again when the title changes, not on every render', () => {
    const { rerender } = render(bar('Run'))
    act(() => vi.advanceTimersByTime(DECODE_MS + 50))
    rerender(bar('Run'))
    expect(glyphLayer()).toBeNull()
    // The run's codename arriving after the placeholder is a new title.
    rerender(bar('amber-lantern'))
    expect(glyphLayer()).not.toBeNull()
    expect(screen.getByText('amber-lantern')).toBeInTheDocument()
  })

  it('does nothing with FX off or reduced motion, and does not start when FX comes on', () => {
    const { rerender, unmount } = render(bar('quiet-harbor', false))
    expect(glyphLayer()).toBeNull()
    rerender(bar('quiet-harbor', true))
    expect(glyphLayer()).toBeNull()
    unmount()
    vi.stubGlobal('matchMedia', (q: string) => ({ matches: q.includes('reduce') }))
    render(bar('trusted-robot'))
    expect(glyphLayer()).toBeNull()
    expect(document.querySelector('.cc-title b')!.innerHTML).toBe('trusted-robot')
  })

  it('stops its frame loop when the screen goes mid-decode', () => {
    const { unmount } = render(bar('quiet-harbor'))
    expect(vi.getTimerCount()).toBeGreaterThan(0)
    unmount()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('leaves the studio mark alone', () => {
    render(
      <FxContext.Provider value>
        <TopBar title="Matrix Studio" brand sub="command centre" />
      </FxContext.Provider>,
    )
    expect(document.querySelector('[aria-hidden="true"]')).toBeNull()
  })
})
