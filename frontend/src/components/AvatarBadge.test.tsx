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
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { AvatarBadge } from './AvatarBadge'
import { avatarUrl, setTokenProvider } from '../api'

// jsdom implements neither of these, and the badge now depends on both.
beforeEach(() => {
  let n = 0
  ;(URL as unknown as { createObjectURL: (b: Blob) => string }).createObjectURL = () =>
    `blob:avatar-${++n}`
  setTokenProvider(null)
  vi.restoreAllMocks()
})

function mockAvatarFetch(ok = true) {
  const fetchMock = vi.fn(async () => ({
    ok,
    blob: async () => new Blob(['png']),
  })) as unknown as typeof fetch
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock as unknown as ReturnType<typeof vi.fn>
}

describe('AvatarBadge', () => {
  it('FETCHES the stored image rather than linking to it', async () => {
    // An `<img src>` cannot send an `Authorization` header, and the avatar route requires
    // one — linking to the URL produced a 401 and a broken image for every avatar.
    const fetchMock = mockAvatarFetch()
    render(
      <AvatarBadge name="Priya" portrait={null}
        portraitUrl="/api/runs/r1/agents/Priya/avatar?v=k1" />,
    )
    await waitFor(() =>
      expect(screen.getByRole('img', { name: 'Priya' })).toHaveAttribute(
        'src', expect.stringContaining('blob:'),
      ),
    )
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/runs/r1/agents/Priya/avatar?v=k1', expect.anything(),
    )
  })

  it('sends the session token with the image request', async () => {
    // The whole point. Through the same provider every other API call uses, so a request
    // cannot be written that forgets it.
    const fetchMock = mockAvatarFetch()
    setTokenProvider(async () => 'tok-123')
    render(
      <AvatarBadge name="Priya" portrait={null}
        portraitUrl="/api/runs/r1/agents/Priya/avatar?v=k2" />,
    )
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer tok-123')
  })

  it('falls back to initials when the image cannot be fetched', async () => {
    // A missing avatar is not an error worth showing: this is what every run that never
    // generated one already does.
    mockAvatarFetch(false)
    render(
      <AvatarBadge name="Marcus Webb" portrait={null}
        portraitUrl="/api/runs/r1/agents/Marcus/avatar?v=k3" />,
    )
    await waitFor(() =>
      expect(screen.getByLabelText('Marcus Webb placeholder avatar')).toBeInTheDocument(),
    )
    expect(screen.queryByRole('img')).toBeNull()
  })

  it('still renders legacy inline base64 (runs predating blob storage)', () => {
    render(<AvatarBadge name="Dan" portrait="QUJD" />)
    expect(screen.getByRole('img', { name: 'Dan' })).toHaveAttribute(
      'src', 'data:image/png;base64,QUJD',
    )
  })

  it('prefers the fetched image when a run somehow has both', async () => {
    // The stored image is the current one; base64 on the same agent can only be stale.
    mockAvatarFetch()
    render(
      <AvatarBadge name="Ada" portrait="QUJD"
        portraitUrl="/api/runs/r1/agents/Ada/avatar?v=k4" />,
    )
    await waitFor(() =>
      expect(screen.getByRole('img', { name: 'Ada' })).toHaveAttribute(
        'src', expect.stringContaining('blob:'),
      ),
    )
  })

  it('shows legacy base64 while the fetch is still in flight, not a placeholder', async () => {
    // A run with both should never flicker to initials: the stale image is better than no
    // image, and this is the only ordering where the two sources are both live.
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})) as unknown as typeof fetch)
    render(
      <AvatarBadge name="Dan" portrait="QUJD"
        portraitUrl="/api/runs/r1/agents/Dan/avatar?v=k5" />,
    )
    expect(screen.getByRole('img', { name: 'Dan' })).toHaveAttribute(
      'src', 'data:image/png;base64,QUJD',
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
