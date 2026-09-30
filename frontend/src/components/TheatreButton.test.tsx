// SPDX-License-Identifier: Apache-2.0
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { THEATRE_LABEL, TheatreButton, theatreUrl } from './TheatreButton'

afterEach(() => vi.restoreAllMocks())

const byName = () => screen.getByRole('button', { name: new RegExp(THEATRE_LABEL) })

describe('TheatreButton', () => {
  it('is greyed out while the run is still going, and says why', () => {
    render(<TheatreButton runId="r1" running empty={false} />)
    expect(byName()).toBeDisabled()
    expect(byName()).toHaveAttribute('aria-description', expect.stringMatching(/when the run ends/))
  })

  it('is greyed out when there is nothing to replay', () => {
    render(<TheatreButton runId="r1" running={false} empty />)
    expect(byName()).toBeDisabled()
  })

  it('in the menu, shows the reason it is off rather than hiding it', () => {
    render(<TheatreButton variant="menu" runId="r1" running empty={false} />)
    expect(byName()).toBeDisabled()
    expect(screen.getByText(/Available when the run ends/)).toBeInTheDocument()
  })

  it('opens the theatre for this run in a new tab, keeping the opener so the sign-in carries over', () => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => null)
    const opened = vi.fn()
    render(<TheatreButton variant="menu" runId="quiet harbor/1" running={false} empty={false} onOpened={opened} />)
    fireEvent.click(byName())
    expect(open).toHaveBeenCalledTimes(1)
    const [url, target, features] = open.mock.calls[0]
    expect(url).toBe(theatreUrl('quiet harbor/1'))
    expect(url).toBe('/theatre.html?run=quiet%20harbor%2F1')
    expect(target).toBe('_blank')
    // `noopener` would drop the copy of sessionStorage the new tab needs to be signed in.
    expect(String(features ?? '')).not.toMatch(/noopener/)
    expect(opened).toHaveBeenCalledTimes(1)
  })
})
