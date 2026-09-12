// SPDX-License-Identifier: Apache-2.0
// That the token actually reaches the API, and that a rejected one ends the session.
//
// Written because mutation testing showed nothing asserted it: deleting the line that
// sets `Authorization` left all 33 auth tests green. The whole point of the login is
// attaching that header, so its absence was the one thing untested.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, setTokenProvider, setUnauthorizedHandler } from './api'

beforeEach(() => {
  vi.restoreAllMocks()
  setTokenProvider(null)
  setUnauthorizedHandler(null)
})

function ok(body: unknown = { runs: [] }) {
  return vi.fn().mockResolvedValue({
    ok: true, status: 200, json: async () => body,
  })
}

describe('the Authorization header', () => {
  it('is attached to every request when a provider is set', async () => {
    const fetchMock = ok()
    vi.stubGlobal('fetch', fetchMock)
    setTokenProvider(async () => 'the-token')

    await api.listRuns()
    const headers = fetchMock.mock.calls[0][1].headers as Record<string, string>
    expect(headers.Authorization).toBe('Bearer the-token')
  })

  it('is attached on writes too, not only reads', async () => {
    // A GET-only implementation would leave the app read-only in a way that reads as a
    // permissions bug rather than a missing header.
    const fetchMock = ok({ run_id: 'r' })
    vi.stubGlobal('fetch', fetchMock)
    setTokenProvider(async () => 'the-token')

    await api.createRun({ topic: 't', cast: [] } as never)
    const [, init] = fetchMock.mock.calls[0]
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer the-token')
    expect(init.method).toBe('POST')
  })

  it('is omitted when there is no provider, so the local tool is unaffected', async () => {
    const fetchMock = ok()
    vi.stubGlobal('fetch', fetchMock)
    await api.listRuns()
    const headers = fetchMock.mock.calls[0][1].headers as Record<string, string>
    expect(headers.Authorization).toBeUndefined()
  })

  it('is omitted when the provider returns null rather than sending "Bearer null"', async () => {
    const fetchMock = ok()
    vi.stubGlobal('fetch', fetchMock)
    setTokenProvider(async () => null)
    await api.listRuns()
    const headers = fetchMock.mock.calls[0][1].headers as Record<string, string>
    expect(headers.Authorization).toBeUndefined()
  })

  it('re-asks the provider on every request, so a refreshed token is used', async () => {
    // A token captured once would go stale after an hour. The provider exists precisely
    // so `validToken` can refresh between requests.
    const fetchMock = ok()
    vi.stubGlobal('fetch', fetchMock)
    const tokens = ['first', 'second']
    setTokenProvider(async () => tokens.shift() ?? null)

    await api.listRuns()
    await api.listRuns()
    const first = fetchMock.mock.calls[0][1].headers as Record<string, string>
    const second = fetchMock.mock.calls[1][1].headers as Record<string, string>
    expect(first.Authorization).toBe('Bearer first')
    expect(second.Authorization).toBe('Bearer second')
  })

  it('keeps Content-Type alongside it', async () => {
    const fetchMock = ok()
    vi.stubGlobal('fetch', fetchMock)
    setTokenProvider(async () => 't')
    await api.listRuns()
    const headers = fetchMock.mock.calls[0][1].headers as Record<string, string>
    expect(headers['Content-Type']).toBe('application/json')
  })
})

describe('a rejected token', () => {
  it('invokes the unauthorized handler on 401', async () => {
    // Without this a dead session surfaces as "401: Unauthorized" in whichever panel
    // fetched first, which reads as a broken app rather than an expired login.
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false, status: 401, statusText: 'Unauthorized', json: async () => ({}),
      }),
    )
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)
    await expect(api.listRuns()).rejects.toThrow()
    expect(onUnauthorized).toHaveBeenCalledOnce()
  })

  it('does not invoke it on other errors', async () => {
    // A 500 is the server's problem, not the session's. Logging the user out would hide
    // a real failure behind a login screen.
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false, status: 500, statusText: 'Server Error', json: async () => ({}),
      }),
    )
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)
    await expect(api.listRuns()).rejects.toThrow()
    expect(onUnauthorized).not.toHaveBeenCalled()
  })
})
