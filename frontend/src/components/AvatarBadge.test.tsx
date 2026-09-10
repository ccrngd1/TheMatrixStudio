// SPDX-License-Identifier: Apache-2.0
/**
 * Avatars moved out of the event payload into blob storage, so the badge has to render
 * two shapes: a URL (current) and inline base64 (runs recorded before the change).
 *
 * The one that is easy to break and hard to notice is the legacy path — 38 runs in a real
 * database carry `portrait_b64` and nothing else, and if that stopped rendering their
 * avatars would silently become initials placeholders, which looks like a styling choice
 * rather than a regression.
 */
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { AvatarBadge } from './AvatarBadge'
import { avatarUrl } from '../api'

describe('AvatarBadge', () => {
  it('renders the stored image from a URL', () => {
    render(<AvatarBadge name="Priya" portrait={null} portraitUrl="/api/runs/r1/agents/Priya/avatar?v=k" />)
    expect(screen.getByRole('img', { name: 'Priya' })).toHaveAttribute(
      'src', '/api/runs/r1/agents/Priya/avatar?v=k',
    )
  })

  it('still renders legacy inline base64 (runs predating blob storage)', () => {
    render(<AvatarBadge name="Dan" portrait="QUJD" />)
    expect(screen.getByRole('img', { name: 'Dan' })).toHaveAttribute(
      'src', 'data:image/png;base64,QUJD',
    )
  })

  it('prefers the URL when a run somehow has both', () => {
    // The URL is the current path; base64 on the same agent can only be stale.
    render(<AvatarBadge name="Ada" portrait="QUJD" portraitUrl="/api/runs/r1/agents/Ada/avatar?v=k" />)
    expect(screen.getByRole('img', { name: 'Ada' })).toHaveAttribute(
      'src', '/api/runs/r1/agents/Ada/avatar?v=k',
    )
  })

  it('falls back to an initials placeholder when there is no image', () => {
    // Avatars are optional eye-candy: a card must always render something.
    render(<AvatarBadge name="Marcus Webb" portrait={null} portraitUrl={null} />)
    expect(screen.queryByRole('img')).toBeNull()
    expect(screen.getByLabelText('Marcus Webb placeholder avatar')).toBeInTheDocument()
  })
})

describe('avatarUrl', () => {
  it('carries the key as a cache-busting parameter', () => {
    // The key is a content hash, so a regenerated portrait produces a different URL —
    // which is what allows the server to mark the response immutable.
    const url = avatarUrl('run-1', 'Priya', 'avatars/abc123.png')
    expect(url).toBe('/api/runs/run-1/agents/Priya/avatar?v=avatars%2Fabc123.png')
  })

  it('returns null with no key, so callers render the placeholder', () => {
    expect(avatarUrl('run-1', 'Priya', null)).toBeNull()
  })

  it('encodes names that need it', () => {
    expect(avatarUrl('run-1', 'Dr. Emily Chen', 'avatars/a.png')).toContain(
      'Dr.%20Emily%20Chen',
    )
  })
})
