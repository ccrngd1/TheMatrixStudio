// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from 'vitest'
import { colorForName, initials } from './avatar'

describe('avatar placeholder', () => {
  it('produces stable initials', () => {
    expect(initials('Ada')).toBe('AD')
    expect(initials('Ada Lovelace')).toBe('AL')
  })

  it('produces a deterministic color per name', () => {
    expect(colorForName('Ada')).toBe(colorForName('Ada'))
    expect(colorForName('Ada')).toMatch(/^#[0-9a-f]{6}$/)
  })
})
