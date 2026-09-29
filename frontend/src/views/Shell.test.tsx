// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { Dock, SettingsButton } from './Shell'
import { SessionContext } from './AuthGate'

describe('the shell', () => {
  it('the dock is the only persistent navigation, and it routes by the hash', () => {
    render(<Dock active="runs" />)
    const nav = screen.getByRole('navigation', { name: 'Main' })
    expect(within(nav).getAllByRole('button').map((b) => b.textContent)).toEqual(['Runs', 'Ensembles', 'Knowledge', 'Library'])
    expect(within(nav).getByRole('button', { name: 'Runs' })).toHaveAttribute('aria-current', 'page')
    fireEvent.click(within(nav).getByRole('button', { name: 'Library' }))
    expect(window.location.hash).toBe('#/library')
  })

  it('settings switch the theme and effects, and offer sign-out only when there is a session', () => {
    const state = { theme: 'holo' as const, setTheme: (t: string) => (seen.theme = t), fx: true, setFx: (f: boolean) => (seen.fx = f) }
    const seen: Record<string, unknown> = {}
    const { rerender } = render(<SessionContext.Provider value={{ signOut: null }}><SettingsButton {...(state as any)} /></SessionContext.Provider>)
    fireEvent.click(screen.getByRole('button', { name: 'Settings' }))
    const sheet = within(screen.getByRole('dialog', { name: 'Settings' }))
    fireEvent.click(sheet.getByRole('radio', { name: 'neon' }))
    fireEvent.click(sheet.getByRole('switch', { name: 'Effects' }))
    expect(seen).toEqual({ theme: 'neon', fx: false })
    expect(sheet.queryByRole('button', { name: 'Sign out' })).toBeNull()
    rerender(<SessionContext.Provider value={{ signOut: () => (seen.out = true) }}><SettingsButton {...(state as any)} /></SessionContext.Provider>)
    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }))
    expect(seen.out).toBe(true)
  })
})
