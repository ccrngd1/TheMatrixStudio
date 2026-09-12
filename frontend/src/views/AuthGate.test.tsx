// SPDX-License-Identifier: Apache-2.0
// The login gate, and specifically that it FAILS CLOSED.
//
// The deployed page was open to the world because there was no gate at all. The way a
// gate like this fails is not by being absent but by being bypassed on an error path — a
// config fetch that 500s, malformed JSON, a missing pool — so those cases are asserted
// before the happy one.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { AuthGate } from './AuthGate'
import { resetConfigCache } from '../lib/config'
import { storeTokens } from '../lib/auth'

const PROTECTED = 'the application'

function child() {
  return <div>{PROTECTED}</div>
}

function respond(body: unknown, status = 200) {
  return vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  })
}

const DEPLOYED = {
  hostedUiUrl: 'https://pool.auth.us-east-1.amazoncognito.com',
  clientId: 'client123',
  userPoolId: 'us-east-1_abc',
  authRequired: true,
}

beforeEach(() => {
  sessionStorage.clear()
  resetConfigCache()
  vi.restoreAllMocks()
  window.history.replaceState({}, '', '/')
})

afterEach(() => sessionStorage.clear())

// --------------------------------------------------------------------------- //
// Fails closed
// --------------------------------------------------------------------------- //

describe('the gate fails closed', () => {
  it('hides the app when the config fetch fails', async () => {
    // A 500 must NOT become "no login needed". That is exactly how a deployment ends up
    // open, and it is the plausible bug: the natural `catch` returns a default.
    vi.stubGlobal('fetch', respond({}, 500))
    render(<AuthGate>{child()}</AuthGate>)
    await waitFor(() => expect(screen.getByText(/Configuration error/i)).toBeTruthy())
    expect(screen.queryByText(PROTECTED)).toBeNull()
  })

  it('hides the app when the config is malformed', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => {
          throw new Error('not json')
        },
      }),
    )
    render(<AuthGate>{child()}</AuthGate>)
    await waitFor(() => expect(screen.getByText(/Configuration error/i)).toBeTruthy())
    expect(screen.queryByText(PROTECTED)).toBeNull()
  })

  it('shows the login screen, not the app, when there is no token', async () => {
    vi.stubGlobal('fetch', respond(DEPLOYED))
    render(<AuthGate>{child()}</AuthGate>)
    await waitFor(() => expect(screen.getByRole('button', { name: /sign in/i })).toBeTruthy())
    expect(screen.queryByText(PROTECTED)).toBeNull()
  })

  it('shows the login screen when the stored token is expired and cannot refresh', async () => {
    storeTokens({
      idToken: 'stale',
      accessToken: 'a',
      expiresAt: Math.floor(Date.now() / 1000) - 10,
      // No refresh token, so no refresh is possible.
    })
    vi.stubGlobal('fetch', respond(DEPLOYED))
    render(<AuthGate>{child()}</AuthGate>)
    await waitFor(() => expect(screen.getByRole('button', { name: /sign in/i })).toBeTruthy())
    expect(screen.queryByText(PROTECTED)).toBeNull()
  })

  it('requires auth when the config OMITS authRequired', async () => {
    // Defaults must fail closed. `authRequired: body.authRequired === true` — the natural
    // way to write it — opens the app for any config missing the field, and every test
    // that sends it explicitly passes either way. Caught by mutation, not review.
    vi.stubGlobal('fetch', respond({
      hostedUiUrl: DEPLOYED.hostedUiUrl,
      clientId: DEPLOYED.clientId,
      userPoolId: DEPLOYED.userPoolId,
    }))
    render(<AuthGate>{child()}</AuthGate>)
    await waitFor(() => expect(screen.getByRole('button', { name: /sign in/i })).toBeTruthy())
    expect(screen.queryByText(PROTECTED)).toBeNull()
  })

  it('refuses to offer a login button a deployment cannot honour', async () => {
    // `authRequired` with no pool. A button here sends the user to a broken redirect and
    // tells them nothing; naming the cause is the useful failure.
    vi.stubGlobal('fetch', respond({ ...DEPLOYED, clientId: '', hostedUiUrl: '' }))
    render(<AuthGate>{child()}</AuthGate>)
    await waitFor(() => expect(screen.getByText(/names no user pool/i)).toBeTruthy())
    expect(screen.queryByRole('button', { name: /sign in/i })).toBeNull()
    expect(screen.queryByText(PROTECTED)).toBeNull()
  })
})

// --------------------------------------------------------------------------- //
// Opens for the cases it should
// --------------------------------------------------------------------------- //

describe('the gate opens', () => {
  it('renders the app with a live token', async () => {
    storeTokens({
      idToken: 'good',
      accessToken: 'a',
      refreshToken: 'r',
      expiresAt: Math.floor(Date.now() / 1000) + 3600,
    })
    vi.stubGlobal('fetch', respond(DEPLOYED))
    render(<AuthGate>{child()}</AuthGate>)
    await waitFor(() => expect(screen.getByText(PROTECTED)).toBeTruthy())
    expect(screen.getByRole('button', { name: /sign out/i })).toBeTruthy()
  })

  it('renders the app with no login for the local single-user tool', async () => {
    // A clean 404 is the ONE case treated as "no auth": the server answered, and the
    // local tool ships no config file. Distinguishable from a failure for that reason.
    vi.stubGlobal('fetch', respond({}, 404))
    render(<AuthGate>{child()}</AuthGate>)
    await waitFor(() => expect(screen.getByText(PROTECTED)).toBeTruthy())
    // No sign-out button: there is no session to end.
    expect(screen.queryByRole('button', { name: /sign out/i })).toBeNull()
  })
})

// --------------------------------------------------------------------------- //
// Returning from the Hosted UI
// --------------------------------------------------------------------------- //

describe('the callback leg', () => {
  it('exchanges the code, strips it from the URL, and renders the app', async () => {
    sessionStorage.setItem('matrix.pkce_verifier', 'v')
    window.history.replaceState({}, '', '/?code=abc123')
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (String(url).includes('config.json')) {
        return Promise.resolve({ ok: true, status: 200, json: async () => DEPLOYED })
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          id_token: 'fresh', access_token: 'a', refresh_token: 'r', expires_in: 3600,
        }),
      })
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<AuthGate>{child()}</AuthGate>)
    await waitFor(() => expect(screen.getByText(PROTECTED)).toBeTruthy())
    // `?code=` must be stripped: a reload would otherwise retry a CONSUMED code, which
    // Cognito rejects — and that reads as a broken login rather than a stale URL.
    expect(window.location.search).toBe('')
  })

  it('shows the reason when Cognito returns an error', async () => {
    // A cancelled login or a misconfigured client. Without this the user lands on the
    // login screen with no explanation and tries again forever.
    window.history.replaceState({}, '', '/?error=invalid_request&error_description=bad+redirect')
    vi.stubGlobal('fetch', respond(DEPLOYED))
    render(<AuthGate>{child()}</AuthGate>)
    await waitFor(() => expect(screen.getByText(/bad redirect/i)).toBeTruthy())
    expect(screen.queryByText(PROTECTED)).toBeNull()
    expect(window.location.search).toBe('')
  })

  it('shows the reason when the code exchange fails', async () => {
    sessionStorage.setItem('matrix.pkce_verifier', 'v')
    window.history.replaceState({}, '', '/?code=abc123')
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (String(url).includes('config.json')) {
        return Promise.resolve({ ok: true, status: 200, json: async () => DEPLOYED })
      }
      return Promise.resolve({
        ok: false, status: 400, statusText: 'Bad Request', json: async () => ({}),
      })
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<AuthGate>{child()}</AuthGate>)
    await waitFor(() => expect(screen.getByText(/Token request failed/i)).toBeTruthy())
    expect(screen.queryByText(PROTECTED)).toBeNull()
  })
})
