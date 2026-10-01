// SPDX-License-Identifier: Apache-2.0
// The ⓘ's tap target (docs/MOBILE-UI.md §7: every target ≥ 44 px), without pushing apart the lines it sits
// in. jsdom has no layout, so this reads what Hint.css declares for the button; the boxes themselves were
// measured in Chromium at 360 px.
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { Hint } from './Hint'

const css = document.createElement('style')
css.textContent = readFileSync(join(__dirname, 'Hint.css'), 'utf8')
afterEach(() => css.remove())

describe('the ⓘ', () => {
  it('answers a 44 px tap and keeps the 28 px it takes in the line', () => {
    document.head.append(css)
    render(<p>Model <Hint label="the model">Which model runs.</Hint> and more text</p>)
    const s = getComputedStyle(screen.getByRole('button', { name: 'About the model' }))
    expect([s.width, s.height]).toEqual(['44px', '44px'])
    // The margin gives back 8 px each way, so the line lays it out at 44 - 16 = 28 px, as before.
    expect([s.marginTop, s.marginRight, s.marginBottom, s.marginLeft]).toEqual(['-8px', '-8px', '-8px', '-8px'])
  })

  it('opens its sheet on a tap, and inside a label does not also toggle the checkbox', () => {
    render(<label><input type="checkbox" /> Memory <Hint label="memory">What it remembers.</Hint></label>)
    fireEvent.click(screen.getByRole('button', { name: 'About memory' }))
    expect(screen.getByRole('dialog', { name: 'About memory' })).toBeInTheDocument()
    expect(screen.getByRole('checkbox')).not.toBeChecked()
  })
})
